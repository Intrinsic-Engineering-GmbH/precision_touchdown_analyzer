"""Site planning helper for the touchdown analyzer.

Answers: at a given camera distance, how much of the landing strip is in frame,
how many pixels per metre do we get, and what does that mean for the error budget?

Defaults are the AXIS P1485-LE (1/2.8" CMOS, 1920x1080, 10.8-28.2 mm,
HFOV 29-11 deg, VFOV 17-6 deg, max 50/60 fps).

    python tools/site_geometry.py --distance 75 --mast 8
"""

from __future__ import annotations

import argparse
import math

W_PX, H_PX = 1920, 1080
HFOV_WIDE, HFOV_TELE = 29.0, 11.0  # degrees
VFOV_WIDE = 17.0  # degrees
TOUCHDOWN_SPEED = 25.0  # m/s, ~90 km/h
GLIDER_LEN = 7.0  # m


def covered_width(distance_m: float, hfov_deg: float) -> float:
    return 2 * distance_m * math.tan(math.radians(hfov_deg) / 2)


def distance_for_range(half_range_m: float, hfov_deg: float, margin: float = 1.1) -> float:
    needed = 2 * (half_range_m + GLIDER_LEN) * margin
    return (needed / 2) / math.tan(math.radians(hfov_deg) / 2)


def report(distance: float, mast: float, fps: float, hfov: float) -> None:
    width = covered_width(distance, hfov)
    px_per_m = W_PX / width
    px_v = math.radians(VFOV_WIDE) / H_PX
    elev = math.degrees(math.atan(mast / distance))
    depth_per_px = px_v * distance / math.sin(math.radians(elev))
    half = width / 2
    lon_from_1px_v = depth_per_px * half / distance

    print(
        f"Camera {distance:.0f} m from the strip, mast {mast:.1f} m, {fps:.0f} fps, HFOV {hfov:.0f} deg"
    )
    print(f"  covered width        {width:6.1f} m   (+/-{half:.1f} m around the target line)")
    print(f"  scale at the line    {px_per_m:6.1f} px/m ({1000 / px_per_m:.1f} mm/px)")
    print(f"  elevation angle      {elev:6.1f} deg")
    print(f"  cross-strip (depth)  {depth_per_px:6.2f} m per vertical pixel")
    print(
        f"  longitudinal error from 1 px vertical error at the frame edge: {lon_from_1px_v:.2f} m"
    )
    print(
        f"  travel per frame     {TOUCHDOWN_SPEED / fps:6.2f} m  (sub-frame fit ~{TOUCHDOWN_SPEED / fps / 5:.2f} m)"
    )
    for shutter in (500, 1000, 2000):
        blur_mm = TOUCHDOWN_SPEED / shutter * 1000
        print(
            f"  motion blur 1/{shutter:<5} {blur_mm:6.1f} mm = {blur_mm * px_per_m / 1000:.1f} px"
        )
    for char_cm in (20, 30, 50):
        print(
            f"  OCR: {char_cm} cm character = {char_cm / 100 * px_per_m:.0f} px "
            f"({'ok' if char_cm / 100 * px_per_m >= 22 else 'too small'})"
        )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--distance", type=float, default=75.0, help="camera distance from the strip [m]"
    )
    ap.add_argument("--mast", type=float, default=8.0, help="camera height above ground [m]")
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--hfov", type=float, default=HFOV_WIDE, help="horizontal field of view [deg]")
    args = ap.parse_args()

    report(args.distance, args.mast, args.fps, args.hfov)
    print("\nDistance needed for a given touchdown range (glider length + 10% margin):")
    for half in (15, 20, 25):
        print(f"  +/-{half:2} m -> camera at {distance_for_range(half, args.hfov):5.0f} m")
    print("\nTele end (28.2 mm, HFOV 11 deg) for a dedicated identification camera:")
    tele_w = covered_width(args.distance, HFOV_TELE)
    print(
        f"  covers {tele_w:.1f} m at {args.distance:.0f} m, {W_PX / tele_w:.0f} px/m "
        f"-> 30 cm character = {0.3 * W_PX / tele_w:.0f} px"
    )


if __name__ == "__main__":
    main()
