#!/usr/bin/env python3
"""CLI entry point for generating .radarloc files from real-world locations.

Supports --maritime flag to auto-reposition the radar center near the nearest
coastline, ensuring features fall within the DRS4DNXT's short range (0.125 NM).
"""
import argparse
import math
import re
import sys

from radarloc_generator.geocoding import geocode
from radarloc_generator.coordinate_transform import latlon_to_xy, xy_to_latlon, nm_to_meters
from radarloc_generator.osm_query import query_water_features
from radarloc_generator.elevation import query_elevation_grid
from radarloc_generator.radarloc_builder import build_radarloc, save_radarloc, validate_radarloc


def parse_coordinates(text: str):
    """Try to parse 'lat,lon' from text. Returns (lat, lon) or None."""
    m = re.match(r"^\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*$", text)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


def find_nearest_coastline_point(coastlines, center_lat, center_lon):
    """Find the nearest coastline vertex to the center.

    Args:
        coastlines: List of feature dicts from query_water_features()
        center_lat, center_lon: Current center coordinates

    Returns:
        (nearest_x, nearest_y, nearest_dist, feature_name) in local meters,
        or None if no coastlines.
    """
    nearest = None
    min_dist = float('inf')
    feat_name = ""

    for feat in coastlines:
        pts = feat.get("points", [])
        name = feat.get("name", "")
        for p in pts:
            if isinstance(p, dict):
                x, y = p["x"], p["y"]
            else:
                x, y = p[0], p[1]
            d = math.sqrt(x * x + y * y)
            if d < min_dist:
                min_dist = d
                nearest = (x, y)
                feat_name = name

    if nearest is None:
        return None
    return nearest[0], nearest[1], min_dist, feat_name


def reposition_near_coastline(coastlines, center_lat, center_lon,
                               target_range_m, coast_fraction=0.6):
    """Reposition radar center near the nearest coastline.

    Moves the center so the nearest coastline is at ~coast_fraction of the
    target range. This ensures coastline features fall within the radar's
    actual operational range.

    Args:
        coastlines: List of feature dicts from query_water_features()
        center_lat, center_lon: Original center coordinates
        target_range_m: The radar's actual range (e.g., 231.5m for DRS4DNXT)
        coast_fraction: Place coastline at this fraction of range (default 0.6)

    Returns:
        (new_lat, new_lon, offset_x, offset_y, nearest_dist) or
        (center_lat, center_lon, 0, 0, 0) if no repositioning needed.
    """
    result = find_nearest_coastline_point(coastlines, center_lat, center_lon)
    if result is None:
        return center_lat, center_lon, 0.0, 0.0, 0.0

    nx, ny, nearest_dist, feat_name = result

    # If already within range, no move needed
    if nearest_dist <= target_range_m * 0.8:
        print(f"  Nearest coastline ({feat_name}) at {nearest_dist:.0f}m "
              f"-- already within range")
        return center_lat, center_lon, 0.0, 0.0, nearest_dist

    # Move toward nearest coastline, stopping coast_fraction * range short
    target_coast_dist = target_range_m * coast_fraction
    move_dist = nearest_dist - target_coast_dist

    # Direction from center to nearest point
    dx = nx / nearest_dist
    dy = ny / nearest_dist
    offset_x = dx * move_dist
    offset_y = dy * move_dist

    new_lat, new_lon = xy_to_latlon(offset_x, offset_y, center_lat, center_lon)

    print(f"  Nearest coastline ({feat_name}) at {nearest_dist:.0f}m from center")
    print(f"  Repositioning {move_dist:.0f}m toward coastline")
    print(f"  New center: ({new_lat:.6f}, {new_lon:.6f})")
    print(f"  Coastline will be ~{target_coast_dist:.0f}m from radar")

    return new_lat, new_lon, offset_x, offset_y, nearest_dist


def main():
    parser = argparse.ArgumentParser(
        description="Generate .radarloc files from real-world locations.")
    parser.add_argument("location",
                        help="Location name (e.g. 'Lake Murray, SC') or lat,lon coordinates")
    parser.add_argument("--range", type=float, default=6.0,
                        help="Radar range in nautical miles (default: 6)")
    parser.add_argument("--terrain", action="store_true",
                        help="Include elevation/terrain data (slower)")
    parser.add_argument("--terrain-grid", type=int, default=128,
                        help="Terrain grid size (default: 128)")
    parser.add_argument("-o", "--output", default=None,
                        help="Output filename (default: <location>.radarloc)")
    parser.add_argument("--maritime", action="store_true",
                        help="Auto-reposition near nearest coastline for DRS4DNXT "
                             "short-range radar. Queries at wide range, finds "
                             "nearest shore, re-centers, then re-queries at "
                             "tight range so features fit within radar view.")
    parser.add_argument("--radar-range", type=float, default=0.125,
                        help="Actual radar range in NM for --maritime repositioning "
                             "(default: 0.125 NM = 231.5m, DRS4DNXT)")
    args = parser.parse_args()

    # Resolve location
    coords = parse_coordinates(args.location)
    if coords:
        lat, lon = coords
        location_name = f"{lat:.4f}, {lon:.4f}"
        print(f"Using coordinates: {lat}, {lon}")
    else:
        print(f"Geocoding '{args.location}'...")
        try:
            result = geocode(args.location)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        lat, lon = result["lat"], result["lon"]
        location_name = result["display_name"]
        print(f"Found: {location_name} ({lat:.4f}, {lon:.4f})")

    range_m = nm_to_meters(args.range)

    # Query coastlines/water features (wide range first)
    print(f"Querying water features (radius {range_m:.0f}m)...")
    try:
        coastlines = query_water_features(lat, lon, range_m)
    except Exception as e:
        print(f"Warning: OSM query failed: {e}", file=sys.stderr)
        coastlines = []
    print(f"Found {len(coastlines)} coastline/water features")

    # Maritime auto-reposition: move center near coastline, re-query tighter
    final_range_nm = args.range
    if args.maritime and coastlines:
        radar_range_m = nm_to_meters(args.radar_range)
        # Use 3x the radar range as the capture radius (enough context
        # around the radar position, but tight enough for features to be
        # within the actual radar view after Mode 12 repositioning)
        capture_range_m = radar_range_m * 3.0
        capture_range_nm = args.radar_range * 3.0

        print(f"\n-- Maritime auto-positioning --")
        print(f"  Radar range: {radar_range_m:.1f}m ({args.radar_range} NM)")
        print(f"  Capture range: {capture_range_m:.1f}m ({capture_range_nm:.3f} NM)")

        new_lat, new_lon, off_x, off_y, nearest_dist = reposition_near_coastline(
            coastlines, lat, lon, radar_range_m, coast_fraction=0.6)

        if off_x != 0 or off_y != 0:
            # Re-query at the new center with tighter range
            print(f"  Re-querying water features at new center "
                  f"(radius {capture_range_m:.0f}m)...")
            try:
                coastlines = query_water_features(
                    new_lat, new_lon, capture_range_m,
                    simplify_epsilon=2.0)  # Fine detail for short range
            except Exception as e:
                print(f"  Warning: Re-query failed: {e}", file=sys.stderr)
                # Fall back to shifting the original features
                for feat in coastlines:
                    pts = feat.get("points", [])
                    feat["points"] = [
                        {"x": round(p["x"] - off_x, 1),
                         "y": round(p["y"] - off_y, 1)}
                        if isinstance(p, dict) else
                        {"x": round(p[0] - off_x, 1),
                         "y": round(p[1] - off_y, 1)}
                        for p in pts
                    ]

            lat, lon = new_lat, new_lon
            final_range_nm = capture_range_nm
            location_name = f"{location_name} (maritime-repositioned)"
            print(f"  Final features: {len(coastlines)}")
        else:
            print(f"  No repositioning needed -- coastline already nearby")
            # Still tighten the range for Mode 12 compatibility
            final_range_nm = capture_range_nm

    elif args.maritime and not coastlines:
        print("\nWARNING: --maritime flag set but no coastlines found at this "
              "location. Try a larger --range or different coordinates.")

    # Query terrain if requested
    terrain = None
    if args.terrain:
        print(f"Querying elevation data ({args.terrain_grid}x{args.terrain_grid} grid)...")
        try:
            terrain = query_elevation_grid(lat, lon,
                                           nm_to_meters(final_range_nm),
                                           args.terrain_grid)
        except Exception as e:
            print(f"Warning: Elevation query failed: {e}", file=sys.stderr)

    # Build and save
    doc = build_radarloc(location_name, lat, lon, final_range_nm, coastlines, terrain)

    output = args.output
    if not output:
        safe_name = re.sub(r"[^\w\-]", "_", args.location.split(",")[0].strip().lower())
        if args.maritime:
            safe_name += "_maritime"
        output = f"{safe_name}.radarloc"
    elif not output.lower().endswith('.radarloc'):
        output = f"{output}.radarloc"

    # Validate before saving
    validation = validate_radarloc(doc)

    save_radarloc(doc, output)
    print(f"\nSaved: {output}")

    # Summary with quality metrics
    stats = validation['stats']
    print(f"  Features: {stats.get('total_features', 0)} "
          f"({stats.get('closed_polygons', 0)} closed, {stats.get('open_segments', 0)} open)")
    print(f"  Vertices: {stats.get('total_vertices', 0)}")
    print(f"  Center: ({lat:.6f}, {lon:.6f})")
    print(f"  Range: {final_range_nm:.4f} NM ({nm_to_meters(final_range_nm):.1f}m)")
    if stats.get('largest_polygon_km2', 0) > 0:
        print(f"  Largest polygon: {stats['largest_polygon_km2']:.1f} km2")

    if terrain:
        print(f"  Terrain: {terrain['rows']}x{terrain['cols']} grid, "
              f"cell size {terrain['cell_size']:.1f}m")

    # Quality warnings
    if validation['warnings']:
        print()
        print("Quality warnings:")
        for w in validation['warnings']:
            print(f"  ! {w}")

    # Final status
    if validation['valid'] and not validation['warnings']:
        print(f"\nData quality: GOOD (ready for simulation)")
    elif validation['valid']:
        print(f"\nData quality: ACCEPTABLE (check warnings)")
    else:
        print(f"\nData quality: ISSUES DETECTED")

    if args.maritime:
        print(f"\nUsage with Object Creation Mode 12:")
        print(f"  python radar_simulator.py --mode12 --radarloc {output} --interactive")


if __name__ == "__main__":
    main()
