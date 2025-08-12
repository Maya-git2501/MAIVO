# awacs_stage3_groups.py
import argparse, math, time
from ttv_raw_listener import connect_and_handshake, iter_lines
from acmi_parse import (
    ACMIFileParser,
    ACTION_TIME, ACTION_GLOBAL, ACTION_REMOVE, ACTION_UPDATE, ACMIObject
)

EARTH_M_PER_NM = 1852.0
M_TO_FT = 3.280839895
KTS_PER_MPS = 1.943844492

BLUE_HINTS = {"Allies","Blue","NATO","USA","USAF","ROK","Training","Allied","Player"}
RED_HINTS  = {"Enemies","Red","OPFOR","Aggressor","DPRK","Russia","China","Serbia","Hostile"}

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
    # lon/lat fallback (small-angle)
    latref = (bv + tv)/2.0 if latref is None else latref
    dx_nm = (tu - bu) * 60.0 * max(0.01, abs(math.cos(math.radians(latref))))
    dy_nm = (tv - bv) * 60.0
    brg = bearing_deg(dx_nm, dy_nm)
    rng_nm = math.hypot(dx_nm, dy_nm)
    return int(round(brg)) % 360, rng_nm

def hot_cold(obj_heading_deg, brg_obj_to_friend_deg):
    if obj_heading_deg is None or brg_obj_to_friend_deg is None:
        return "UNK"
    diff = abs(((obj_heading_deg - brg_obj_to_friend_deg + 540) % 360) - 180)
    if diff < 45: return "HOT"
    if diff > 135: return "COLD"
    return "FLANK"

def angels_from_meters(m):
    if m is None: return None
    return int(round((m * M_TO_FT) / 1000.0))

def coalition_hint(s: str) -> str:
    s = (s or "").strip()
    if any(tag in s for tag in BLUE_HINTS): return "Blue"
    if any(tag in s for tag in RED_HINTS):  return "Red"
    return "Unknown"

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

class Track:
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
        self.tracks = {}            # id -> Track
        self.bullseye_ids_by_coal = {}
        self.last_seen = {}         # id -> time
        self.ttl = ttl

    def upsert(self, obj: ACMIObject, now):
        oid = obj.object_id
        self.objects[oid] = obj
        self.last_seen[oid] = now
        if oid not in self.tracks:
            self.tracks[oid] = Track(obj)
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
        for o in self.objects.values():
            if str(getattr(o, "Type","")).startswith("Navaid+Static"):
                return o
        return None

def group_contacts(contacts, rng_nm=5.0, alt_ft=2000.0, mode="uv"):
    """
    Single-link clustering: if a contact is within thresholds of ANY member,
    it joins the group (iterative expansion).
    """
    # Build list with handy fields
    items = []
    for o in contacts:
        xy = get_xy_mode(o)
        if xy is None: 
            continue
        if xy[0] != mode:
            continue
        _, u, v = xy
        alt_m = getattr(o.T, "Altitude", None)
        items.append({"o": o, "u": u, "v": v, "alt_ft": alt_m*M_TO_FT if alt_m is not None else None})

    groups = []
    used = set()

    def close(a, b):
        dr_nm = math.hypot(a["u"]-b["u"], a["v"]-b["v"]) / EARTH_M_PER_NM
        dz_ft = (abs((a["alt_ft"] or 0) - (b["alt_ft"] or 0)))
        return dr_nm <= rng_nm and dz_ft <= alt_ft

    for i in range(len(items)):
        if i in used: 
            continue
        # seed a new group
        g_idx = {i}
        changed = True
        while changed:
            changed = False
            for j in range(len(items)):
                if j in g_idx: 
                    continue
                if any(close(items[j], items[k]) for k in list(g_idx)):
                    g_idx.add(j)
                    changed = True
        used.update(g_idx)
        groups.append([items[k] for k in sorted(g_idx)])

    return groups

def summarize_groups(groups, bull, mode="uv"):
    # bull position
    b_xy = get_xy_mode(bull)
    if b_xy is None or b_xy[0] != mode:
        return []
    _, bu, bv = b_xy

    out = []
    for g in groups:
        size = len(g)
        # centroid
        cu = sum(it["u"] for it in g)/size
        cv = sum(it["v"] for it in g)/size
        # alt band
        alts = [it["alt_ft"] for it in g if it["alt_ft"] is not None]
        lo_ang = hi_ang = None
        if alts:
            lo_ang = int(round(min(alts)/1000.0))
            hi_ang = int(round(max(alts)/1000.0))
        # bearing/range from bull
        brg, rng_nm = bulls_to_contact(bu, bv, cu, cv, mode)
        # avg heading for aspect
        hdgs = []
        for it in g:
            T = it["o"].T
            h = getattr(T, "Heading", None)
            if h is not None:
                hdgs.append(norm_head(h))
        avg_hdg = sum(hdgs)/len(hdgs) if hdgs else None
        aspect = hot_cold(avg_hdg, brg)

        out.append({
            "size": size,
            "brg": brg,
            "rng_nm": rng_nm,
            "lo_ang": lo_ang, "hi_ang": hi_ang,
            "aspect": aspect
        })
    # sort by range
    out.sort(key=lambda x: x["rng_nm"])
    return out

def format_picture(summaries):
    if not summaries:
        return "PICTURE: no hostile groups."
    n = len(summaries)
    parts = []
    lead = "PICTURE: " + (["single group","two groups","three groups"][min(n,3)-1] if n <= 3 else f"{n} groups") + ". "
    parts.append(lead)
    labels = ["Nearest group", "Second group", "Third group", "Fourth group", "Fifth group"]

    for i, g in enumerate(summaries[:5]):
        size_word = ["single","two-ship","three-ship","four-ship","five-ship"][min(g["size"],5)-1] if g["size"]<=5 else f"{g['size']}-ship"
        if g["lo_ang"] is not None and g["hi_ang"] is not None:
            if g["lo_ang"] == g["hi_ang"]:
                ang_text = f"angels {g['lo_ang']}"
            else:
                ang_text = f"angels {g['lo_ang']}-{g['hi_ang']}"
        else:
            ang_text = "angels UNK"
        parts.append(f"{labels[i]} BULLS {g['brg']:03d} for {int(round(g['rng_nm']))}, {size_word}, {ang_text}, {g['aspect']}. ")

    return "".join(parts).strip()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--password", default="0")
    ap.add_argument("--grp_range_nm", type=float, default=5.0, help="Group clustering radius (nm)")
    ap.add_argument("--grp_alt_ft", type=float, default=2000.0, help="Group vertical window (ft)")
    ap.add_argument("--ttl", type=float, default=10.0, help="Track stale timeout (s)")
    ap.add_argument("--hz", type=float, default=1.0, help="Output rate (Hz)")
    args = ap.parse_args()

    ws = WorldState(ttl=args.ttl)
    parser = ACMIFileParser()
    last_emit = -1
    last_line = None

    sock = connect_and_handshake(args.host, args.port, args.password)
    print("[connected] AWACS multi-group picture; Ctrl+C to stop")
    try:
        for line in iter_lines(sock):
            entry = parser.parse_line(line)
            if entry is None:
                continue

            if entry.action == ACTION_TIME:
                ws.time = entry.timestamp or ws.time
                # update speeds
                for tr in ws.tracks.values():
                    tr.update_speed(ws.time)
                ws.cull_stale(ws.time)

                # emit at fixed rate
                cur_tick = int(ws.time * args.hz)
                if cur_tick != last_emit:
                    last_emit = cur_tick

                    bull = ws.find_bull()
                    mode = None
                    if bull:
                        bxy = get_xy_mode(bull)
                        if bxy: mode = bxy[0]
                    # choose mode based on bull, else prefer uv
                    mode = mode or "uv"

                    enemies = [o for o in ws.get_air() if coalition_hint(getattr(o, "Coalition",""))=="Red"]
                    if bull and enemies:
                        groups = group_contacts(enemies, rng_nm=args.grp_range_nm, alt_ft=args.grp_alt_ft, mode=mode)
                        summaries = summarize_groups(groups, bull, mode=mode)
                        line = format_picture(summaries)
                    elif not enemies:
                        line = "PICTURE: no hostile groups."
                    else:
                        line = "PICTURE: no bullseye available yet."

                    if line != last_line:
                        print(line)
                        last_line = line
                continue

            if entry.action == ACTION_GLOBAL and isinstance(entry, ACMIObject):
                ws.global_props.update(entry.properties)
                continue

            if entry.action == ACTION_REMOVE:
                ws.remove(entry.object_id)
                continue

            if entry.action == ACTION_UPDATE and isinstance(entry, ACMIObject):
                T = getattr(entry, "T", None)
                if T and hasattr(T, "Heading"):
                    T.Heading = norm_head(T.Heading)
                ws.upsert(entry, ws.time)

    except KeyboardInterrupt:
        pass
    finally:
        try: sock.close()
        except: pass

if __name__ == "__main__":
    main()
