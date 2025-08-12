# awacs_stage1_feed.py
import argparse
from collections import defaultdict
from ttv_raw_listener import connect_and_handshake, iter_lines
from acmi_parse import ACMIFileParser, ACTION_TIME, ACTION_GLOBAL, ACTION_REMOVE, ACTION_UPDATE, ACMIObject

class WorldState:
    def __init__(self):
        self.time = 0.0
        self.global_props = {}     # ref lon/lat, bullseyes, title, etc.
        self.objects = {}          # id -> ACMIObject
        self.bullseye_by_coalition = {}  # "Allies"/"Enemies"/etc -> object_id

    def upsert(self, obj: ACMIObject):
        # Keep the last seen properties per object_id
        self.objects[obj.object_id] = obj

        # Track bullseyes when we see them
        t = getattr(obj, "Type", "")
        if "Navaid+Bullseye" in t:
            coal = getattr(obj, "Coalition", None) or "Unknown"
            self.bullseye_by_coalition[coal] = obj.object_id

    def remove(self, object_id: str):
        self.objects.pop(object_id, None)

    def get_air(self):
        # return only flying things we care about initially
        out = []
        for o in self.objects.values():
            if "Air" in getattr(o, "Type", ""):
                out.append(o)
        return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--password", default="0")
    args = ap.parse_args()

    ws = WorldState()
    parser = ACMIFileParser()

    sock = connect_and_handshake(args.host, args.port, args.password)
    print("[connected] structured feed; Ctrl+C to stop")
    try:
        for line in iter_lines(sock):
            entry = parser.parse_line(line)
            if entry is None:
                continue

            if entry.action == ACTION_TIME:
                ws.time = entry.timestamp or ws.time
                continue

            if entry.action == ACTION_GLOBAL and isinstance(entry, ACMIObject):
                # global settings also arrive as an object with id "global"
                ws.global_props.update(entry.properties)
                continue

            if entry.action == ACTION_REMOVE:
                ws.remove(entry.object_id)
                continue

            if entry.action == ACTION_UPDATE and isinstance(entry, ACMIObject):
                ws.upsert(entry)
                # Log a normalized line for sanity checking
                o = entry
                print(f"{ws.time:8.2f}s  {o.object_id:>8} "
                      f"{getattr(o,'Coalition','?')[:7]:<7} "
                      f"{getattr(o,'Type','?')[:12]:<12} "
                      f"{o.T.Longitude:9.5f},{o.T.Latitude:9.5f} "
                      f"Alt {o.T.Altitude:7.1f}  Hdg {getattr(o,'T').Heading if hasattr(o.T,'Heading') else None}")
    except KeyboardInterrupt:
        pass
    finally:
        try: sock.close()
        except: pass

if __name__ == "__main__":
    main()
