# awacs_stage2_picture.py
import argparse
from math import atan2, degrees, hypot
from ttv_raw_listener import connect_and_handshake, iter_lines
from acmi_parse import (
    ACMIFileParser,
    ACTION_TIME, ACTION_GLOBAL, ACTION_REMOVE, ACTION_UPDATE, ACMIObject
)

EARTH_M_PER_NM = 1852.0
M_TO_FT = 3.280839895

BLUE_HINTS = {"Allies","Blue","NATO","USA","USAF","ROK","Training","Allied"}
RED_HINTS  = {"Enemies","Red","OPFOR","Aggressor","DPRK","Russia","China","Serbia","Hostile"}

def norm_head(h):
    if h is None: return None
    # keep in [0,360)
    return (h % 360 + 360) % 360

def bearing_deg(dx, dy):
    # 0° = North, clockwise positive
    ang = degrees(atan2(dx, dy))
    return (ang + 360.0) % 360.0

def bulls_to_contact(bu, bv, tu, tv, mode="uv", latref=None):
    """
    Compute BULLS bearing/range from (bu,bv) to (tu,tv).
    mode:
      - "uv": U/V in meters -> NM via 1852
      - "ll": lon/lat degrees -> NM via small-angle approximation at latref
    """
    if mode == "uv":
        dx = tu - bu
        dy = tv - bv
        brg = bearing_deg(dx, dy)
        rng_nm = hypot(dx, dy) / EARTH_M_PER_NM
        return int(round(brg)) % 360, round(rng_nm)
    else:  # lon/lat degrees
        if latref is None:
            latref = (bv + tv) / 2.0
        k = 60.0  # nm per degree
        dx_nm = (tu - bu) * k * max(0.01, abs(__import__("math").cos(__import__("math").radians(latref))))
        dy_nm = (tv - bv) * k
        brg = bearing_deg(dx_nm, dy_nm)
        rng_nm = (dx_nm**2 + dy_nm**2) ** 0.5
        return int(round(brg)) % 360, round(rng_nm)

def hot_cold(obj_heading_deg, brg_obj_to_friend_deg):
    if obj_heading_deg is None or brg_obj_to_friend_deg is None:
        return "UNK"
    diff = abs(((obj_heading_deg - brg_obj_to_friend_deg + 540) % 360) - 180)
    if diff < 45: return "HOT"
    if diff > 135: return "COLD"
    return "FLANK"

class WorldState:
    def __init__(self):
        self.time = 0.0
        self.objects = {}                  # id -> ACMIObject
        self.global_props = {}             # header/global settings
        self.bullseye_ids_by_coal = {}     # coalition -> object_id

    def upsert(self, obj: ACMIObject):
        self.objects[obj.object_id] = obj
        t = getattr(obj, "Type", "")
        if "Navaid+Bullseye" in t:
            coal = getattr(obj, "Coalition", None) or "Unknown"
            self.bullseye_ids_by_coal[coal] = obj.object_id

    def remove(self, object_id: str):
        self.objects.pop(object_id, None)

    def get_air(self):
        return [o for o in self.objects.values()
                if "Air" in getattr(o, "Type","") and hasattr(o, "T")]

    def find_bull(self):
        # Prefer clearly blue bull; else any bull; else fall back to a static navaid as “theater bulls”.
        # 1) blue-ish bull
        for key in list(BLUE_HINTS) + ["Allies","Blue","ROK","NATO","Training"]:
            oid = self.bullseye_ids_by_coal.get(key)
            if oid and oid in self.objects:
                return self.objects[oid]
        # 2) any bull
        for oid in self.bullseye_ids_by_coal.values():
            if oid in self.objects:
                return self.objects[oid]
        # 3) fallback: any object whose Type starts with Navaid+Static
        for o in self.objects.values():
            if str(getattr(o, "Type","")).startswith("Navaid+Static"):
                return o
        return None

def coalition_hint(name: str) -> str:
    name = (name or "").strip()
    if any(tag in name for tag in BLUE_HINTS): return "Blue"
    if any(tag in name for tag in RED_HINTS):  return "Red"
    return "Unknown"

def get_xy_mode(obj):
    # Prefer U/V meters; else lon/lat degrees
    T = getattr(obj, "T", None)
    if not T: return None
    u = getattr(T, "U", None); v = getattr(T, "V", None)
    if u not in (None, 0.0) or v not in (None, 0.0):
        return ("uv", u or 0.0, v or 0.0)
    lon = getattr(T, "Longitude", None); lat = getattr(T, "Latitude", None)
    if lon is not None and lat is not None:
        return ("ll", lon, lat)
    return None

def angels_from_meters(m):
    if m is None: return "UNK"
    ft = m * M_TO_FT
    return int(round(ft / 1000.0))

def nearest_enemy(ws: WorldState, bull):
    bluish = set(BLUE_HINTS)
    enemies = []
    for o in ws.get_air():
        coal = coalition_hint(getattr(o, "Coalition",""))
        if coal == "Red":
            enemies.append(o)
    if not enemies:
        return None

    bull_xy = get_xy_mode(bull)
    if bull_xy is None: return None
    mode, bu, bv = bull_xy

    best = None
    best_rng = 1e12
    for e in enemies:
        exy = get_xy_mode(e)
        if exy is None or exy[0] != mode:
            continue
        _, eu, ev = exy
        brg, rng = bulls_to_contact(bu, bv, eu, ev, mode, latref=bv if mode=="ll" else None)
        if rng < best_rng:
            best_rng = rng
            best = (e, brg, rng)
    return best

def run_once_per_second(ws, last_second_printed):
    cur_sec = int(ws.time)
    if cur_sec == last_second_printed:
        return last_second_printed

    bull = ws.find_bull()
    sel = nearest_enemy(ws, bull) if bull else None

    if not bull:
        print("PICTURE: no bullseye available yet.")
    elif not sel:
        print("PICTURE: no hostile groups.")
    else:
        e, brg, rng = sel
        T = e.T
        ang = angels_from_meters(getattr(T, "Altitude", None))
        hdg = norm_head(getattr(T, "Heading", None))
        aspect = hot_cold(hdg, brg)
        print(f"PICTURE: single group BULLS {brg:03d} for {int(rng)}, angels {ang}, {aspect}.")

    return cur_sec

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--password", default="0")
    args = ap.parse_args()

    ws = WorldState()
    parser = ACMIFileParser()
    last_second_printed = -1

    sock = connect_and_handshake(args.host, args.port, args.password)
    print("[connected] AWACS picture; Ctrl+C to stop")
    try:
        for line in iter_lines(sock):
            entry = parser.parse_line(line)
            if entry is None:
                continue

            if entry.action == ACTION_TIME:
                ws.time = entry.timestamp or ws.time
                last_second_printed = run_once_per_second(ws, last_second_printed)
                continue

            if entry.action == ACTION_GLOBAL and isinstance(entry, ACMIObject):
                ws.global_props.update(entry.properties)
                continue

            if entry.action == ACTION_REMOVE:
                ws.remove(entry.object_id)
                continue

            if entry.action == ACTION_UPDATE and isinstance(entry, ACMIObject):
                # normalize a couple fields expected by geometry
                T = getattr(entry, "T", None)
                if T and hasattr(T, "Heading"):
                    T.Heading = norm_head(T.Heading)
                ws.upsert(entry)

    except KeyboardInterrupt:
        pass
    finally:
        try: sock.close()
        except: pass

if __name__ == "__main__":
    main()
