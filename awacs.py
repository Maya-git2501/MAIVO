# awacs_stage11_cap_push.py
# AWACS controller + web UI, with CAP/PUSH flows.
#
# Keeps from stage10:
# - Quiet by default (no auto chatter) EXCEPT high-priority: MERGED, DEFEND (missile).
# - Commands: PICTURE, BOGEY DOPE (true BRAA), DECLARE (+ bulls), SNAP, VECTOR (home/tanker/name),
#             ALPHA CHECK (self/home/tanker), WHO/WHO ALL/WHO UNKNOWN.
# - Enemy altitude in FL or XX,XXX FT (no "angels" for threats).
# - Friendly label preference: Callsign > Pilot (pretty) > Group/Unit > Airframe.
# - Dynamic "home plate" per Blue (first low-speed, low-alt fix). Dynamic tanker discovery.
# - NO CLI flags — tiny local web UI at http://127.0.0.1:8088/
#
# NEW — CAP/PUSH:
#   CAP define:
#     "cap add <name> bulls <BRG> for <RNG> [radius <NM>] [alt <LO>-<HI>]"
#     "cap add <name> at fighter <CALLSIGN> [radius <NM>] [alt <LO>-<HI>]"
#     defaults: radius 10 nm, alt 20-40 (thousands ft)
#   CAP assign:
#     "cap assign <CALLSIGN> <CAPNAME>"
#   CAP status:
#     "cap status"      (all)
#     "cap status <name>"
#   CAP clear:
#     "cap clear <name>" or "cap clear all"
#
#   PUSH plan to a BULLS point:
#     "push set bulls <BRG> for <RNG> now"
#     "push set bulls <BRG> for <RNG> in 90s" | "in 2m" | "at +2:00"
#     recipients: all flights assigned to any CAP at execution time
#     "push status"
#     "push cancel"
#     "push execute"  (run immediately)
#
# Example quick commands:
#   cap add ViperCAP bulls 320 for 40 radius 15 alt 25-35
#   cap assign Warhawk 1-1 ViperCAP
#   cap status
#   push set bulls 330 for 35 in 2m
#   push status
#   push execute
#
# Requires: ttv_raw_listener.py, acmi_parse.py

import json, math, re, threading, webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Tuple, List, Dict
from collections import defaultdict, deque

from ttv_raw_listener import connect_and_handshake, iter_lines
from acmi_parse import (
    ACMIFileParser,
    ACTION_TIME, ACTION_GLOBAL, ACTION_REMOVE, ACTION_UPDATE, ACMIObject
)

# ------------------------------ Constants ------------------------------------

EARTH_M_PER_NM = 1852.0
M_TO_FT = 3.280839895

BLUE_HINTS = {
    "Allies","Blue","NATO","USA","USAF","USN","USMC","U.S.","United States",
    "ROK","Training","Allied","Player","Friendly"
}
RED_HINTS  = {"Enemies","Red","OPFOR","Aggressor","DPRK","Russia","China","Serbia","Hostile","Adversary"}

TANKER_HINTS = ("KC-135","KC135","KC-10","KC10","KC-46","KC46","Tanker","Texaco","Shell","Arco")
MISSILE_HINTS = ("AIM-","R-","AA-","AMRAAM","Sparrow","Sidewinder","Adder","Alamo","Archer","Vympel","PL-","SD-","Meteor","Fox")

# Aspect thresholds (deg)
HOT_MAX   = 45
BEAM_MIN  = 70
BEAM_MAX  = 110
COLD_MIN  = 135

# High-priority thresholds
MERGED_NM = 3.0
MISSILE_ALERT_MAX_RANGE_NM = 30.0  # blue within this of missile birth → DEFEND

# ------------------------------ Sanitizer ------------------------------------

_GLUE_FIX = re.compile(r'(Health=[^,|]+)(?=Name=)')
def sanitize_glued_props(line: str) -> str:
    # Only fix the known Tacview glue: "Health=...Name="
    return _GLUE_FIX.sub(r'\1|', line)

# ------------------------------ Math helpers ---------------------------------

def norm_head(h):
    if h is None: return None
    return (h % 360 + 360) % 360

def bearing_deg(dx, dy):
    ang = math.degrees(math.atan2(dx, dy))
    return (ang + 360.0) % 360.0

def brg_rng_from_xy(ou, ov, tu, tv):
    dx, dy = tu - ou, tv - ov
    brg = bearing_deg(dx, dy)
    rng_nm = math.hypot(dx, dy) / EARTH_M_PER_NM
    return int(round(brg)) % 360, rng_nm

def bulls_to_uv(bu, bv, brg_deg, rng_nm):
    r = math.radians(brg_deg)
    du = math.sin(r) * (rng_nm * EARTH_M_PER_NM)
    dv = math.cos(r) * (rng_nm * EARTH_M_PER_NM)
    return bu + du, bv + dv

def angels_from_meters(m):
    if m is None: return None
    return int(round((m * M_TO_FT) / 1000.0))

def threat_alt_phrase(alt_m) -> str:
    """Enemy altitude formatting: FL if >=18k ft, else XX,XXX FT. If unknown -> ALT UNKNOWN."""
    if alt_m is None:
        return "ALT UNKNOWN"
    ft = alt_m * M_TO_FT
    if ft >= 18000:
        return f"FL{int(round(ft/100.0))}"  # FL180 = 18,000 ft
    ft_int = int(round(ft/100.0))*100
    return f"{ft_int:,} FT"

def dir_word_from_heading(hdg):
    if hdg is None: return None
    dirs = ["north","northeast","east","southeast","south","southwest","west","northwest"]
    idx = int(((hdg % 360) + 22.5) // 45) % 8
    return dirs[idx]

def aspect_word(hdg, brg_from_fighter):
    if hdg is None or brg_from_fighter is None:
        return "UNK"
    diff = abs(((hdg - brg_from_fighter + 540) % 360) - 180)
    if BEAM_MIN <= diff <= BEAM_MAX:
        return f"beam {dir_word_from_heading(hdg) or ''}".strip()
    if diff < HOT_MAX:
        return "HOT"
    if diff > COLD_MIN:
        return "COLD"
    return f"flank {dir_word_from_heading(hdg) or ''}".strip()

# ------------------------------ Object helpers -------------------------------

def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())

def get_xy_mode(obj):
    T = getattr(obj, "T", None)
    if not T: return None
    u = getattr(T, "U", None); v = getattr(T, "V", None)
    if u not in (None, 0.0) or v not in (None, 0.0):
        return ("uv", float(u or 0.0), float(v or 0.0))
    lon = getattr(T, "Longitude", None); lat = getattr(T, "Latitude", None)
    if lon is not None and lat is not None:
        return ("ll", float(lon), float(lat))
    return None

def uv_of(obj) -> Optional[Tuple[float,float]]:
    xy = get_xy_mode(obj)
    if not xy or xy[0] != "uv": return None
    return (xy[1], xy[2])

_airframe_looks_generic_re = re.compile(r'^(?:[A-Z]{1,3}-)?[A-Za-z0-9\-]+$')

def _pilot_to_callsign(p: str) -> Optional[str]:
    """Turn 'Warhawk52' -> 'Warhawk 5-2', 'Plasma31' -> 'Plasma 3-1', keep 'Warhawk 1-1'."""
    if not p: return None
    p = p.strip()
    if re.search(r'\d-\d$', p): 
        return p
    m = re.match(r'^([A-Za-z]+)[\s\-_]*?(\d)\s*[-]?\s*(\d)\s*$', p)
    if m:
        return f"{m.group(1)} {m.group(2)}-{m.group(3)}"
    m = re.match(r'^([A-Za-z]+)[\s\-_]*?(\d)\s*$', p)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return p  # as-is (e.g., 'B-E-B')

def friendly_label(o) -> str:
    """Best-effort human label for a Blue fighter."""
    try:
        cs = getattr(o, "Callsign", None)
        if cs and str(cs).strip():
            return str(cs).strip()
    except Exception:
        pass
    try:
        pilot = getattr(o, "Pilot", None)
        lbl = _pilot_to_callsign(str(pilot)) if pilot else None
        if lbl:
            return lbl
    except Exception:
        pass
    for k in ("Group", "Unit"):
        try:
            v = getattr(o, k, None)
            if v and str(v).strip():
                return str(v).strip()
        except Exception:
            pass
    try:
        nm = getattr(o, "Name", None)
        if nm and str(nm).strip():
            s = str(nm).strip()
            if _airframe_looks_generic_re.match(s) and any(x in s for x in ("F-","MiG","Su-","KC-","B-","C-","Mirage","J-","PL-")):
                return "Fighter"
            return s
    except Exception:
        pass
    return "Fighter"

# ------------------------------ World state ----------------------------------

class TrackWrap:
    __slots__ = ("obj","home_uv","last_u","last_v","last_t","spd_mps")
    def __init__(self, obj):
        self.obj = obj
        self.home_uv = None
        self.last_u = getattr(obj.T,"U",None)
        self.last_v = getattr(obj.T,"V",None)
        self.last_t = None
        self.spd_mps = 0.0
    def update_speed(self, now):
        T = getattr(self.obj, "T", None)
        if not T: return
        if self.last_t is None:
            self.last_t = now
            self.last_u = getattr(T,"U",None)
            self.last_v = getattr(T,"V",None)
            return
        u = getattr(T,"U",None); v = getattr(T,"V",None)
        if None in (u,v,self.last_u,self.last_v): return
        dt = max(1e-6, now - self.last_t)
        self.spd_mps = math.hypot((u - self.last_u)/dt, (v - self.last_v)/dt)
        self.last_t = now
        self.last_u, self.last_v = u, v

class CAPSite:
    __slots__ = ("name","center_uv","radius_nm","alt_lo_ft","alt_hi_ft","assigned_ids")
    def __init__(self, name, center_uv, radius_nm=10.0, alt_lo_ft=20000, alt_hi_ft=40000):
        self.name = name
        self.center_uv = center_uv
        self.radius_nm = float(radius_nm)
        self.alt_lo_ft = float(alt_lo_ft)
        self.alt_hi_ft = float(alt_hi_ft)
        self.assigned_ids: set[str] = set()

class PushPlan:
    __slots__ = ("active","brg","rng","target_uv","exec_time","created_at")
    def __init__(self):
        self.active = False
        self.brg = None
        self.rng = None
        self.target_uv = None
        self.exec_time = None   # world time to execute
        self.created_at = None

class WorldState:
    def __init__(self):
        self.time = 0.0
        self.objects: Dict[str,ACMIObject] = {}
        self.tracks:  Dict[str,TrackWrap]  = {}
        self.last_seen: Dict[str,float]    = {}
        self.bullseye_by_coal: Dict[str,str] = {}
        self.global_props = {}
        self.ttl = 10.0

        # missiles seen (for defend calls)
        self.missiles: Dict[str, ACMIObject] = {}
        self.missile_alerted_ids = set()

        # CAP/PUSH
        self.cap_sites: Dict[str, CAPSite] = {}
        self.push_plan = PushPlan()

    def coalition_of(self, o) -> str:
        c = (getattr(o,"Coalition","") or "")
        if any(tag in c for tag in BLUE_HINTS): return "Blue"
        if any(tag in c for tag in RED_HINTS):  return "Red"
        color = (getattr(o,"Color","") or "").lower()
        if color in ("blue","lightblue","cyan","turquoise"): return "Blue"
        if color in ("red","orange","maroon"): return "Red"
        nm = getattr(o,"Name","") or ""
        if any(h in nm for h in TANKER_HINTS): return "Blue"
        return "Unknown"

    def upsert(self, obj: ACMIObject, now):
        oid = obj.object_id
        self.objects[oid] = obj
        self.last_seen[oid] = now
        if oid not in self.tracks:
            self.tracks[oid] = TrackWrap(obj)
        else:
            self.tracks[oid].obj = obj

        t = getattr(obj,"Type","") or ""
        if "Navaid+Static+Bullseye" in t or "Navaid+Bullseye" in t:
            self.bullseye_by_coal[(getattr(obj,"Coalition","") or "Unknown")] = oid

        # learn "home plate" (first low-speed, low-alt fix)
        if "Air" in str(t) and hasattr(obj,"T"):
            tr = self.tracks[oid]
            T = obj.T
            u,v = getattr(T,"U",None), getattr(T,"V",None)
            alt = getattr(T,"Altitude",None)
            if tr.home_uv is None and None not in (u,v,alt):
                kts = (tr.spd_mps or 0.0) * 1.943844492
                if kts < 60 and (alt < 300.0 or alt != alt):
                    tr.home_uv = (float(u), float(v))

        # track missiles
        if ("Weapon" in t and ("Missile" in t or "Guided" in t)) or any(h in (getattr(obj,"Name","") or "") for h in MISSILE_HINTS):
            self.missiles[oid] = obj

    def remove(self, oid: str):
        self.objects.pop(oid, None)
        self.tracks.pop(oid, None)
        self.last_seen.pop(oid, None)
        self.missiles.pop(oid, None)
        for k,v in list(self.bullseye_by_coal.items()):
            if v == oid:
                self.bullseye_by_coal.pop(k, None)
        for cap in self.cap_sites.values():
            if oid in cap.assigned_ids:
                cap.assigned_ids.discard(oid)

    def cull_stale(self, now):
        for oid,t in list(self.last_seen.items()):
            if now - t > self.ttl:
                self.remove(oid)

    def get_air(self):
        return [o for o in self.objects.values()
                if "Air" in str(getattr(o,"Type","")) and hasattr(o,"T")]

    def find_bull(self):
        for pref in ("Allies","Blue","ROK","NATO","Training","USA","U.S."):
            oid = self.bullseye_by_coal.get(pref)
            if oid and oid in self.objects: return self.objects[oid]
        for oid in self.bullseye_by_coal.values():
            if oid in self.objects: return self.objects[oid]
        blues = [o for o in self.get_air() if self.coalition_of(o)=="Blue"]
        return blues[0] if blues else None

    def blues(self):  return [o for o in self.get_air() if self.coalition_of(o)=="Blue"]
    def reds(self):   return [o for o in self.get_air() if self.coalition_of(o)=="Red"]
    def unks(self):   return [o for o in self.get_air() if self.coalition_of(o)=="Unknown"]

    def nearest_tanker(self) -> Optional[ACMIObject]:
        cands=[]
        for o in self.blues():
            nm=(getattr(o,"Name","") or "")
            if any(h in nm for h in TANKER_HINTS):
                cands.append(o)
        return cands[0] if cands else None

# ------------------------------ Grouping & Picture ---------------------------

def group_contacts(contacts, rng_nm=5.0, alt_ft=2000.0):
    items=[]
    for o in contacts:
        xy = get_xy_mode(o)
        if not xy or xy[0]!="uv": continue
        alt_m = getattr(o.T,"Altitude",None)
        items.append({"o":o, "u":xy[1], "v":xy[2], "alt_ft": alt_m*M_TO_FT if alt_m is not None else None})

    groups=[]; used=set()
    def close(a,b):
        dr_nm = math.hypot(a["u"]-b["u"], a["v"]-b["v"]) / EARTH_M_PER_NM
        dz_ft = abs((a["alt_ft"] or 0)-(b["alt_ft"] or 0))
        return dr_nm <= rng_nm and dz_ft <= alt_ft

    for i in range(len(items)):
        if i in used: continue
        cluster={i}; changed=True
        while changed:
            changed=False
            for j in range(len(items)):
                if j in cluster: continue
                if any(close(items[j], items[k]) for k in list(cluster)):
                    cluster.add(j); changed=True
        used.update(cluster)
        groups.append([items[k] for k in sorted(cluster)])
    return groups

def summarize_groups(groups, bull_uv, ws: WorldState, weapons_free=False):
    summaries=[]
    for g in groups:
        size=len(g)
        cu = sum(it["u"] for it in g)/size
        cv = sum(it["v"] for it in g)/size
        brg, rng_nm = brg_rng_from_xy(bull_uv[0], bull_uv[1], cu, cv)
        alts=[it["alt_ft"] for it in g if it["alt_ft"] is not None]
        lo_ang=hi_ang=None
        if alts:
            lo_ang = int(round(min(alts)/1000.0))
            hi_ang = int(round(max(alts)/1000.0))
        id_counts=defaultdict(int); hdgs=[]
        for it in g:
            coal = ws.coalition_of(it["o"])
            ident = ("Hostile" if weapons_free and coal=="Red"
                     else ("Friendly" if coal=="Blue"
                           else ("Bandit" if coal=="Red" else "Bogey")))
            id_counts[ident]+=1
            h = norm_head(getattr(it["o"].T,"Heading",None))
            if h is not None: hdgs.append(h)
        ident = max(id_counts.items(), key=lambda kv: kv[1])[0] if id_counts else "Bogey"
        avg_hdg = sum(hdgs)/len(hdgs) if hdgs else None
        asp = aspect_word(avg_hdg, brg)
        summaries.append({
            "size":size,"brg":brg,"rng_nm":rng_nm,
            "lo_ang":lo_ang,"hi_ang":hi_ang,"aspect":asp,
            "id":ident,"centroid_uv":(cu,cv)
        })
    summaries.sort(key=lambda s: s["rng_nm"])
    return summaries

def size_word(n):
    return {1:"single",2:"two-ship",3:"three-ship",4:"four-ship"}.get(n, f"{n}-ship")

def format_picture(summaries):
    interest=[s for s in summaries if s["id"] in ("Bogey","Bandit","Hostile")]
    if not interest: return "PICTURE: CLEAN."
    n=len(interest)
    lead = "single group" if n==1 else ("two groups" if n==2 else ("three groups" if n==3 else f"{n} groups"))
    labels=["Nearest group","Second group","Third group","Fourth group","Fifth group"]
    parts=[f"PICTURE: {lead}. "]
    for i,g in enumerate(interest[:5]):
        sz=size_word(g["size"])
        heavy=" HEAVY" if g["size"]>=3 else ""
        ang = ("angels UNK" if g["lo_ang"] is None or g["hi_ang"] is None
               else (f"angels {g['lo_ang']}" if g["lo_ang"]==g["hi_ang"] else f"angels {g['lo_ang']}-{g['hi_ang']}"))
        parts.append(f"{labels[i]} BULLS {g['brg']:03d} for {int(round(g['rng_nm']))}, {sz}{heavy}, {g['id'].lower()}, {ang}, {g['aspect']}. ")
    return "".join(parts).strip()

# ------------------------------ Fighter-centric ops --------------------------

def find_callsign(objects, callsign_substr):
    if not callsign_substr: return None
    q=_norm(callsign_substr)
    def fields(o):
        yield getattr(o,"Name","") or ""
        for k in ("Callsign","Pilot","Unit","Group"):
            try:
                v=getattr(o,k)
                if v: yield str(v)
            except: pass
    for o in objects:
        for s in fields(o):
            if q and q in _norm(s): return o
    return None

def nearest_threat_to_fighter(ws: WorldState, fighter: ACMIObject) -> Optional[ACMIObject]:
    fuv = uv_of(fighter)
    if not fuv: return None
    best=None; best_rng=1e18
    for o in ws.reds()+ws.unks():
        ouv=uv_of(o)
        if not ouv: continue
        _, rng = brg_rng_from_xy(fuv[0], fuv[1], ouv[0], ouv[1])
        if rng < best_rng:
            best=o; best_rng=rng
    return best

def braa_from_fighter_to_target(fighter: ACMIObject, target: ACMIObject) -> Optional[str]:
    fuv = uv_of(fighter); tuv = uv_of(target)
    if not fuv or not tuv: return None
    brg, rng = brg_rng_from_xy(fuv[0], fuv[1], tuv[0], tuv[1])
    t_alt = getattr(target.T,"Altitude",None)
    alt_txt = threat_alt_phrase(t_alt)
    hdg = norm_head(getattr(target.T,"Heading",None))
    asp = aspect_word(hdg, brg)
    return f"BRAA {brg:03d}/{int(round(rng))}, {alt_txt}, {asp}."

def vector_from_fighter_to_uv(fighter: ACMIObject, target_uv: Tuple[float,float]) -> Optional[str]:
    fuv = uv_of(fighter)
    if not fuv or not target_uv: return None
    brg, rng = brg_rng_from_xy(fuv[0], fuv[1], target_uv[0], target_uv[1])
    return f"VECTOR {brg:03d}, {int(round(rng))} miles."

def alpha_check_to_uv(ws: WorldState, target_uv: Tuple[float,float]) -> Optional[str]:
    bull = ws.find_bull()
    buv = uv_of(bull) if bull else None
    if not buv or not target_uv: return None
    brg, rng = brg_rng_from_xy(buv[0], buv[1], target_uv[0], target_uv[1])
    return f"ALPHA CHECK: BULLS {brg:03d} for {int(round(rng))}."

# ------------------------------ CAP/PUSH helpers -----------------------------

def _parse_alt_block(txt: str) -> Tuple[float,float]:
    # expects "<LO>-<HI>" in thousands of feet
    m = re.match(r'^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$', txt or "")
    if not m:
        return (20000.0, 40000.0)
    lo = max(0, int(m.group(1))) * 1000.0
    hi = max(lo+1000, int(m.group(2)) * 1000.0)
    return (lo, hi)

def _parse_time_offset(tstr: str) -> Optional[float]:
    """Return seconds offset for 'in 90s', 'in 2m', 'at +2:30'. Returns None if unknown."""
    if not tstr: return None
    t = tstr.strip().lower()
    if t.startswith("in "):
        t2 = t[3:].strip()
        m = re.match(r'^(\d+)\s*s$', t2)
        if m: return float(m.group(1))
        m = re.match(r'^(\d+)\s*m$', t2)
        if m: return float(m.group(1)) * 60.0
        m = re.match(r'^(\d+)$', t2)
        if m: return float(m.group(1))
    if t.startswith("at +"):
        t2 = t[4:].strip()
        m = re.match(r'^(\d+):(\d{1,2})$', t2)
        if m:
            return float(m.group(1))*60.0 + float(m.group(2))
        m = re.match(r'^(\d+)$', t2)
        if m:
            return float(m.group(1))
    if t in ("now","immediately","0","+0"):
        return 0.0
    return None

# ------------------------------ Parser for commands --------------------------

def parse_command(text: str):
    t=(text or "").strip()
    tl=re.sub(r'\s+',' ',t.lower())

    # lists
    if tl in ("who","callsigns","list"): return ("who", None, None)
    if tl in ("who all","callsigns all","list all"): return ("whoall", None, None)
    if tl in ("who unknown","callsigns unknown","list unknown"): return ("whounk", None, None)

    # alpha check self / to home/home base/home plate / tanker
    m=re.search(r'(alpha\s*check|alphacheck|alpha)\s*(to\s+)?(home(?:\s*plate)?|home\s*base|tanker)?$', tl)
    if m:
        callsign = t[:tl.find(m.group(1))].strip(" ,") if tl.find(m.group(1))>0 else None
        target = (m.group(3) or "").replace(" ","") or None
        return ("alpha", callsign or None, target)

    # vector to home/home base/home plate/tanker/name
    m=re.search(r'(vector)\s+(to\s+)?(home(?:\s*plate)?|home\s*base|tanker|.+)$', tl)
    if m:
        callsign = t[:tl.find(m.group(1))].strip(" ,") if tl.find(m.group(1))>0 else None
        target = (m.group(3) or "").strip()
        return ("vector", callsign or None, target)

    # snap (nearest threat)
    if tl.endswith("snap") or tl.endswith("snap threat") or tl == "snap":
        callsign = re.sub(r'(snap(?:\s*threat)?)$','',t,flags=re.I).strip(" ,")
        return ("snap", callsign or None, None)

    # declare [optional bulls ### for ##]
    m=re.search(r'(declare)(?:\s+(bulls\s+)?(\d{2,3})\s*(for|/)\s*(\d{1,3}))?$', tl)
    if m:
        callsign = t[:tl.find(m.group(1))].strip(" ,") if tl.find(m.group(1))>0 else None
        if m.group(3):
            return ("declare_bulls", callsign or None, (int(m.group(3)), int(m.group(5))))
        return ("declare", callsign or None, None)

    # bogey dope
    m=re.search(r'(bogey\s*dope|bogeydope|dope)$', tl)
    if m:
        callsign = t[:len(tl) - len(m.group(1))].strip(" ,")
        return ("bogeydope", callsign or None, None)

    # picture
    if tl.endswith("picture") or tl == "picture":
        callsign = t[:tl.rfind("picture")].strip(" ,") if tl.endswith("picture") and tl != "picture" else None
        return ("picture", callsign or None, None)

    # ----------------- CAP / PUSH -----------------

    # cap add <name> bulls <brg> for <rng> [radius <nm>] [alt L-H]
    m = re.match(r'^cap\s+add\s+([a-z0-9_\-]+)\s+bulls\s+(\d{2,3})\s*(?:for|/)\s*(\d{1,3})(?:\s+radius\s+(\d{1,3}))?(?:\s+alt\s+(\d{1,2}-\d{1,2}))?$', tl)
    if m:
        return ("cap_add_bulls", m.group(1), {
            "brg": int(m.group(2)), "rng": int(m.group(3)),
            "radius": float(m.group(4)) if m.group(4) else 10.0,
            "alt": m.group(5) or "20-40"
        })
    # cap add <name> at fighter <callsign> [radius <nm>] [alt L-H]
    m = re.match(r'^cap\s+add\s+([a-z0-9_\-]+)\s+at\s+fighter\s+(.+?)(?:\s+radius\s+(\d{1,3}))?(?:\s+alt\s+(\d{1,2}-\d{1,2}))?$', tl)
    if m:
        return ("cap_add_at_fighter", m.group(1), {
            "cs": m.group(2).strip(),
            "radius": float(m.group(3)) if m.group(3) else 10.0,
            "alt": m.group(4) or "20-40"
        })
    # cap assign <callsign> <name>
    m = re.match(r'^cap\s+assign\s+(.+?)\s+([a-z0-9_\-]+)$', tl)
    if m:
        return ("cap_assign", m.group(1).strip(), m.group(2))
    # cap status [name]
    m = re.match(r'^cap\s+status(?:\s+([a-z0-9_\-]+))?$', tl)
    if m:
        return ("cap_status", m.group(1), None)
    # cap clear <name>|all
    m = re.match(r'^cap\s+clear\s+(all|[a-z0-9_\-]+)$', tl)
    if m:
        return ("cap_clear", m.group(1), None)

    # push set bulls <brg> for <rng> (now|in Xs|in Xm|at +mm:ss)
    m = re.match(r'^push\s+set\s+bulls\s+(\d{2,3})\s*(?:for|/)\s*(\d{1,3})\s+(now|in\s+\d+\s*[ms]?|at\s+\+\d+(?::\d{1,2})?)$', tl)
    if m:
        return ("push_set", (int(m.group(1)), int(m.group(2))), m.group(3))
    if tl == "push status":
        return ("push_status", None, None)
    if tl == "push cancel":
        return ("push_cancel", None, None)
    if tl == "push execute":
        return ("push_execute", None, None)

    # default: treat text as "<callsign> picture"
    return ("picture", t, None)

# ------------------------------ Controller Loop ------------------------------

class Controller:
    def __init__(self):
        self.ws = WorldState()
        self.parser = ACMIFileParser()
        self.sock = None
        self.connected = False

        # tunables (could be UI-exposed later)
        self.hz = 1.0
        self.grp_range_nm = 5.0
        self.grp_alt_ft = 2000.0
        self.weapons_free = False

        # state
        self._last_emit_tick = -1
        self.console_log = deque(maxlen=800)
        self._stop = threading.Event()
        self._thread = None

        # missile birth tracking (to issue DEFEND quickly)
        self._known_ids = set()  # track seen object IDs to detect births

        # deconfliction
        self._last_merged_tick = -9999
        self._merged_cooldown = 10  # seconds

    def log(self, s):
        print(s)
        self.console_log.append(s)

    def connect(self, host="127.0.0.1", port=42674, password="0"):
        if self.connected:
            try: self.sock.close()
            except: pass
            self.connected = False
        self.sock = connect_and_handshake(host, int(port), str(password))
        self.connected = True
        self._stop.clear()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()
        self.log(f"[connected] Tacview RT at {host}:{port}")

    def stop(self):
        self._stop.set()
        try:
            if self.sock: self.sock.close()
        except: pass
        self.connected = False

    # --- High-priority emitters ---
    def _check_merged(self, tick_now: int):
        blues = self.ws.blues(); reds = self.ws.reds()
        if not blues or not reds: return
        merged_line = None
        for e in reds:
            euv = uv_of(e)
            if not euv: continue
            for b in blues:
                buv = uv_of(b)
                if not buv: continue
                _, rng = brg_rng_from_xy(euv[0], euv[1], buv[0], buv[1])
                if rng <= MERGED_NM:
                    alt_txt = threat_alt_phrase(getattr(e.T,"Altitude",None))
                    merged_line = f"MERGED, {alt_txt}."
                    break
            if merged_line: break
        if merged_line and tick_now - self._last_merged_tick >= self._merged_cooldown:
            self.log(merged_line)
            self._last_merged_tick = tick_now

    def _check_missile_birth(self, obj: ACMIObject):
        oid = obj.object_id
        if oid in self._known_ids:
            return
        self._known_ids.add(oid)

        t = getattr(obj,"Type","") or ""
        nm = getattr(obj,"Name","") or ""
        if not (("Weapon" in t and ("Missile" in t or "Guided" in t)) or any(h in nm for h in MISSILE_HINTS)):
            return  # not a missile
        # Only alert for hostile shots
        if self.ws.coalition_of(obj) == "Blue":
            return

        muv = uv_of(obj)
        if not muv: return
        # find nearest blue at birth
        best = None; best_rng = 1e18
        for b in self.ws.blues():
            buv = uv_of(b)
            if not buv: continue
            _, rng = brg_rng_from_xy(muv[0], muv[1], buv[0], buv[1])
            if rng < best_rng:
                best, best_rng = b, rng

        if best and best_rng <= MISSILE_ALERT_MAX_RANGE_NM:
            label = friendly_label(best)
            # From Blue to missile
            b_brg, b_rng = brg_rng_from_xy(uv_of(best)[0], uv_of(best)[1], muv[0], muv[1])
            self.log(f"{label}, DEFEND! Missile inbound, BRAA {b_brg:03d}/{int(round(b_rng))}.")

    # --- CAP/PUSH internals ---
    def _cap_status_line(self, name: Optional[str]=None) -> str:
        ws = self.ws
        if not ws.cap_sites:
            return "CAP: none."
        def one(cap: CAPSite):
            parts=[f"{cap.name}: radius {int(round(cap.radius_nm))} nm, alt {int(cap.alt_lo_ft/1000)}-{int(cap.alt_hi_ft/1000)}."]
            # assignments
            if not cap.assigned_ids:
                parts.append("   assigned: none")
            else:
                shows=[]
                for oid in list(cap.assigned_ids):
                    o = ws.objects.get(oid)
                    if not o:
                        cap.assigned_ids.discard(oid)
                        continue
                    lbl = friendly_label(o)
                    ouv = uv_of(o)
                    on = False
                    if ouv:
                        brg, rng = brg_rng_from_xy(cap.center_uv[0], cap.center_uv[1], ouv[0], ouv[1])
                        alt_m = getattr(o.T,"Altitude",None)
                        alt_ft = alt_m*M_TO_FT if alt_m is not None else None
                        on = (rng <= cap.radius_nm) and (alt_ft is not None and cap.alt_lo_ft <= alt_ft <= cap.alt_hi_ft)
                        shows.append(f"{lbl} — {int(round(rng))} nm off center, {'on-station' if on else 'off-station'}")
                    else:
                        shows.append(f"{lbl} — pos unknown")
                if shows:
                    parts.append("   assigned: " + "; ".join(shows))
                else:
                    parts.append("   assigned: none")
            return "\n".join(parts)
        if name:
            cap = ws.cap_sites.get(name)
            if not cap: return f"CAP: {name} not found."
            return one(cap)
        return "\n".join(one(c) for c in ws.cap_sites.values())

    def _push_status_line(self) -> str:
        p = self.ws.push_plan
        if not p.active:
            return "PUSH: no plan."
        return f"PUSH: BULLS {p.brg:03d} for {int(p.rng)}, exec at T+{int(max(0,p.exec_time - self.ws.time))}s."

    def _try_execute_push(self):
        p = self.ws.push_plan
        if not p.active: return
        if self.ws.time is None: return
        if self.ws.time < (p.exec_time or 0): return
        # Recipients = all current flights assigned to any CAP right now
        recips=set()
        for cap in self.ws.cap_sites.values():
            recips |= set(cap.assigned_ids)
        if not recips:
            self.log("PUSH: no assigned flights.")
        else:
            for oid in list(recips):
                o = self.ws.objects.get(oid)
                if not o: continue
                label = friendly_label(o)
                self.log(f"{label}, PUSH, BULLS {p.brg:03d} for {int(p.rng)}.")
        p.active = False  # clear after execution

    # --- Loop ---
    def _run_loop(self):
        try:
            for raw in iter_lines(self.sock):
                if self._stop.is_set(): break
                raw = sanitize_glued_props(raw)
                ent = self.parser.parse_line(raw)
                if ent is None: continue

                if ent.action == ACTION_TIME:
                    self.ws.time = ent.timestamp or self.ws.time
                    for tr in self.ws.tracks.values():
                        tr.update_speed(self.ws.time)
                    self.ws.cull_stale(self.ws.time)

                    tick = int(self.ws.time * self.hz)
                    if tick != self._last_emit_tick:
                        self._last_emit_tick = tick
                        # High-priority only (no automatic picture chatter)
                        self._check_merged(tick)
                        # PUSH scheduler
                        self._try_execute_push()

                elif ent.action == ACTION_GLOBAL and isinstance(ent, ACMIObject):
                    props = getattr(ent, "properties", None)
                    if isinstance(props, dict):
                        self.ws.global_props.update(props)

                elif ent.action == ACTION_REMOVE:
                    self.ws.remove(ent.object_id)

                elif ent.action == ACTION_UPDATE and isinstance(ent, ACMIObject):
                    T = getattr(ent, "T", None)
                    if T and hasattr(T, "Heading"):
                        T.Heading = norm_head(T.Heading)
                    # Detect missile birth BEFORE storing (so existed-check works via _known_ids)
                    self._check_missile_birth(ent)
                    self.ws.upsert(ent, self.ws.time)
        except Exception as e:
            self.log(f"[error] run loop: {e}")
            self.connected = False

    # ------------- Command processing (UI talks to this) ---------------------

    def handle_text(self, text: str) -> str:
        cmd, cs, target = parse_command(text)
        ws = self.ws
        bull = ws.find_bull()
        buv = uv_of(bull) if bull else None
        blues = ws.blues()
        reds = ws.reds(); unks = ws.unks()
        interest = reds + unks

        def who_line():
            items = []
            for o in blues[:50]:
                label = friendly_label(o)
                airframe = getattr(o, 'Name', '?')
                items.append(f"{label} ({airframe})")
            return "Blue tracks: " + (", ".join(items) if items else "none")

        # -------- Lists
        if cmd == "who": return who_line()
        if cmd == "whoall":
            def fmt(o):
                return f"{getattr(o,'Name','(no Name)')} [Coal={getattr(o,'Coalition','?')}, Color={getattr(o,'Color','?')}, Type={getattr(o,'Type','?')}]"
            reds_l  = [fmt(o) for o in reds[:12]]
            blues_l = [fmt(o) for o in blues[:12]]
            unk_l   = [fmt(o) for o in unks[:12]]
            return (f"Seen — Blue:{len(blues)} Red:{len(reds)} Unknown:{len(unks)}\n"
                    f"Blue:    " + (", ".join(blues_l) if blues_l else "—") + "\n"
                    f"Red:     " + (", ".join(reds_l)  if reds_l  else "—") + "\n"
                    f"Unknown: " + (", ".join(unk_l)   if unk_l   else "—"))
        if cmd == "whounk":
            def fmtu(o):
                return f"{getattr(o,'Name','(no Name)')} [Coal={getattr(o,'Coalition','?')}, Color={getattr(o,'Color','?')}, Type={getattr(o,'Type','?')}]"
            unk_l = [fmtu(o) for o in unks[:25]]
            return "Unknown tracks: " + (", ".join(unk_l) if unk_l else "none")

        # -------- PICTURE
        if cmd == "picture":
            if buv and interest:
                groups = group_contacts(interest, rng_nm=self.grp_range_nm, alt_ft=self.grp_alt_ft)
                sums = summarize_groups(groups, buv, ws, weapons_free=self.weapons_free)
                pic_line = format_picture(sums)
            elif not interest:
                pic_line = "PICTURE: CLEAN."
            else:
                pic_line = "PICTURE: no bullseye available yet."
            return (f"{cs}, {pic_line}" if cs else pic_line)

        # -------- BOGEY DOPE (direct BRAA)
        if cmd == "bogeydope":
            fr = find_callsign(blues, cs) if cs else (blues[0] if blues else None)
            if not fr:
                return f"{cs or 'Fighter'}, UNABLE: no friendly reference available."
            tgt = nearest_threat_to_fighter(ws, fr)
            if not tgt:
                return f"{friendly_label(fr)}, PICTURE: CLEAN."
            braa = braa_from_fighter_to_target(fr, tgt)
            return f"{friendly_label(fr)}, BOGEY DOPE: {braa}"

        # -------- DECLARE
        if cmd == "declare":
            fr = find_callsign(blues, cs) if cs else (blues[0] if blues else None)
            if not fr: return f"{cs or 'Fighter'}, UNABLE: declare (no friendly reference)."
            tgt = nearest_threat_to_fighter(ws, fr)
            if not tgt: return "DECLARE: no factor."
            coal = ws.coalition_of(tgt)
            ident = "Friendly" if coal=="Blue" else ("Bandit" if coal=="Red" else "Bogey")
            return f"DECLARE: {ident}."
        if cmd == "declare_bulls":
            if not buv: return "DECLARE: no bullseye available."
            brg, rng = target
            tu, tv = bulls_to_uv(buv[0], buv[1], brg, rng)
            best=None; best_rng=1e18
            for o in interest + blues:
                ouv = uv_of(o)
                if not ouv: continue
                _, rnm = brg_rng_from_xy(tu, tv, ouv[0], ouv[1])
                if rnm < best_rng:
                    best, best_rng = o, rnm
            if not best or best_rng > 6.0:
                return "DECLARE: no factor."
            coal = ws.coalition_of(best)
            ident = "Friendly" if coal=="Blue" else ("Bandit" if coal=="Red" else "Bogey")
            return f"DECLARE: {ident}."

        # -------- SNAP (vector to nearest threat)
        if cmd == "snap":
            fr = find_callsign(blues, cs) if cs else (blues[0] if blues else None)
            if not fr: return f"{cs or 'Fighter'}, UNABLE: snap (no friendly reference)."
            tgt = nearest_threat_to_fighter(ws, fr)
            if not tgt: return f"{friendly_label(fr)}, SNAP: no factor."
            tuv = uv_of(tgt); line = vector_from_fighter_to_uv(fr, tuv)
            return f"{friendly_label(fr)}, {line}" if line else "UNABLE: snap."

        # -------- VECTOR (home/tanker/name)
        if cmd == "vector":
            fr = find_callsign(blues, cs) if cs else (blues[0] if blues else None)
            if not fr: return f"{cs or 'Fighter'}, UNABLE: vector (no friendly reference)."
            tgt_name = (target or "").strip().lower()
            if "home" in tgt_name:
                tr = ws.tracks.get(fr.object_id)
                tuv = tr.home_uv if tr and tr.home_uv else uv_of(fr)
                line = vector_from_fighter_to_uv(fr, tuv) if tuv else None
                return f"{friendly_label(fr)}, {line}" if line else "UNABLE: vector home."
            if "tanker" in tgt_name:
                tan = ws.nearest_tanker()
                if not tan: return "UNABLE: no tanker."
                tuv = uv_of(tan); line = vector_from_fighter_to_uv(fr, tuv) if tuv else None
                return f"{friendly_label(fr)}, {line}" if line else "UNABLE: vector tanker."
            tgt = find_callsign(blues, target)
            if not tgt: return "UNABLE: target not found."
            tuv = uv_of(tgt); line = vector_from_fighter_to_uv(fr, tuv) if tuv else None
            return f"{friendly_label(fr)}, {line}" if line else "UNABLE: vector."

        # -------- ALPHA CHECK (self/home/tanker)
        if cmd == "alpha":
            if not buv: return "ALPHA CHECK: no bullseye available."
            tgt_uv=None
            tgt_key = (target or "").replace(" ","").lower()
            fr = find_callsign(blues, cs) if cs else (blues[0] if blues else None)
            if tgt_key in ("home","homeplate","homebase"):
                if fr:
                    tr = ws.tracks.get(fr.object_id)
                    if tr and tr.home_uv: tgt_uv = tr.home_uv
                    else: tgt_uv = uv_of(fr)
            elif tgt_key == "tanker":
                tan = ws.nearest_tanker()
                if tan: tgt_uv = uv_of(tan)
            else:
                if fr: tgt_uv = uv_of(fr)
            line = alpha_check_to_uv(ws, tgt_uv) if tgt_uv else None
            if line:
                if cs: return f"{cs}, {line}"
                if fr: return f"{friendly_label(fr)}, {line}"
                return line
            return f"{cs or (friendly_label(fr) if fr else 'Fighter')}, UNABLE: alpha check."

        # -------- CAP / PUSH --------
        if cmd == "cap_add_bulls":
            if not buv: return "CAP: UNABLE — no bullseye."
            data = target
            brg, rng = data["brg"], data["rng"]
            cu, cv = bulls_to_uv(buv[0], buv[1], brg, rng)
            lo, hi = _parse_alt_block(data["alt"])
            ws.cap_sites[cs] = CAPSite(cs, (cu,cv), radius_nm=data["radius"], alt_lo_ft=lo, alt_hi_ft=hi)
            return f"CAP: {cs} set at BULLS {brg:03d}/{rng}, radius {int(data['radius'])} nm, alt {int(lo/1000)}-{int(hi/1000)}."
        if cmd == "cap_add_at_fighter":
            data = target
            f = find_callsign(blues, data["cs"])
            if not f: return "CAP: UNABLE — fighter not found."
            fuv = uv_of(f)
            if not fuv: return "CAP: UNABLE — fighter pos unknown."
            lo, hi = _parse_alt_block(data["alt"])
            ws.cap_sites[cs] = CAPSite(cs, fuv, radius_nm=data["radius"], alt_lo_ft=lo, alt_hi_ft=hi)
            return f"CAP: {cs} set at {friendly_label(f)} position, radius {int(data['radius'])} nm, alt {int(lo/1000)}-{int(hi/1000)}."
        if cmd == "cap_assign":
            fighter = find_callsign(blues, cs)
            if not fighter: return "CAP: UNABLE — fighter not found."
            cap = ws.cap_sites.get(target)
            if not cap: return f"CAP: {target} not found."
            cap.assigned_ids.add(fighter.object_id)
            return f"CAP: assigned {friendly_label(fighter)} to {cap.name}."
        if cmd == "cap_status":
            return self._cap_status_line(cs)
        if cmd == "cap_clear":
            if cs == "all":
                ws.cap_sites.clear()
                return "CAP: cleared all."
            if cs in ws.cap_sites:
                ws.cap_sites.pop(cs, None)
                return f"CAP: cleared {cs}."
            return f"CAP: {cs} not found."

        if cmd == "push_set":
            if not buv: return "PUSH: UNABLE — no bullseye."
            brg, rng = target
            offs = _parse_time_offset(cs)  # here 'cs' holds the time phrase from parse_command
            if offs is None: return "PUSH: UNABLE — bad time (use 'now', 'in 90s', 'in 2m', or 'at +mm:ss')."
            p = ws.push_plan
            p.brg, p.rng = brg, rng
            p.target_uv = bulls_to_uv(buv[0], buv[1], brg, rng)
            p.exec_time = (ws.time or 0.0) + offs
            p.created_at = ws.time or 0.0
            p.active = True
            when = "now" if offs == 0 else f"T+{int(offs)}s"
            return f"PUSH: set BULLS {brg:03d}/{rng}, exec {when}."
        if cmd == "push_status":
            return self._push_status_line()
        if cmd == "push_cancel":
            ws.push_plan = PushPlan()
            return "PUSH: canceled."
        if cmd == "push_execute":
            if not ws.push_plan.active:
                return "PUSH: no plan."
            # execute immediately
            ws.push_plan.exec_time = ws.time or 0.0
            self._try_execute_push()
            return "PUSH: executed."

        return "UNABLE: command."

# ------------------------------ Web UI (no deps) -----------------------------

HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>AIVO AWACS Panel</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:#0b1220;color:#dce3f1}
header{padding:12px 16px;background:#111a2e;border-bottom:1px solid #1f2a44}
h1{margin:0;font-size:18px}
main{display:flex;gap:16px;padding:16px}
.card{background:#0f172a;border:1px solid #23304f;border-radius:8px; padding:12px; flex:1}
label{display:block;margin:6px 0 2px;font-size:12px;color:#a9b7d0}
input,button,select,textarea{font:inherit;border-radius:6px;border:1px solid #304167;background:#0b1220;color:#eaf0ff;padding:8px}
button{cursor:pointer}
.row{display:flex;gap:8px;align-items:end;flex-wrap:wrap}
small{color:#9fb1d6}
#log{height:180px;width:100%;resize:vertical}
pre{white-space:pre-wrap;word-break:break-word}
.badge{display:inline-block;background:#23304f;border:1px solid #304167;padding:2px 6px;border-radius:999px;font-size:12px;margin-right:6px}
.switch{display:inline-flex;align-items:center;gap:8px}
</style>
</head>
<body>
<header><h1>AIVO AWACS Panel</h1></header>
<main>
  <section class="card" style="max-width:520px">
    <h3>Connection</h3>
    <div class="row">
      <div><label>Host</label><input id="host" value="127.0.0.1"></div>
      <div><label>Port</label><input id="port" value="42674" style="width:90px"></div>
      <div><label>Password</label><input id="pass" value="0" style="width:110px"></div>
      <div><button onclick="doConnect()">Connect</button></div>
    </div>
    <p class="switch">
      <label><input type="checkbox" id="wf" onchange="setWF()"> Weapons Free</label>
      <small id="status">Not connected</small>
    </p>

    <h3>Quick Commands</h3>
    <div class="row">
      <div><label>Callsign (optional)</label><input id="cs" placeholder="e.g., Plasma31"></div>
    </div>
    <div class="row">
      <button onclick="sendCmd('who')">who</button>
      <button onclick="sendCmd('who all')">who all</button>
      <button onclick="sendCmd(csVal() ? csVal()+' picture' : 'picture')">picture</button>
      <button onclick="sendCmd(csVal() ? csVal()+' bogey dope' : 'bogey dope')">bogey dope</button>
      <button onclick="sendCmd(csVal() ? csVal()+' declare' : 'declare')">declare</button>
      <button onclick="sendCmd(csVal() ? csVal()+' snap' : 'snap')">snap</button>
    </div>
    <div class="row">
      <button onclick="sendCmd(csVal() ? csVal()+' alpha check' : 'alpha check')">alpha (self)</button>
      <button onclick="sendCmd(csVal() ? csVal()+' alpha check home' : 'alpha check home')">alpha → home</button>
      <button onclick="sendCmd(csVal() ? csVal()+' alpha check tanker' : 'alpha check tanker')">alpha → tanker</button>
    </div>
    <div class="row">
      <button onclick="sendCmd(csVal() ? csVal()+' vector to home' : 'vector to home')">vector → home</button>
      <button onclick="sendCmd(csVal() ? csVal()+' vector to tanker' : 'vector to tanker')">vector → tanker</button>
      <button onclick="sendCmd(csVal() ? csVal()+' vector to '+prompt('Vector to (name):','Texaco 11') : 'vector to '+prompt('Vector to (name):','Texaco 11'))">vector → name</button>
    </div>
    <h3>CAP / PUSH</h3>
    <div class="row">
      <button onclick="sendCmd('cap status')">CAP status</button>
      <button onclick="sendCmd('push status')">PUSH status</button>
    </div>
    <p><small>Examples (type in Command box):<br>
      cap add ViperCAP bulls 320 for 40 radius 15 alt 25-35<br>
      cap assign Warhawk 1-1 ViperCAP<br>
      push set bulls 330 for 35 in 2m
    </small></p>
    <div class="row">
      <div style="flex:1">
        <label>Command</label>
        <input id="free" placeholder="type any command and press Run">
      </div>
      <button onclick="sendCmd(document.getElementById('free').value);">Run</button>
    </div>
  </section>

  <section class="card">
    <h3>Reply</h3>
    <pre id="reply">—</pre>
    <h3>World</h3>
    <div id="world">
      <span class="badge" id="counts">air: — | blue: — | red: — | unk: — | bull: —</span>
    </div>
    <h3>Console (auto only: MERGED / DEFEND / PUSH / errors)</h3>
    <textarea id="log" readonly></textarea>
  </section>
</main>
<script>
let lastState = null;
function csVal(){ return document.getElementById('cs').value.trim(); }

async function doConnect(){
  const host=document.getElementById('host').value.trim();
  const port=document.getElementById('port').value.trim();
  const pass=document.getElementById('pass').value.trim();
  const r=await fetch('/api/connect',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({host,port,password:pass})});
  const j=await r.json();
  document.getElementById('status').textContent=j.ok?'Connected':'Failed';
  setTimeout(pollState,300);
}

async function setWF(){
  const wf=document.getElementById('wf').checked;
  await fetch('/api/config',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({weapons_free:wf})});
}

async function pollState(){
  try{
    const r=await fetch('/api/state');
    const j=await r.json();
    lastState=j;
    document.getElementById('counts').textContent=`air: ${j.air} | blue: ${j.blue} | red: ${j.red} | unk: ${j.unk} | bull: ${j.bull?'Y':'N'}`;
    document.getElementById('log').value=j.console.join('\\n');
  }catch(e){}
  setTimeout(pollState,1000);
}

async function sendCmd(text){
  if(!text) return;
  const r=await fetch('/api/command',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({text})});
  const j=await r.json();
  document.getElementById('reply').textContent=j.reply||'(no reply)';
}

window.addEventListener('load', ()=>{ pollState(); });
</script>
</body>
</html>
"""

class WebHandler(BaseHTTPRequestHandler):
    controller: 'Controller' = None

    def _json(self, code:int, data:dict):
        b = json.dumps(data).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index.html"):
            body = HTML.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body); return
        if self.path.startswith("/api/state"):
            ws = self.controller.ws
            air = ws.get_air()
            blues = ws.blues()
            reds = ws.reds(); unks = ws.unks()
            bull = ws.find_bull() is not None
            self._json(200, {
                "air": len(air), "blue": len(blues), "red": len(reds), "unk": len(unks), "bull": bull,
                "console": list(self.controller.console_log),
                "weapons_free": self.controller.weapons_free
            }); return
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get('Content-Length','0'))
        data = self.rfile.read(length)
        try: payload = json.loads(data.decode('utf-8')) if data else {}
        except: payload = {}
        if self.path.startswith("/api/connect"):
            host = payload.get("host","127.0.0.1")
            port = int(payload.get("port", 42674))
            password = str(payload.get("password","0"))
            try:
                self.controller.connect(host, port, password)
                self._json(200, {"ok": True})
            except Exception as e:
                self.controller.console_log.append(f"[connect error] {e}")
                self._json(200, {"ok": False, "error": str(e)})
            return
        if self.path.startswith("/api/command"):
            text = payload.get("text","")
            reply = self.controller.handle_text(text)
            self._json(200, {"ok": True, "reply": reply}); return
        if self.path.startswith("/api/config"):
            wf = bool(payload.get("weapons_free", False))
            self.controller.weapons_free = wf
            self._json(200, {"ok": True}); return
        self.send_error(404)

# --------------------------------- Boot --------------------------------------

def main():
    ctrl = Controller()
    WebHandler.controller = ctrl
    srv = HTTPServer(("127.0.0.1", 8088), WebHandler)
    url = "http://127.0.0.1:8088/"
    print(f"[ui] Open {url}")
    try: webbrowser.open(url)
    except: pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ctrl.stop()
        srv.server_close()

if __name__ == "__main__":
    main()
