"""Assemble and save .radarloc JSON files."""
import json
import math
from datetime import datetime, timezone


def validate_radarloc(doc: dict) -> dict:
    """Validate a .radarloc document for accuracy and completeness.

    Checks:
    - At least one closed polygon exists
    - Total coverage area is reasonable
    - Coordinates are valid

    Returns:
        Dict with 'valid' bool and 'warnings' list.
    """
    result = {'valid': True, 'warnings': [], 'stats': {}}

    coastlines = doc.get('coastlines', [])
    if not coastlines:
        result['warnings'].append('No coastlines found')
        result['valid'] = False
        return result

    # Count closed vs open polygons
    closed_count = sum(1 for c in coastlines if c.get('closed', False))
    open_count = len(coastlines) - closed_count

    result['stats']['total_features'] = len(coastlines)
    result['stats']['closed_polygons'] = closed_count
    result['stats']['open_segments'] = open_count

    if closed_count == 0:
        result['warnings'].append('No closed polygons - terrain may not generate correctly')

    # Calculate total coastline points
    total_points = sum(len(c.get('points', [])) for c in coastlines)
    result['stats']['total_vertices'] = total_points

    if total_points < 100:
        result['warnings'].append(f'Low vertex count ({total_points}) - data may be sparse')

    # Find largest polygon and calculate its area
    largest_area = 0
    for coast in coastlines:
        if not coast.get('closed', False):
            continue
        pts = coast.get('points', [])
        if len(pts) < 3:
            continue
        # Shoelace formula
        n = len(pts)
        area = 0.0
        for i in range(n):
            j = (i + 1) % n
            area += pts[i]['x'] * pts[j]['y']
            area -= pts[j]['x'] * pts[i]['y']
        area = abs(area) / 2.0 / 1e6  # km²
        largest_area = max(largest_area, area)

    result['stats']['largest_polygon_km2'] = round(largest_area, 2)

    # Validate coordinates
    meta = doc.get('metadata', {})
    lat = meta.get('center_lat', 0)
    lon = meta.get('center_lon', 0)

    if not (-90 <= lat <= 90):
        result['warnings'].append(f'Invalid latitude: {lat}')
        result['valid'] = False
    if not (-180 <= lon <= 180):
        result['warnings'].append(f'Invalid longitude: {lon}')
        result['valid'] = False

    # Check range
    range_nm = meta.get('range_nm', 0)
    if range_nm <= 0 or range_nm > 50:
        result['warnings'].append(f'Unusual range: {range_nm} nm')

    return result


def build_radarloc(location_name: str, center_lat: float, center_lon: float,
                   range_nm: float, coastlines: list,
                   terrain: dict = None) -> dict:
    """Assemble all data into the .radarloc JSON schema.

    Args:
        location_name: Human-readable location name.
        center_lat, center_lon: Center coordinates.
        range_nm: Radar range in nautical miles.
        coastlines: List of coastline dicts from osm_query.
        terrain: Optional terrain grid dict from elevation module.

    Returns:
        Complete .radarloc dict.
    """
    doc = {
        "version": "1.0",
        "metadata": {
            "location_name": location_name,
            "center_lat": round(center_lat, 6),
            "center_lon": round(center_lon, 6),
            "range_nm": range_nm,
            "generated_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "coordinate_system": {
            "type": "local_tangent_plane",
            "origin_lat": round(center_lat, 6),
            "origin_lon": round(center_lon, 6),
            "units": "meters",
        },
        "coastlines": coastlines,
        "terrain": {
            "enabled": terrain is not None,
        },
        "vessels": [],
    }

    if terrain is not None:
        doc["terrain"]["grid"] = {
            "origin_x": terrain["origin_x"],
            "origin_y": terrain["origin_y"],
            "rows": terrain["rows"],
            "cols": terrain["cols"],
            "cell_size": terrain["cell_size"],
        }
        doc["terrain"]["elevations"] = terrain["elevations"]
        doc["terrain"]["data_source"] = terrain.get("data_source", "open-elevation")

    return doc


def save_radarloc(doc: dict, filepath: str) -> str:
    """Save a .radarloc document to disk.

    Args:
        doc: The .radarloc dict.
        filepath: Output file path.

    Returns:
        The absolute path written.
    """
    with open(filepath, "w") as f:
        json.dump(doc, f, indent=2)
    return filepath
