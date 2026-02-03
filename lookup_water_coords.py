#!/usr/bin/env python3
"""Look up water/marine coordinates for a location name.

This tool finds the center point of a water body (lake, bay, reservoir, etc.)
which can then be used with generate_location.py for accurate coastline data.
"""
import sys
from radarloc_generator.osm_query import find_water_coordinates


def main():
    if len(sys.argv) < 2:
        print("Usage: python lookup_water_coords.py <water body name>")
        print()
        print("Examples:")
        print('  python lookup_water_coords.py "Lake Murray"')
        print('  python lookup_water_coords.py "San Francisco Bay"')
        print('  python lookup_water_coords.py "Chesapeake Bay"')
        sys.exit(1)

    location = " ".join(sys.argv[1:])
    print(f"Searching for water body: {location}")
    print()

    result = find_water_coordinates(location)

    if "error" in result:
        print(f"Error: {result['error']}")
        sys.exit(1)

    all_matches = result.get("all_matches", [])

    if not all_matches:
        print("No matches found.")
        sys.exit(1)

    # Sort by area descending
    all_matches = sorted(all_matches, key=lambda x: -x["area_km2"])

    print("=" * 60)
    print(f"  Found {len(all_matches)} water bodies matching '{location}'")
    print("=" * 60)
    print()

    # Show all matches with numbers for selection
    for i, match in enumerate(all_matches[:10], 1):
        lat, lon = match['lat'], match['lon']
        # Determine hemisphere labels
        lat_dir = 'N' if lat >= 0 else 'S'
        lon_dir = 'E' if lon >= 0 else 'W'
        print(f"  {i}. {match['name']}")
        print(f"     Coordinates: {abs(lat):.4f}{lat_dir}, {abs(lon):.4f}{lon_dir}")
        print(f"     Area: ~{match['area_km2']:.1f} km²")
        print()

    print("=" * 60)
    print()
    print("To use a location, copy the coordinates and run:")
    print()

    # Show example with first US location if available, otherwise first match
    us_match = None
    for m in all_matches:
        # Rough check for US coordinates (continental US)
        if 24 < m['lat'] < 50 and -130 < m['lon'] < -65:
            us_match = m
            break

    example = us_match or all_matches[0]
    print(f'  python generate_location.py "{example["lat"]},{example["lon"]}" --range 6')
    print()


if __name__ == "__main__":
    main()
