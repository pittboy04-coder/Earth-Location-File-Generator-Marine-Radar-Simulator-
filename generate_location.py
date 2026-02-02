#!/usr/bin/env python3
"""CLI entry point for generating .radarloc files from real-world locations."""
import argparse
import re
import sys

from radarloc_generator.geocoding import geocode
from radarloc_generator.coordinate_transform import nm_to_meters
from radarloc_generator.osm_query import query_water_features
from radarloc_generator.elevation import query_elevation_grid
from radarloc_generator.radarloc_builder import build_radarloc, save_radarloc


def parse_coordinates(text: str):
    """Try to parse 'lat,lon' from text. Returns (lat, lon) or None."""
    m = re.match(r"^\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*$", text)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


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

    # Query coastlines/water features
    print(f"Querying water features (radius {range_m:.0f}m)...")
    try:
        coastlines = query_water_features(lat, lon, range_m)
    except Exception as e:
        print(f"Warning: OSM query failed: {e}", file=sys.stderr)
        coastlines = []
    print(f"Found {len(coastlines)} coastline/water features")

    # Query terrain if requested
    terrain = None
    if args.terrain:
        print(f"Querying elevation data ({args.terrain_grid}x{args.terrain_grid} grid)...")
        try:
            terrain = query_elevation_grid(lat, lon, range_m, args.terrain_grid)
        except Exception as e:
            print(f"Warning: Elevation query failed: {e}", file=sys.stderr)

    # Build and save
    doc = build_radarloc(location_name, lat, lon, args.range, coastlines, terrain)

    output = args.output
    if not output:
        safe_name = re.sub(r"[^\w\-]", "_", args.location.split(",")[0].strip().lower())
        output = f"{safe_name}.radarloc"

    save_radarloc(doc, output)
    print(f"Saved: {output}")

    # Summary
    total_points = sum(len(c["points"]) for c in coastlines)
    print(f"  Coastlines: {len(coastlines)} features, {total_points} points")
    if terrain:
        print(f"  Terrain: {terrain['rows']}x{terrain['cols']} grid, "
              f"cell size {terrain['cell_size']:.1f}m")


if __name__ == "__main__":
    main()
