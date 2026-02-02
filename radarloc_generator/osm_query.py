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


def query_water_features(center_lat: float, center_lon: float,
                         radius_m: float, simplify_epsilon: float = 50.0) -> list:
    """Query OSM for water boundaries near a location.

    Fetches coastlines, lakes, reservoirs, and riverbanks within the radius.

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

    # Extract ways
    ways = []
    for el in data.get("elements", []):
        if el["type"] == "way" and "nodes" in el:
            way_nodes = el["nodes"]
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

            tags = el.get("tags", {})
            name = tags.get("name", "")
            water_type = tags.get("water", tags.get("natural", "shoreline"))

            ways.append({
                "id": f"shoreline_{el['id']}",
                "name": name or f"{water_type}_{el['id']}",
                "points": [{"x": round(p[0], 1), "y": round(p[1], 1)} for p in simplified],
                "closed": closed,
            })

    # Also extract relation members (outer ways of multipolygons)
    relations = [el for el in data.get("elements", []) if el["type"] == "relation"]
    relation_way_ids = set()
    for rel in relations:
        for member in rel.get("members", []):
            if member["type"] == "way" and member.get("role") in ("outer", ""):
                relation_way_ids.add(member["ref"])

    # Ways that belong to relations are already included above
    return ways
