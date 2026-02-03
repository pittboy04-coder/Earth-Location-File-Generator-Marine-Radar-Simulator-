"""Query Overpass API for water boundaries and coastlines."""
import math
import requests
from .coordinate_transform import latlon_to_xy

OVERPASS_URL = "https://overpass-api.de/api/interpreter"


def _douglas_peucker(points: list, epsilon: float) -> list:
    """Simplify a polyline using the Douglas-Peucker algorithm.

    Args:
        points: List of (x, y) tuples.
        epsilon: Maximum allowed deviation in meters.

    Returns:
        Simplified list of (x, y) tuples.
    """
    if len(points) <= 2:
        return points

    # Find point with max distance from line between first and last
    start, end = points[0], points[-1]
    max_dist = 0.0
    max_idx = 0
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    line_len_sq = dx * dx + dy * dy

    for i in range(1, len(points) - 1):
        px, py = points[i][0] - start[0], points[i][1] - start[1]
        if line_len_sq > 0:
            t = max(0.0, min(1.0, (px * dx + py * dy) / line_len_sq))
            proj_x, proj_y = t * dx, t * dy
        else:
            proj_x, proj_y = 0.0, 0.0
        dist = math.sqrt((px - proj_x) ** 2 + (py - proj_y) ** 2)
        if dist > max_dist:
            max_dist = dist
            max_idx = i

    if max_dist > epsilon:
        left = _douglas_peucker(points[:max_idx + 1], epsilon)
        right = _douglas_peucker(points[max_idx:], epsilon)
        return left[:-1] + right
    else:
        return [start, end]


def _assemble_multipolygon(way_segments: list, center_lat: float, center_lon: float,
                           nodes: dict, simplify_epsilon: float) -> list:
    """Assemble way segments into closed polygons by connecting endpoints.

    OSM multipolygon relations have multiple ways that connect end-to-end
    to form closed rings. This function joins them together.

    Returns list of (points, closed) tuples.
    """
    if not way_segments:
        return []

    # Convert each way segment to points with start/end node IDs
    segments = []
    for way_id, way_nodes in way_segments:
        if len(way_nodes) < 2:
            continue
        points = []
        for nid in way_nodes:
            if nid in nodes:
                lat, lon = nodes[nid]
                x, y = latlon_to_xy(lat, lon, center_lat, center_lon)
                points.append((x, y))
        if len(points) >= 2:
            segments.append({
                'start_node': way_nodes[0],
                'end_node': way_nodes[-1],
                'points': points
            })

    if not segments:
        return []

    # Greedily assemble segments into rings
    assembled = []
    used = set()

    while len(used) < len(segments):
        # Start a new ring with first unused segment
        ring_points = []
        for i, seg in enumerate(segments):
            if i not in used:
                ring_points = list(seg['points'])
                current_end = seg['end_node']
                start_node = seg['start_node']
                used.add(i)
                break

        # Try to extend the ring by finding connecting segments
        max_iterations = len(segments) * 2
        for _ in range(max_iterations):
            found = False
            for i, seg in enumerate(segments):
                if i in used:
                    continue
                # Check if this segment connects to current end
                if seg['start_node'] == current_end:
                    ring_points.extend(seg['points'][1:])  # Skip first (duplicate)
                    current_end = seg['end_node']
                    used.add(i)
                    found = True
                    break
                elif seg['end_node'] == current_end:
                    # Reverse and connect
                    ring_points.extend(reversed(seg['points'][:-1]))
                    current_end = seg['start_node']
                    used.add(i)
                    found = True
                    break

            if not found or current_end == start_node:
                break

        # Check if ring is closed
        is_closed = (current_end == start_node) and len(ring_points) >= 3

        # Simplify
        simplified = _douglas_peucker(ring_points, simplify_epsilon)
        if len(simplified) >= 3:
            assembled.append((simplified, is_closed))

    return assembled


def query_water_features(center_lat: float, center_lon: float,
                         radius_m: float, simplify_epsilon: float = 50.0) -> list:
    """Query OSM for water boundaries near a location.

    Fetches coastlines, lakes, reservoirs, and riverbanks within the radius.
    Properly assembles multipolygon relations (like large lakes).

    Args:
        center_lat, center_lon: Center point.
        radius_m: Search radius in meters.
        simplify_epsilon: Douglas-Peucker simplification tolerance in meters.

    Returns:
        List of dicts, each with:
            id: str identifier
            name: str feature name
            points: list of {"x": float, "y": float} in local meters
            closed: bool whether the polygon is closed
    """
    # Build Overpass query for water features
    query = f"""
    [out:json][timeout:60];
    (
      way["natural"="coastline"](around:{radius_m},{center_lat},{center_lon});
      way["natural"="water"](around:{radius_m},{center_lat},{center_lon});
      relation["natural"="water"](around:{radius_m},{center_lat},{center_lon});
      way["waterway"="riverbank"](around:{radius_m},{center_lat},{center_lon});
    );
    out body;
    >;
    out skel qt;
    """

    resp = requests.post(OVERPASS_URL, data={"data": query}, timeout=120)
    resp.raise_for_status()
    data = resp.json()

    # Build node lookup
    nodes = {}
    for el in data.get("elements", []):
        if el["type"] == "node":
            nodes[el["id"]] = (el["lat"], el["lon"])

    # Build way lookup
    way_data = {}
    for el in data.get("elements", []):
        if el["type"] == "way" and "nodes" in el:
            way_data[el["id"]] = {
                'nodes': el["nodes"],
                'tags': el.get("tags", {})
            }

    # Track which ways belong to relations (don't add them separately)
    relation_way_ids = set()

    # Process relations first - assemble multipolygons
    results = []
    for el in data.get("elements", []):
        if el["type"] != "relation":
            continue

        tags = el.get("tags", {})
        rel_name = tags.get("name", "")
        rel_type = tags.get("water", tags.get("natural", "water"))

        # Collect outer way members
        outer_ways = []
        for member in el.get("members", []):
            if member["type"] == "way" and member.get("role") in ("outer", ""):
                way_id = member["ref"]
                relation_way_ids.add(way_id)
                if way_id in way_data:
                    outer_ways.append((way_id, way_data[way_id]['nodes']))

        # Assemble the outer ring(s)
        assembled = _assemble_multipolygon(outer_ways, center_lat, center_lon,
                                           nodes, simplify_epsilon)
        for i, (points, is_closed) in enumerate(assembled):
            # Filter: keep only features that have points within range
            in_range = any(
                math.sqrt(p[0]**2 + p[1]**2) <= radius_m * 1.2
                for p in points
            )
            if not in_range:
                continue

            results.append({
                "id": f"relation_{el['id']}_{i}",
                "name": rel_name or f"{rel_type}_{el['id']}",
                "points": [{"x": round(p[0], 1), "y": round(p[1], 1)} for p in points],
                "closed": is_closed,
            })

    # Process standalone ways (not part of any relation)
    for way_id, wd in way_data.items():
        if way_id in relation_way_ids:
            continue  # Skip - already part of a relation

        way_nodes = wd['nodes']
        raw_points = []
        for nid in way_nodes:
            if nid in nodes:
                lat, lon = nodes[nid]
                x, y = latlon_to_xy(lat, lon, center_lat, center_lon)
                raw_points.append((x, y))
        if len(raw_points) < 3:
            continue

        # Check if closed
        closed = (way_nodes[0] == way_nodes[-1]) and len(way_nodes) > 3

        # Simplify
        simplified = _douglas_peucker(raw_points, simplify_epsilon)
        if len(simplified) < 3:
            continue

        # Filter: keep only features that have points within range
        in_range = any(
            math.sqrt(p[0]**2 + p[1]**2) <= radius_m * 1.2
            for p in simplified
        )
        if not in_range:
            continue

        tags = wd['tags']
        name = tags.get("name", "")
        water_type = tags.get("water", tags.get("natural", "shoreline"))

        results.append({
            "id": f"way_{way_id}",
            "name": name or f"{water_type}_{way_id}",
            "points": [{"x": round(p[0], 1), "y": round(p[1], 1)} for p in simplified],
            "closed": closed,
        })

    return results
