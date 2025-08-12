# geo_utils.py
import math

EARTH_M = 1852.0  # meters per NM

def bearing_deg(dx, dy):
    # 0° = North, increasing clockwise (typical BRAA/BULLS compass)
    ang = math.degrees(math.atan2(dx, dy))
    return (ang + 360.0) % 360.0

def bulls_to_contact(bull_u, bull_v, obj_u, obj_v):
    dx = obj_u - bull_u
    dy = obj_v - bull_v
    brg = bearing_deg(dx, dy)
    rng_nm = math.hypot(dx, dy) / EARTH_M
    return int(round(brg)), round(rng_nm)

def hot_cold(obj_heading_deg, brg_obj_to_friend_deg):
    # return "HOT"/"COLD"/"FLANK"
    diff = abs(((obj_heading_deg - brg_obj_to_friend_deg + 540) % 360) - 180)
    if diff < 45: return "HOT"
    if diff > 135: return "COLD"
    return "FLANK"
