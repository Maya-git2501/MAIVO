# awacs_stage4_brevity.py
import argparse, math
from collections import defaultdict
from ttv_raw_listener import connect_and_handshake, iter_lines
from acmi_parse import (
    ACMIFileParser,
    ACTION_TIME, ACTION_GLOBAL, ACTION_REMOVE, ACTION_UPDATE, ACMIObject
)

# --- Constants & sets ---------------------------------------------------------
EARTH_M_PER_NM = 1852.0
M_TO_FT = 3.280839895
KTS_PER_MPS = 1.943844492

BLUE_HINTS = {"Allies","Blue","NATO","USA","USAF","ROK","Training","Allied","Player","Friendly"}
RED_HINTS  = {"Enemies","Red","OPFOR","Aggressor","DPRK","Russia","China","Serbia","Hostile","Adversary"}

# Aspect thresholds (degrees) – NATO-ish
HOT_MAX   = 45
BEAM_MIN  = 70
BEAM_MAX  = 110
COLD_MIN  = 135

# --- Helpers -----------------------------------------------------------------
def norm_head(h):
    if h is None: return None
    return (h % 360 + 360) % 360

def bearing_deg(dx, dy):
    ang = math.degrees(math.atan2(dx, dy))
    return (ang + 360.0) % 360.0

def bulls_to_contact(bu, bv, tu, tv, mode="uv", latref=None):
    if mode == "uv":
        dx, dy = tu - bu, tv - bv
        brg = bearing_deg(dx, dy)
        rng_nm = math.hypot(dx, dy) / EARTH_M_PER_NM
        return int(round(brg)) % 360, rng_nm
    # lon/lat fallback (small-angle approx)
    latref = (bv + tv)/2.0 if latref is None else latref
    dx_nm = (tu - bu) * 60.0 * max(0.01, abs(math.cos(math.radians(latref))))
    dy_nm = (tv - bv) * 60.0
    brg = bearing_deg(dx_nm, dy_nm)
    rng_nm = math.hypot(dx_nm, dy_nm)
    return int(round(brg)) % 360, rng_nm

def angels_from_meters(m):
    if m is None: return None
    return int(round((m * M_TO_FT) / 1000.0))

def coalition_hint(s: str) -> str:
    s = (s or "").strip()
    if any(tag in s for tag in BLUE_HINTS): return "Blue"
    if any(tag in s for tag in RED_HINTS):  return "Red"
    return "Unknown"

def classify_id(obj, roe_weapons_free=False):
    """
    Returns one of: 'Friendly', 'Bandit', 'Hostile', 'Bogey'
    Logic: coalition tags -> Friendly/Red; Unknown -> Bogey.
           Red => Bandit by default; Hostile if weapons-free flag set.
    """
    coal = coalition_hint(getattr(obj, "Coalition",""))
    if coal == "Blue":    return "Friendly"
    if coal == "Red":     return "Hostile" if roe_weapons_free else "Bandit"
    return "Bogey"

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

def dir_word_from_heading(hdg):
    """Cardinal direction word from heading."""
    if hdg is None: return None
    dirs = ["north","northeast","east","southeast","south","southwest","west","northwest"]
    idx = int(( (hdg % 360) + 22.5 ) // 45) % 8
    return dirs[idx]

def aspect_word(hdg, brg_from_friend):
    """
    HOT/COLD/FLANK/BEAM + optional cardinal (for beam/flank).
    """
    if hdg is None or brg_from_friend is None:
        return "UNK"
    diff = abs(((hdg - brg_from_friend + 540) % 360) - 180)
    # Determine beam/flank first, then hot/cold extremes
    if BEAM_MIN <= diff <= BEAM_MAX:
        return f"beam {dir_word_from_heading(hdg) or ''}".strip()
    if diff < HOT_MAX:
        return "HOT"
    if diff > COLD_MIN:
        return "COLD"
    # otherwise flanking – include general direction
    return f"flank {dir_word_from_heading(hdg) or ''}".strip()

# --- Tracks & world -----------------------------------------------------------
class TrackWrap:
    __slots__ = ("obj","last_u","last_v","last_t","spd_kts")
    def __init__(self, obj):
        self.obj = obj
        self.last_u = getattr(obj.T, "U", None)
        self.last_v = getattr(obj.T, "V", None)
        self.last_t = None
        self.spd_kts = None
    def update_speed(self, now):
        T = getattr(self.obj, "T", None)
        if not T or self.last_t is None:
            self.last_t = now
            self.last_u = getattr(T, "U", None)
            self.last_v = getattr(T, "V", None)
            self.spd_kts = None
            return
        u = getattr(T, "U", None); v = getattr(T, "V", None)
        if None in (u, v, self.last_u, self.last_v):
            self.spd_kts = None
        else:
            dt = max(1e-6, now - self.last_t)
            mps = math.hypot((u - self.last_u)/dt, (v - self.last_v)/dt)
            self.spd_kts = mps * KTS_PER_MPS
        self.last_t = now
        self.last_u, self.last_v = u, v

class WorldState:
    def __init__(self, ttl=10.0):
        self.time = 0.0
        self.global_props = {}
        self.objects = {}           # id -> ACMIObject
        self.tracks = {}            # id -> TrackWrap
        self.last_seen = {}         # id -> time
        self.bullseye_ids_by_coal = {}
        self.ttl = ttl

        # for FADED/merged de-spam
        self._faded_announced = set()
        self._last_picture_line = None
        self._last_emit_tick = -1
        self._last_merged_tick = -9999

    def upsert(self, obj: ACMIObject, now):
        oid = obj.object_id
        self.objects[oid] = obj
        self.last_seen[oid] = now
        if oid not in self.tracks:
            self.tracks[oid] = TrackWrap(obj)
        else:
            self.tracks[oid].obj = obj

        t = getattr(obj, "Type", "")
        if "Navaid+Bullseye" in t:
            coal = getattr(obj, "Coalition", None) or "Unknown"
            self.bullseye_ids_by_coal[coal] = oid

    def remove(self, oid: str):
        self.objects.pop(oid, None)
        self.tracks.pop(oid, None)
        self.last_seen.pop(oid, None)
        for k, v in list(self.bullseye_ids_by_coal.items()):
            if v == oid:
                self.bullseye_ids_by_coal.pop(k, None)

    def cull_stale(self, now):
        for oid, t in list(self.last_seen.items()):
            if now - t > self.ttl:
                # FADED – only for Red-classified
                obj = self.objects.get(oid)
                if obj and oid not in self._faded_announced:
                    self._announce_faded(obj)
                self.remove(oid)

    def get_air(self):
        return [o for o in self.objects.values()
                if "Air" in str(getattr(o, "Type","")) and hasattr(o, "T")]

    def find_bull(self):
        for key in ["Allies","Blue","ROK","NATO","Training"]:
            oid = self.bullseye_ids_by_coal.get(key)
            if oid and oid in self.objects:
                return self.objects[oid]
        for oid in self.bullseye_ids_by_coal.values():
            if oid in self.objects:
                return self.objects[oid]
        # fallback: any Navaid+Static for a stable reference
        for o in self.objects.values():
            if str(getattr(o, "Type","")).startswith("Navaid+Static"):
                return o
        return None

    def _announce_faded(self, obj):
        bull = self.find_bull()
        if not bull: 
            return
        bxy = get_xy_mode(bull)
        oxy = get_xy_mode(obj)
        if not bxy or not oxy or bxy[0] != oxy[0]:
            return
        _, bu, bv = bxy
        _, ou, ov = oxy
        brg, rng = bulls_to_contact(bu, bv, ou, ov, mode=bxy[0])
        hdg = norm_head(getattr(obj.T, "Heading", None))
        track_dir = dir_word_from_heading(hdg) or ""
        ident = classify_id(obj)
        print(f"{ident.upper()} FADED, last seen BULLS {brg:03d} for {int(round(rng))}, track {track_dir}.")
        self._faded_announced.add(obj.object_id)

# --- Grouping & summarizing ---------------------------------------------------
def group_contacts(contacts, rng_nm=5.0, alt_ft=2000.0, mode="uv"):
    """
    Single-link clustering: within thresholds of ANY member -> joins the group.
    """
    items = []
    for o in contacts:
        xy = get_xy_mode(o)
        if xy is None or xy[0] != mode:
            continue
        _, u, v = xy
        alt_m = getattr(o.T, "Altitude", None)
        items.append({"o": o, "u": u, "v": v, "alt_ft": alt_m*M_TO_FT if alt_m is not None else None})

    groups, used = [], set()

    def close(a, b):
        dr_nm = math.hypot(a["u"]-b["u"], a["v"]-b["v"]) / EARTH_M_PER_NM
        dz_ft = abs((a["alt_ft"] or 0) - (b["alt_ft"] or 0))
        return dr_nm <= rng_nm and dz_ft <= alt_ft

    for i in range(len(items)):
        if i in used: continue
        cluster = {i}
        changed = True
        while changed:
            changed = False
            for j in range(len(items)):
                if j in cluster: continue
                if any(close(items[j], items[k]) for k in list(cluster)):
                    cluster.add(j); changed = True
        used.update(cluster)
        groups.append([items[k] for k in sorted(cluster)])
    return groups

def summarize_groups(groups, bull, mode, roe_weapons_free=False):
    bxy = get_xy_mode(bull)
    if not bxy or bxy[0] != mode:
        return []
    _, bu, bv = bxy

    summaries = []
    for g in groups:
        size = len(g)
        # centroid
        cu = sum(it["u"] for it in g)/size
        cv = sum(it["v"] for it in g)/size
        # range & bearing from bull
        brg, rng_nm = bulls_to_contact(bu, bv, cu, cv, mode=mode)
        # alt band
        alts = [it["alt_ft"] for it in g if it["alt_ft"] is not None]
        lo_ang = hi_ang = None
        if alts:
            lo_ang = int(round(min(alts)/1000.0))
            hi_ang = int(round(max(alts)/1000.0))
        # ID: majority vote over members
        id_counts = defaultdict(int)
        hdgs = []
        for it in g:
            ident = classify_id(it["o"], roe_weapons_free=roe_weapons_free)
            id_counts[ident] += 1
            h = norm_head(getattr(it["o"].T, "Heading", None))
            if h is not None: hdgs.append(h)
        ident = max(id_counts.items(), key=lambda kv: kv[1])[0] if id_counts else "Bogey"
        avg_hdg = sum(hdgs)/len(hdgs) if hdgs else None
        aspect = aspect_word(avg_hdg, brg)

        summaries.append({
            "size": size,
            "brg": brg,
            "rng_nm": rng_nm,
            "lo_ang": lo_ang, "hi_ang": hi_ang,
            "aspect": aspect,
            "id": ident
        })
    summaries.sort(key=lambda s: s["rng_nm"])
    return summaries

def size_word(n):
    if n <= 1: return "single"
    if n == 2: return "two-ship"
    if n == 3: return "three-ship"
    if n == 4: return "four-ship"
    if n == 5: return "five-ship"
    return f"{n}-ship"

def format_picture(summaries):
    # Filter to things of interest: not Friendly
    interest = [s for s in summaries if s["id"] in ("Bogey","Bandit","Hostile")]
    if not interest:
        return "PICTURE: CLEAN."

    n = len(interest)
    lead = "single group" if n==1 else ("two groups" if n==2 else ( "three groups" if n==3 else f"{n} groups"))
    parts = [f"PICTURE: {lead}. "]
    labels = ["Nearest group", "Second group", "Third group", "Fourth group", "Fifth group"]

    for i, g in enumerate(interest[:5]):
        sz = size_word(g["size"])
        # 'HEAVY' when 3+
        heavy = " HEAVY" if g["size"] >= 3 else ""
        if g["lo_ang"] is not None and g["hi_ang"] is not None:
            ang_text = f"angels {g['lo_ang']}" if g["lo_ang"]==g["hi_ang"] else f"angels {g['lo_ang']}-{g['hi_ang']}"
        else:
            ang_text = "angels UNK"
        parts.append(f"{labels[i]} BULLS {g['brg']:03d} for {int(round(g['rng_nm']))}, {sz}{heavy}, {g['id'].lower()}, {ang_text}, {g['aspect']}. ")
    return "".join(parts).strip()

def nearest_blue(bull, blue_tracks, mode):
    # Return one representative Blue (closest to bull) for optional BRAA baseline
    if not blue_tracks or not bull: return None
    bxy = get_xy_mode(bull)
    if not bxy or bxy[0] != mode: return None
    _, bu, bv = bxy
    best, best_rng = None, 1e12
    for o in blue_tracks:
        oxy = get_xy_mode(o)
        if not oxy or oxy[0] != mode: continue
        _, ou, ov = oxy
        _, rng = bulls_to_contact(bu, bv, ou, ov, mode=mode)
        if rng < best_rng:
            best_rng, best = rng, o
    return best

def find_callsign(objects, callsign_substr):
    callsign_substr = (callsign_substr or "").lower()
    for o in objects:
        name = (getattr(o, "Name", None) or "").lower()
        if callsign_substr and callsign_substr in name:
            return o
    return None

def format_braa_line(target_summary, from_obj, bull, mode):
    if not target_summary or not from_obj:
        return None
    fxy = get_xy_mode(from_obj)
    t_br = target_summary["brg"]; t_rng = target_summary["rng_nm"]
    if not fxy or fxy[0] != mode:
        return None
    # compute BRAA from friend to target centroid
    bxy = get_xy_mode(bull)
    _, fu, fv = fxy
    if not bxy: return None
    # We need the target centroid in same coords. We can't reconstruct cu/cv from summary.
    # Approximate: using bull + (bearing/range) to derive a point – sufficient for voice guidance.
    brg_rad = math.radians(t_br)
    if mode == "uv":
        du = math.sin(brg_rad) * (t_rng * EARTH_M_PER_NM)
        dv = math.cos(brg_rad) * (t_rng * EARTH_M_PER_NM)
        # target position relative to bull:
        bu, bv = get_xy_mode(bull)[1:]
        tu, tv = bu + du, bv + dv
        braa_brg, braa_rng = bulls_to_contact(fu, fv, tu, tv, mode=mode)
    else:
        # Very rough: treat nm as degrees (1 deg ~ 60 nm) with orientation by bearing
        bu, bv = get_xy_mode(bull)[1:]
        d_nm = t_rng
        dx_nm = math.sin(brg_rad) * d_nm
        dy_nm = math.cos(brg_rad) * d_nm
        tu = bu + (dx_nm/60.0)
        tv = bv + (dy_nm/60.0)
        braa_brg, braa_rng = bulls_to_contact(fu, fv, tu, tv, mode=mode, latref=bv)
    # altitude/aspect from summary
    if target_summary["lo_ang"] is not None and target_summary["hi_ang"] is not None:
        ang_text = f"angels {target_summary['lo_ang']}" if target_summary["lo_ang"]==target_summary["hi_ang"] else f"angels {target_summary['lo_ang']}-{target_summary['hi_ang']}"
    else:
        ang_text = "angels UNK"
    return f"BRAA {braa_brg:03d}/{int(round(braa_rng))}, {ang_text}, {target_summary['aspect']}."

def check_merged(enemies, blues, thresh_nm, mode):
    if not enemies or not blues: return False, None
    # if any pair is < thresh_nm, return True with a text
    for e in enemies:
        exy = get_xy_mode(e)
        if not exy or exy[0] != mode: continue
        _, eu, ev = exy
        e_alt = getattr(e.T, "Altitude", None)
        for b in blues:
            bxy = get_xy_mode(b)
            if not bxy or bxy[0] != mode: continue
            _, bu, bv = bxy
            dist_nm = math.hypot(eu-bu, ev-bv) / EARTH_M_PER_NM
            if dist_nm <= thresh_nm:
                ang = angels_from_meters(e_alt)
                return True, f"MERGED, {('angels '+str(ang)) if ang is not None else 'altitude unknown'}."
    return False, None

# --- Main loop ----------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--password", default="0")
    ap.add_argument("--grp_range_nm", type=float, default=5.0, help="Group clustering radius (nm)")
    ap.add_argument("--grp_alt_ft", type=float, default=2000.0, help="Group vertical window (ft)")
    ap.add_argument("--ttl", type=float, default=10.0, help="Track stale timeout (s)")
    ap.add_argument("--hz", type=float, default=1.0, help="Output rate (Hz)")
    ap.add_argument("--weapons_free", action="store_true", help="Upgrade Bandit -> HOSTILE in ID")
    ap.add_argument("--braa_callsign", type=str, default=None, help="If set, also provide BRAA from this friendly callsign")
    ap.add_argument("--merged_nm", type=float, default=3.0, help="MERGED threshold (nm)")
    ap.add_argument("--merged_cooldown", type=int, default=10, help="Seconds between MERGED announcements")
    args = ap.parse_args()

    ws = WorldState(ttl=args.ttl)
    parser = ACMIFileParser()

    sock = connect_and_handshake(args.host, args.port, args.password)
    print("[connected] AWACS brevity picture; Ctrl+C to stop")

    try:
        for raw in iter_lines(sock):
            ent = parser.parse_line(raw)
            if ent is None: 
                continue

            if ent.action == ACTION_TIME:
                ws.time = ent.timestamp or ws.time
                # speeds
                for tr in ws.tracks.values():
                    tr.update_speed(ws.time)
                # cull & faded
                ws.cull_stale(ws.time)

                # emit on cadence
                tick = int(ws.time * args.hz)
                if tick != ws._last_emit_tick:
                    ws._last_emit_tick = tick

                    bull = ws.find_bull()
                    bmode = get_xy_mode(bull)[0] if bull and get_xy_mode(bull) else "uv"

                    # Partition contacts
                    air = ws.get_air()
                    blues = [o for o in air if coalition_hint(getattr(o,"Coalition",""))=="Blue"]
                    reds  = [o for o in air if coalition_hint(getattr(o,"Coalition",""))=="Red"]
                    unk   = [o for o in air if coalition_hint(getattr(o,"Coalition",""))=="Unknown"]

                    # Groups of interest: anything not Friendly (Red + Unknown)
                    interest = reds + unk
                    if bull and interest:
                        groups = group_contacts(interest, rng_nm=args.grp_range_nm, alt_ft=args.grp_alt_ft, mode=bmode)
                        sums = summarize_groups(groups, bull, mode=bmode, roe_weapons_free=args.weapons_free)
                        pic = format_picture(sums)
                    elif not interest:
                        pic = "PICTURE: CLEAN."
                    else:
                        pic = "PICTURE: no bullseye available yet."

                    # MERGED (rate limited)
                    merged, merged_line = check_merged(reds, blues, args.merged_nm, bmode)
                    if merged and tick - ws._last_merged_tick >= int(args.hz*args.merged_cooldown):
                        print(merged_line)
                        ws._last_merged_tick = tick

                    # Optional BRAA from a friendly (closest to bull or by callsign)
                    braa_line = None
                    if args.braa_callsign:
                        fr = find_callsign(blues, args.braa_callsign)
                        if not fr:
                            fr = nearest_blue(bull, blues, bmode)
                        if fr and bull and interest and pic != "PICTURE: CLEAN.":
                            # BRAA to the nearest group (first in sorted list)
                            nearest = None
                            if bull and interest:
                                nearest = sums[0] if sums else None
                            braa_line = format_braa_line(nearest, fr, bull, bmode)

                    # De-spam identical picture lines
                    to_print = pic
                    if braa_line:
                        to_print = f"{pic} {braa_line}"
                    if to_print != ws._last_picture_line:
                        print(to_print)
                        ws._last_picture_line = to_print

                continue

            if ent.action == ACTION_GLOBAL and isinstance(ent, ACMIObject):
                ws.global_props.update(ent.properties)
                continue

            if ent.action == ACTION_REMOVE:
                ws.remove(ent.object_id)
                continue

            if ent.action == ACTION_UPDATE and isinstance(ent, ACMIObject):
                T = getattr(ent, "T", None)
                if T and hasattr(T, "Heading"):
                    T.Heading = norm_head(T.Heading)
                ws.upsert(ent, ws.time)

    except KeyboardInterrupt:
        pass
    finally:
        try: sock.close()
        except: pass

if __name__ == "__main__":
    main()
