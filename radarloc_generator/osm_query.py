"""Query Overpass API for water boundaries and coastlines."""
import hashlib
import json
import math
import os
import sys
import time
import requests
from .coordinate_transform import latlon_to_xy

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_OVERPASS_URLS = [
    OVERPASS_URL,
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
_USER_AGENT = "MarineRadarLocationGenerator/1.0"
_OVERPASS_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "application/json",
}
# Raw Overpass responses are cached on disk so a flaky mirror or an offline
# session can still regenerate a location from the last good pull.
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".overpass_cache")
_CACHE_MAX_AGE_S = 30 * 24 * 3600  # cached pulls stay valid for 30 days
_EXCLUDED_NAME_KEYWORDS = ("fountain", "reflecting pool", "ornamental pool")
_EXCLUDED_WATER_KINDS = {
    "reflecting_pool",
    "moat",
    "wastewater",
    "drain",
    "ditch",
    "raw",
}
_SHORE_BOUNDARY_CLASSES = {
    "coastline",
    "shoreline",
    "riverbank",
    "water",
    "harbour",
    "harbor",
    "bay",
    "strait",
    "lagoon",
}
# Man-made structures worth exporting as radar detail near harbors.
_DETAIL_STRUCTURE_CLASSES = {
    "pier",
    "breakwater",
    "groyne",
    "quay",
    "jetty",
    "dock",
}
# Wetland flavors: low-lying vegetated shoreline that returns weak-but-real
# radar echo and must not be pruned as "not part of the water network".
_WETLAND_CLASSES = {
    "beach",
    "sand",
    "dune",
    "wetland",
    "marsh",
    "saltmarsh",
    "tidalflat",
    "reedbed",
    "swamp",
    "wet_meadow",
    "mangrove",
    "bog",
    "fen",
}
_PASSTHROUGH_CLASSES = _DETAIL_STRUCTURE_CLASSES | _WETLAND_CLASSES


def _cache_path_for_query(query: str) -> str:
    digest = hashlib.sha1(" ".join(query.split()).encode("utf-8")).hexdigest()
    return os.path.join(_CACHE_DIR, f"overpass_{digest}.json")


def _read_query_cache(query: str) -> dict | None:
    path = _cache_path_for_query(query)
    try:
        if not os.path.isfile(path):
            return None
        if time.time() - os.path.getmtime(path) > _CACHE_MAX_AGE_S:
            return None
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) and "elements" in data else None
    except (OSError, ValueError):
        return None


def _write_query_cache(query: str, data: dict) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        path = _cache_path_for_query(query)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp_path, path)
    except OSError:
        pass  # cache is best-effort; never fail an export over it


def _validate_overpass_payload(data: dict) -> str | None:
    """Return an error string when a response is unusable, else None."""
    if not isinstance(data, dict):
        return "response is not a JSON object"
    if "elements" not in data:
        return "response has no elements array"
    remark = str(data.get("remark", "") or "")
    lowered = remark.lower()
    if "timed out" in lowered or "out of memory" in lowered or "error" in lowered:
        # Overpass reports query-runtime truncation via `remark` while still
        # returning HTTP 200 and a partial elements list. Treat as retryable.
        return f"overpass remark: {remark}"
    return None


def _post_overpass(query: str, *, allow_cache_fallback: bool = True) -> dict:
    """Run an Overpass query with mirror fallback, retries, and disk cache.

    Order of preference: live result from any mirror -> partial (truncated)
    live result -> cached result from an earlier successful run. HTTP-level
    failures, malformed payloads, and Overpass-side timeouts all rotate to the
    next attempt/mirror instead of aborting the export.
    """
    last_error: Exception | None = None
    partial_data: dict | None = None
    for url_index, url in enumerate(_OVERPASS_URLS):
        for attempt in range(3):
            try:
                resp = requests.post(
                    url,
                    data={"data": query},
                    headers=_OVERPASS_HEADERS,
                    timeout=240,
                )
                resp.raise_for_status()
                data = resp.json()
                problem = _validate_overpass_payload(data)
                if problem is None:
                    _write_query_cache(query, data)
                    return data
                last_error = RuntimeError(f"{url}: {problem}")
                if isinstance(data, dict) and data.get("elements"):
                    partial_data = data  # keep the best truncated payload seen
            except ValueError as exc:  # non-JSON body (rate-limit HTML, etc.)
                last_error = exc
            except requests.RequestException as exc:
                last_error = exc
                status_code = getattr(exc.response, "status_code", None)
                retryable = status_code in {429, 500, 502, 503, 504} or status_code is None
                if not retryable:
                    raise
            sleep_s = min(12.0, 1.5 * (attempt + 1) * (url_index + 1))
            time.sleep(sleep_s)

    if partial_data is not None:
        print("  Warning: all Overpass mirrors truncated the query; "
              "using the largest partial result", file=sys.stderr)
        return partial_data
    if allow_cache_fallback:
        cached = _read_query_cache(query)
        if cached is not None:
            print("  Warning: Overpass unreachable; using cached response",
                  file=sys.stderr)
            return cached
    if last_error is not None:
        raise last_error
    raise RuntimeError("Overpass query failed without an exception")


def _polygon_area_xy(points: list[tuple[float, float]]) -> float:
    """Return polygon area in square meters for local x/y point tuples."""
    if len(points) < 3:
        return 0.0
    area = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _point_distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _distance_point_to_segment_projection(px: float, py: float,
                                          ax: float, ay: float,
                                          bx: float, by: float) -> tuple[float, float, float]:
    dx, dy = bx - ax, by - ay
    denominator = dx * dx + dy * dy
    if denominator <= 1e-12:
        return math.hypot(px - ax, py - ay), ax, ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denominator))
    qx, qy = ax + t * dx, ay + t * dy
    return math.hypot(px - qx, py - qy), qx, qy


def _normalize_feature_class(value: str) -> str:
    return str(value or "").strip().lower()


def _point_in_polygon_xy(px: float, py: float, points: list[tuple[float, float]]) -> bool:
    if len(points) < 3:
        return False
    inside = False
    j = len(points) - 1
    for i in range(len(points)):
        xi, yi = points[i]
        xj, yj = points[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-9) + xi):
            inside = not inside
        j = i
    return inside


def _bbox_for_points(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_gap_m(a: tuple[float, float, float, float],
                b: tuple[float, float, float, float]) -> float:
    dx = max(0.0, a[0] - b[2], b[0] - a[2])
    dy = max(0.0, a[1] - b[3], b[1] - a[3])
    return math.hypot(dx, dy)


def _min_point_set_distance(points_a: list[tuple[float, float]],
                            points_b: list[tuple[float, float]],
                            best_so_far: float) -> float:
    best = best_so_far
    for ax, ay in points_a:
        for bx, by in points_b:
            dist_m = math.hypot(ax - bx, ay - by)
            if dist_m < best:
                best = dist_m
                if best <= 1e-6:
                    return 0.0
    return best


# Below this search radius, man-made shoreline structures (piers, docks,
# breakwaters) are resolvable at radar scale and worth exporting.
_STRUCTURE_DETAIL_RADIUS_M = 15_000.0
# Wetland/marsh boundaries matter out to broader ranges than piers do.
_WETLAND_DETAIL_RADIUS_M = 30_000.0


def _wrap_overpass_query(clauses: list[str], timeout_s: int) -> str:
    body = "\n      ".join(clauses)
    return f"""
    [out:json][timeout:{timeout_s}];
    (
      {body}
    );
    out body;
    >;
    out skel qt;
    """


def _build_overpass_query_groups(center_lat: float, center_lon: float,
                                 radius_m: float, detail_profile: str) -> list[dict]:
    """Build Overpass query groups tuned for the requested detail profile.

    Groups run as separate requests so one failing/overloaded feature class
    degrades the export instead of aborting it. Only the core water group is
    required; everything else is additive detail.
    """
    around = f"(around:{radius_m},{center_lat},{center_lon});"
    timeout_s = 90 if radius_m <= 12_000.0 else 180

    groups: list[dict] = [{
        "name": "water_core",
        "required": True,
        "query": _wrap_overpass_query([
            f'way["natural"="coastline"]{around}',
            f'way["natural"="water"]{around}',
            f'relation["natural"="water"]{around}',
            f'way["waterway"="riverbank"]{around}',
            f'relation["waterway"="riverbank"]{around}',
        ], timeout_s),
    }]

    if detail_profile == "harbor_tidal":
        groups.append({
            "name": "water_tidal",
            "required": False,
            "query": _wrap_overpass_query([
                f'way["water"~"river|canal|harbour|harbor|lagoon|bay|fairway|dock|stream|strait"]{around}',
                f'relation["water"~"river|canal|harbour|harbor|lagoon|bay|fairway|dock|stream|strait"]{around}',
                f'way["tidal"="yes"]["natural"="water"]{around}',
                f'relation["tidal"="yes"]["natural"="water"]{around}',
                f'way["tidal"="yes"]["water"]{around}',
                f'relation["tidal"="yes"]["water"]{around}',
                f'way["waterway"~"canal|stream|river"]{around}',
                f'relation["waterway"~"canal|stream|river"]{around}',
            ], timeout_s),
        })

    if radius_m <= _WETLAND_DETAIL_RADIUS_M:
        groups.append({
            "name": "wetlands",
            "required": False,
            "query": _wrap_overpass_query([
                f'way["natural"="wetland"]{around}',
                f'relation["natural"="wetland"]{around}',
                f'way["natural"~"mud|shoal|beach"]{around}',
            ], timeout_s),
        })

    if radius_m <= _STRUCTURE_DETAIL_RADIUS_M or detail_profile == "harbor_tidal":
        groups.append({
            "name": "structures",
            "required": False,
            "query": _wrap_overpass_query([
                f'way["man_made"~"^(pier|breakwater|groyne|quay|jetty)$"]{around}',
                f'relation["man_made"~"^(pier|breakwater|groyne|quay|jetty)$"]{around}',
                f'way["waterway"="dock"]{around}',
                f'relation["waterway"="dock"]{around}',
                f'way["leisure"="marina"]{around}',
                f'relation["leisure"="marina"]{around}',
            ], timeout_s),
        })

    return groups


def _feature_class_from_tags(tags: dict, fallback: str) -> str:
    """Derive the exported feature_class from OSM tags.

    Emits classes the RustCore loader already understands: water-fill classes
    (water/river/lake/dock/...), detail structures (pier/breakwater/groyne/
    quay/jetty), shorelines, and wetland flavors (marsh/tidalflat/...).
    """
    man_made = str(tags.get("man_made", "")).strip().lower()
    if man_made in {"pier", "breakwater", "groyne", "quay", "jetty"}:
        return man_made
    waterway = str(tags.get("waterway", "")).strip().lower()
    if waterway == "dock":
        return "dock"
    if str(tags.get("leisure", "")).strip().lower() == "marina":
        # A marina is a navigable water basin: reuse the dock water-fill class.
        return "dock"
    natural = str(tags.get("natural", "")).strip().lower()
    if natural == "wetland":
        wetland_kind = str(tags.get("wetland", "")).strip().lower()
        return wetland_kind if wetland_kind in _WETLAND_CLASSES else "wetland"
    if natural == "beach":
        return "beach"
    if natural in {"mud", "shoal"}:
        return "tidalflat"
    if natural == "coastline":
        return "coastline"
    water_kind = str(tags.get("water", "")).strip().lower()
    if water_kind:
        return water_kind
    if waterway == "riverbank":
        # RustCore's water-fill set has no "riverbank"; a riverbank polygon IS
        # river water, so emit the class the loader can act on.
        return "river"
    if natural == "water":
        return "water"
    return _normalize_feature_class(fallback) or "water_or_shoreline"


def _should_preserve_detail(points: list[tuple[float, float]], *,
                            feature_class: str, closed: bool,
                            radius_m: float, detail_profile: str) -> bool:
    """Mark near-range harbor/tidal features so downstream simplification stays gentle."""
    if not points:
        return False
    if _normalize_feature_class(feature_class) in _PASSTHROUGH_CLASSES:
        return True
    if detail_profile != "harbor_tidal":
        return False

    feature_class = _normalize_feature_class(feature_class)
    if feature_class in _EXCLUDED_WATER_KINDS:
        return False

    min_radius = min(math.hypot(x, y) for x, y in points)
    if min_radius > radius_m * 1.05:
        return False

    if not closed:
        return True

    area_m2 = _polygon_area_xy(points)
    return area_m2 <= max(2_000_000.0, (radius_m * radius_m) * 0.75)


def _feature_endpoint_pairs(points_a: list[tuple[float, float]],
                            points_b: list[tuple[float, float]]) -> list[tuple[str, float]]:
    return [
        ("start_start", _point_distance(points_a[0], points_b[0])),
        ("start_end", _point_distance(points_a[0], points_b[-1])),
        ("end_start", _point_distance(points_a[-1], points_b[0])),
        ("end_end", _point_distance(points_a[-1], points_b[-1])),
    ]


def _merge_open_feature_geometries(features: list[dict], *,
                                   simplify_epsilon: float,
                                   radius_m: float) -> list[dict]:
    """Merge nearby open shoreline fragments into cleaner chains.

    OSM frequently returns harbor boundaries as several adjoining coastline
    ways. When exported separately they leave small gaps that later confuse
    land/water filling. This pass reconnects adjacent open fragments and
    closes near-rings that are clearly local harbor or island outlines.
    """
    if len(features) < 2:
        return features

    # Structures and wetlands are exported verbatim: stitching a pier stub to
    # a marsh edge (or auto-closing either) invents geometry that OSM doesn't
    # contain. Split them out, merge only genuine shoreline fragments.
    passthrough = [
        feature for feature in features
        if _normalize_feature_class(feature.get("feature_class", "")) in _PASSTHROUGH_CLASSES
    ]
    features = [
        feature for feature in features
        if _normalize_feature_class(feature.get("feature_class", "")) not in _PASSTHROUGH_CLASSES
    ]
    if len(features) < 2:
        return features + passthrough

    merge_threshold_m = max(8.0, min(180.0, simplify_epsilon * 4.0))
    close_threshold_m = max(16.0, min(220.0, simplify_epsilon * 6.0))
    edge_guard_m = max(50.0, min(radius_m * 0.08, 500.0))

    def _to_working_feature(src: dict) -> dict:
        return {
            "id": src.get("id", ""),
            "name": src.get("name", ""),
            "feature_class": _normalize_feature_class(src.get("feature_class", "")),
            "closed": bool(src.get("closed", False)),
            "preserve_detail": bool(src.get("preserve_detail", False)),
            "points": [
                (float(p["x"]), float(p["y"])) if isinstance(p, dict) else (float(p[0]), float(p[1]))
                for p in src.get("points", [])
            ],
        }

    def _append_points(base: list[tuple[float, float]],
                       extra: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if not base:
            return list(extra)
        if not extra:
            return list(base)
        merged = list(base)
        if _point_distance(merged[-1], extra[0]) <= 0.5:
            merged.extend(extra[1:])
        else:
            merged.extend(extra)
        return merged

    def _combine(a: dict, b: dict, orientation: str) -> dict:
        if orientation == "end_start":
            points = _append_points(a["points"], b["points"])
        elif orientation == "start_end":
            points = _append_points(b["points"], a["points"])
        elif orientation == "start_start":
            points = _append_points(list(reversed(a["points"])), b["points"])
        else:  # end_end
            points = _append_points(a["points"], list(reversed(b["points"])))
        return {
            "id": a["id"] or b["id"],
            "name": a["name"] or b["name"],
            "feature_class": a["feature_class"] or b["feature_class"],
            "closed": False,
            "preserve_detail": a["preserve_detail"] or b["preserve_detail"],
            "points": points,
        }

    closed_features = []
    open_features = []
    for feature in features:
        working = _to_working_feature(feature)
        if len(working["points"]) < 2:
            continue
        if working["closed"]:
            closed_features.append(working)
        else:
            open_features.append(working)

    changed = True
    while changed and len(open_features) > 1:
        changed = False
        best = None
        for i in range(len(open_features)):
            for j in range(i + 1, len(open_features)):
                a = open_features[i]
                b = open_features[j]
                if a["feature_class"] != b["feature_class"]:
                    continue
                for orientation, dist_m in _feature_endpoint_pairs(a["points"], b["points"]):
                    if dist_m > merge_threshold_m:
                        continue
                    if best is None or dist_m < best[0]:
                        best = (dist_m, i, j, orientation)
        if best is None:
            break
        _, i, j, orientation = best
        combined = _combine(open_features[i], open_features[j], orientation)
        for index in sorted((i, j), reverse=True):
            del open_features[index]
        open_features.append(combined)
        changed = True

    for feature in open_features:
        if len(feature["points"]) < 3:
            continue
        gap_m = _point_distance(feature["points"][0], feature["points"][-1])
        max_endpoint_radius = max(
            math.hypot(*feature["points"][0]),
            math.hypot(*feature["points"][-1]),
        )
        if gap_m <= close_threshold_m and max_endpoint_radius <= radius_m + edge_guard_m:
            if _point_distance(feature["points"][0], feature["points"][-1]) > 0.5:
                feature["points"].append(feature["points"][0])
            feature["closed"] = True
            closed_features.append(feature)
        else:
            closed_features.append(feature)

    results = []
    for feature in closed_features:
        points = [{"x": round(p[0], 1), "y": round(p[1], 1)} for p in feature["points"]]
        results.append({
            "id": feature["id"],
            "name": feature["name"],
            "points": points,
            "closed": bool(feature["closed"]),
            "feature_class": feature["feature_class"] or "water_or_shoreline",
            **({"preserve_detail": True} if feature["preserve_detail"] else {}),
        })
    return results + passthrough


def _xy_points(feature: dict) -> list[tuple[float, float]]:
    return [
        (float(point["x"]), float(point["y"]))
        if isinstance(point, dict)
        else (float(point[0]), float(point[1]))
        for point in feature.get("points", [])
    ]


def _polyline_length(points: list[tuple[float, float]]) -> float:
    return sum(_point_distance(a, b) for a, b in zip(points, points[1:]))


def _is_redundant_short_boundary_fragment(feature_index: int,
                                          points: list[tuple[float, float]],
                                          point_lists: list[list[tuple[float, float]]],
                                          features: list[dict], *,
                                          coverage_tolerance_m: float,
                                          anchor_tolerance_m: float,
                                          max_length_m: float) -> bool:
    """Identify short open shoreline shards already covered by closed water geometry."""
    if len(points) < 2 or _polyline_length(points) > max_length_m:
        return False

    provider_segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for other_index, other_points in enumerate(point_lists):
        if other_index == feature_index or not features[other_index].get("closed", False):
            continue
        feature_class = _normalize_feature_class(features[other_index].get("feature_class", ""))
        if feature_class in _DETAIL_STRUCTURE_CLASSES or len(other_points) < 2:
            continue
        provider_segments.extend(zip(other_points, other_points[1:]))

    if not provider_segments:
        return False

    min_distances = []
    for px, py in points:
        best = min(
            _distance_point_to_segment_projection(px, py, a[0], a[1], b[0], b[1])[0]
            for a, b in provider_segments
        )
        if best > coverage_tolerance_m:
            return False
        min_distances.append(best)

    return bool(min_distances) and min(min_distances) <= anchor_tolerance_m


def _segment_circle_parameters(a: tuple[float, float],
                               b: tuple[float, float],
                               radius_m: float) -> list[float]:
    dx, dy = b[0] - a[0], b[1] - a[1]
    qa = dx * dx + dy * dy
    if qa <= 1e-12:
        return []
    qb = 2.0 * (a[0] * dx + a[1] * dy)
    qc = a[0] * a[0] + a[1] * a[1] - radius_m * radius_m
    discriminant = qb * qb - 4.0 * qa * qc
    if discriminant < 0.0:
        return []
    root = math.sqrt(max(0.0, discriminant))
    return sorted({
        max(0.0, min(1.0, value))
        for value in ((-qb - root) / (2.0 * qa), (-qb + root) / (2.0 * qa))
        if -1e-9 <= value <= 1.0 + 1e-9
    })


def _clip_open_feature_to_range(feature: dict, radius_m: float) -> list[dict]:
    """Clip an open shoreline to the circular radar range boundary."""
    points = _xy_points(feature)
    if feature.get("closed", False) or len(points) < 2:
        return [dict(feature)]

    chains: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for a, b in zip(points, points[1:]):
        parameters = [0.0, *_segment_circle_parameters(a, b, radius_m), 1.0]
        parameters = sorted(set(parameters))
        for t0, t1 in zip(parameters, parameters[1:]):
            if t1 - t0 <= 1e-10:
                continue
            tm = (t0 + t1) * 0.5
            mx = a[0] + (b[0] - a[0]) * tm
            my = a[1] + (b[1] - a[1]) * tm
            if math.hypot(mx, my) > radius_m + 1e-6:
                if len(current) >= 2:
                    chains.append(current)
                current = []
                continue
            p0 = (a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0)
            p1 = (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1)
            if not current or _point_distance(current[-1], p0) > 0.05:
                if len(current) >= 2:
                    chains.append(current)
                current = [p0]
            if _point_distance(current[-1], p1) > 0.05:
                current.append(p1)
    if len(current) >= 2:
        chains.append(current)

    output = []
    for index, chain in enumerate(chains):
        clipped = dict(feature)
        clipped["id"] = f"{feature.get('id', 'feature')}_clip{index}" if len(chains) > 1 else feature.get("id", "")
        clipped["points"] = [{"x": round(x, 1), "y": round(y, 1)} for x, y in chain]
        clipped["topology_range_clipped"] = any(
            math.hypot(x, y) >= radius_m - 0.2 for x, y in (chain[0], chain[-1])
        )
        output.append(clipped)
    return output


def _ray_segment_intersection(origin: tuple[float, float],
                              direction: tuple[float, float],
                              a: tuple[float, float],
                              b: tuple[float, float]) -> tuple[float, tuple[float, float]] | None:
    sx, sy = b[0] - a[0], b[1] - a[1]
    cross = direction[0] * sy - direction[1] * sx
    if abs(cross) <= 1e-9:
        return None
    qx, qy = a[0] - origin[0], a[1] - origin[1]
    distance = (qx * sy - qy * sx) / cross
    segment_t = (qx * direction[1] - qy * direction[0]) / cross
    if distance <= 0.05 or segment_t < -1e-7 or segment_t > 1.0 + 1e-7:
        return None
    return distance, (
        origin[0] + direction[0] * distance,
        origin[1] + direction[1] * distance,
    )


def _ray_ray_intersection(a: tuple[float, float],
                          a_direction: tuple[float, float],
                          b: tuple[float, float],
                          b_direction: tuple[float, float]) -> tuple[float, float, tuple[float, float]] | None:
    cross = a_direction[0] * b_direction[1] - a_direction[1] * b_direction[0]
    if abs(cross) <= 1e-9:
        return None
    qx, qy = b[0] - a[0], b[1] - a[1]
    a_distance = (qx * b_direction[1] - qy * b_direction[0]) / cross
    b_distance = (qx * a_direction[1] - qy * a_direction[0]) / cross
    if a_distance <= 0.05 or b_distance <= 0.05:
        return None
    return a_distance, b_distance, (
        a[0] + a_direction[0] * a_distance,
        a[1] + a_direction[1] * a_distance,
    )


def _normalized_direction(a: tuple[float, float],
                          b: tuple[float, float]) -> tuple[float, float] | None:
    dx, dy = a[0] - b[0], a[1] - b[1]
    length = math.hypot(dx, dy)
    if length <= 1e-6:
        return None
    return dx / length, dy / length


def _resolve_harbor_topology(features: list[dict], *, radius_m: float) -> tuple[list[dict], dict]:
    """Clip shorelines, continue dangling ends, and audit the resulting graph.

    The circular range boundary is a topological terminus, not a land feature.
    River and stream centerlines are deliberately excluded from continuation.
    """
    working: list[dict] = []
    for feature in features:
        feature_class = _normalize_feature_class(feature.get("feature_class", ""))
        if feature.get("closed", False) or feature_class not in _SHORE_BOUNDARY_CLASSES:
            working.append(dict(feature))
        else:
            working.extend(_clip_open_feature_to_range(feature, radius_m))

    boundary_indices = [
        index for index, feature in enumerate(working)
        if not feature.get("closed", False)
        and _normalize_feature_class(feature.get("feature_class", "")) in _SHORE_BOUNDARY_CLASSES
        and len(feature.get("points", [])) >= 2
    ]
    snap_m = max(1.0, min(6.0, radius_m * 0.0015))
    max_extension_m = max(30.0, min(180.0, radius_m * 0.15))
    ring_tolerance_m = max(1.0, min(5.0, radius_m * 0.002))
    redundant_overlap_tolerance_m = max(25.0, min(60.0, radius_m * 0.0045))
    redundant_anchor_tolerance_m = max(6.0, min(15.0, snap_m * 2.0))
    redundant_max_length_m = max(150.0, min(350.0, radius_m * 0.03))

    point_lists = [_xy_points(feature) for feature in working]
    endpoints = []
    for feature_index in boundary_indices:
        points = point_lists[feature_index]
        for at_start in (True, False):
            point = points[0] if at_start else points[-1]
            neighbor = points[1] if at_start else points[-2]
            direction = _normalized_direction(point, neighbor)
            if direction is None:
                continue
            endpoints.append({
                "feature": feature_index,
                "start": at_start,
                "point": point,
                "direction": direction,
                "ring": abs(math.hypot(*point) - radius_m) <= ring_tolerance_m,
            })

    assigned: dict[int, tuple[float, float]] = {}
    extension_kind: dict[int, str] = {}

    # Resolve tiny coordinate seams first without changing the mapped shape.
    endpoint_events = []
    for left in range(len(endpoints)):
        if endpoints[left]["ring"]:
            continue
        for right in range(left + 1, len(endpoints)):
            if endpoints[right]["ring"] or endpoints[left]["feature"] == endpoints[right]["feature"]:
                continue
            gap = _point_distance(endpoints[left]["point"], endpoints[right]["point"])
            if gap <= snap_m:
                endpoint_events.append((gap, left, right))
    for _, left, right in sorted(endpoint_events):
        if left in assigned or right in assigned:
            continue
        a, b = endpoints[left]["point"], endpoints[right]["point"]
        meeting = ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)
        assigned[left] = meeting
        assigned[right] = meeting
        extension_kind[left] = extension_kind[right] = "snap"

    segments = []
    for feature_index, points in enumerate(point_lists):
        feature_class = _normalize_feature_class(working[feature_index].get("feature_class", ""))
        if feature_class not in _SHORE_BOUNDARY_CLASSES:
            continue
        for segment_index, (a, b) in enumerate(zip(points, points[1:])):
            segments.append((feature_index, segment_index, a, b))

    # Compute every continuation against the same unmodified geometry, then
    # accept shortest events. This allows two dangling rays to meet naturally.
    continuation_events = []
    for endpoint_index, endpoint in enumerate(endpoints):
        if endpoint["ring"] or endpoint_index in assigned:
            continue
        for feature_index, segment_index, a, b in segments:
            if feature_index == endpoint["feature"]:
                own_last = len(point_lists[feature_index]) - 2
                if segment_index in ({0} if endpoint["start"] else {own_last}):
                    continue
            hit = _ray_segment_intersection(endpoint["point"], endpoint["direction"], a, b)
            if hit is not None and hit[0] <= max_extension_m:
                continuation_events.append((hit[0], "segment", endpoint_index, None, hit[1]))

    for left in range(len(endpoints)):
        if endpoints[left]["ring"] or left in assigned:
            continue
        for right in range(left + 1, len(endpoints)):
            if endpoints[right]["ring"] or right in assigned:
                continue
            if endpoints[left]["feature"] == endpoints[right]["feature"]:
                continue
            hit = _ray_ray_intersection(
                endpoints[left]["point"],
                endpoints[left]["direction"],
                endpoints[right]["point"],
                endpoints[right]["direction"],
            )
            if hit is None:
                continue
            left_distance, right_distance, meeting = hit
            if left_distance <= max_extension_m and right_distance <= max_extension_m:
                continuation_events.append((max(left_distance, right_distance), "pair", left, right, meeting))

    for _, kind, left, right, meeting in sorted(continuation_events, key=lambda item: item[0]):
        if left in assigned or (right is not None and right in assigned):
            continue
        assigned[left] = meeting
        extension_kind[left] = kind
        if right is not None:
            assigned[right] = meeting
            extension_kind[right] = kind

    per_feature_extensions: dict[int, list[dict]] = {}
    for endpoint_index, meeting in assigned.items():
        endpoint = endpoints[endpoint_index]
        feature_index = endpoint["feature"]
        points = point_lists[feature_index]
        if endpoint["start"]:
            points.insert(0, meeting)
        else:
            points.append(meeting)
        per_feature_extensions.setdefault(feature_index, []).append({
            "endpoint": "start" if endpoint["start"] else "end",
            "kind": extension_kind[endpoint_index],
            "length_m": round(_point_distance(endpoint["point"], meeting), 1),
        })

    unresolved_total = 0
    boundary_total = 0
    for feature_index in boundary_indices:
        points = point_lists[feature_index]
        unresolved = []
        for label, point in (("start", points[0]), ("end", points[-1])):
            if abs(math.hypot(*point) - radius_m) <= ring_tolerance_m:
                continue
            connected = False
            for other_index, other_points in enumerate(point_lists):
                for a, b in zip(other_points, other_points[1:]):
                    if other_index == feature_index and (point == a or point == b):
                        continue
                    distance, _, _ = _distance_point_to_segment_projection(point[0], point[1], a[0], a[1], b[0], b[1])
                    if distance <= snap_m:
                        connected = True
                        break
                if connected:
                    break
            if not connected:
                unresolved.append(label)
        feature = working[feature_index]
        if unresolved and _is_redundant_short_boundary_fragment(
            feature_index,
            points,
            point_lists,
            working,
            coverage_tolerance_m=redundant_overlap_tolerance_m,
            anchor_tolerance_m=redundant_anchor_tolerance_m,
            max_length_m=redundant_max_length_m,
        ):
            feature["topology_redundant_with_closed_boundary"] = True
            unresolved = []
        unresolved_total += len(unresolved)
        boundary_total += 2
        feature["points"] = [{"x": round(x, 1), "y": round(y, 1)} for x, y in points]
        if len(points) >= 3 and _point_distance(points[0], points[-1]) <= snap_m:
            feature["points"][-1] = dict(feature["points"][0])
            feature["closed"] = True
        feature["topology_extensions"] = per_feature_extensions.get(feature_index, [])
        feature["topology_unresolved_endpoints"] = unresolved

    audit = {
        "boundary_endpoint_count": boundary_total,
        "resolved_endpoint_count": boundary_total - unresolved_total,
        "unresolved_endpoint_count": unresolved_total,
        "extension_count": len(assigned),
        "range_clipped_feature_count": sum(
            1 for feature in working if feature.get("topology_range_clipped", False)
        ),
    }
    return working, audit


def _select_major_harbor_features(features: list[dict], *, radius_m: float) -> list[dict]:
    """Keep the major harbor outline plus major connected tributaries.

    Exact harbor queries can return thousands of tiny shoreline fragments. For
    broad harbor exports we want a simpler, defensible scene: the main water
    body outline and the major rivers/tributaries that shape it.
    """
    if not features:
        return []

    major_closed_area_m2 = max(150_000.0, radius_m * radius_m * 0.0012)
    major_open_length_m = max(2_400.0, radius_m * 0.22)
    origin_keep_radius_m = max(2_500.0, radius_m * 0.23)
    connect_threshold_m = max(140.0, min(radius_m * 0.02, 220.0))
    water_classes = {"water", "river", "shoreline", "coastline", "harbour", "harbor", "bay", "strait"}
    open_classes = {"shoreline", "coastline"}

    working = []
    for feature in features:
        points = [
            (float(p["x"]), float(p["y"])) if isinstance(p, dict) else (float(p[0]), float(p[1]))
            for p in feature.get("points", [])
        ]
        if len(points) < 2:
            continue
        closed = bool(feature.get("closed", False))
        feature_class = _normalize_feature_class(feature.get("feature_class", ""))
        min_radius = min(math.hypot(x, y) for x, y in points)
        length_m = sum(_point_distance(points[i], points[i + 1]) for i in range(len(points) - 1))
        area_m2 = _polygon_area_xy(points) if closed else 0.0
        keep = False
        if closed and feature_class in water_classes:
            keep = area_m2 >= major_closed_area_m2 or min_radius <= origin_keep_radius_m
        elif (not closed) and feature_class in open_classes:
            keep = length_m >= major_open_length_m or min_radius <= origin_keep_radius_m
        if not keep:
            continue
        working.append({
            "feature": feature,
            "points": points,
            "bbox": _bbox_for_points(points),
            "closed": closed,
            "feature_class": feature_class,
            "min_radius": min_radius,
            "contains_origin": closed and _point_in_polygon_xy(0.0, 0.0, points),
        })

    if len(working) <= 2:
        return [entry["feature"] for entry in working]

    seed_indices = {
        idx for idx, entry in enumerate(working)
        if entry["contains_origin"] or entry["min_radius"] <= origin_keep_radius_m
    }
    if not seed_indices:
        nearest_radius = min(entry["min_radius"] for entry in working)
        seed_indices = {
            idx for idx, entry in enumerate(working)
            if entry["min_radius"] <= nearest_radius + connect_threshold_m
        }

    adjacency = {idx: set() for idx in range(len(working))}
    for i in range(len(working)):
        for j in range(i + 1, len(working)):
            a = working[i]
            b = working[j]
            if _bbox_gap_m(a["bbox"], b["bbox"]) > connect_threshold_m:
                continue
            dist_m = _min_point_set_distance(a["points"], b["points"], connect_threshold_m + 1.0)
            if dist_m <= connect_threshold_m:
                adjacency[i].add(j)
                adjacency[j].add(i)

    visited = set(seed_indices)
    queue = list(seed_indices)
    while queue:
        idx = queue.pop()
        for neighbor in adjacency[idx]:
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)

    kept = [working[idx]["feature"] for idx in sorted(visited)]
    if kept:
        return kept
    return [entry["feature"] for entry in working]


def _prune_origin_connected_harbor_network(features: list[dict], *,
                                           radius_m: float,
                                           simplify_epsilon: float) -> list[dict]:
    """Keep only the water-geometry network connected to the own-ship origin.

    This is aimed at short-range harbor exports where OSM often includes nearby
    ornamental or disconnected inland water bodies that are not part of the
    navigable channel surrounding the radar. We seed from the feature(s)
    nearest the origin and retain only geometrically connected neighbors.
    """
    if len(features) <= 2:
        return features

    # Piers, docks, and marshes legitimately hang off land rather than the
    # navigable water network; connectivity pruning must never remove them.
    passthrough = [
        feature for feature in features
        if _normalize_feature_class(feature.get("feature_class", "")) in _PASSTHROUGH_CLASSES
    ]
    features = [
        feature for feature in features
        if _normalize_feature_class(feature.get("feature_class", "")) not in _PASSTHROUGH_CLASSES
    ]
    if len(features) <= 2:
        return features + passthrough

    working = []
    for feature in features:
        points = [
            (float(p["x"]), float(p["y"])) if isinstance(p, dict) else (float(p[0]), float(p[1]))
            for p in feature.get("points", [])
        ]
        if len(points) < 2:
            continue
        closed = bool(feature.get("closed", False))
        bbox = _bbox_for_points(points)
        min_radius = min(math.hypot(x, y) for x, y in points)
        contains_origin = closed and _point_in_polygon_xy(0.0, 0.0, points)
        working.append({
            "feature": feature,
            "points": points,
            "bbox": bbox,
            "closed": closed,
            "feature_class": _normalize_feature_class(feature.get("feature_class", "")),
            "min_radius": min_radius,
            "contains_origin": contains_origin,
        })

    if len(working) <= 2:
        return [entry["feature"] for entry in working] + passthrough

    nearest_radius = min(entry["min_radius"] for entry in working)
    seed_margin_m = max(30.0, min(180.0, simplify_epsilon * 25.0))
    connect_threshold_m = max(20.0, min(65.0, simplify_epsilon * 18.0))

    seed_indices = {
        idx for idx, entry in enumerate(working)
        if entry["contains_origin"] or entry["min_radius"] <= nearest_radius + seed_margin_m
    }
    open_indices = [idx for idx, entry in enumerate(working) if not entry["closed"]]
    if open_indices:
        nearest_open_radius = min(working[idx]["min_radius"] for idx in open_indices)
        seed_indices.update(
            idx for idx in open_indices
            if working[idx]["min_radius"] <= nearest_open_radius + seed_margin_m
        )
    if not seed_indices:
        return [entry["feature"] for entry in working] + passthrough

    adjacency = {idx: set() for idx in range(len(working))}
    for i in range(len(working)):
        for j in range(i + 1, len(working)):
            a = working[i]
            b = working[j]
            if _bbox_gap_m(a["bbox"], b["bbox"]) > connect_threshold_m:
                continue
            dist_m = _min_point_set_distance(a["points"], b["points"], connect_threshold_m + 1.0)
            if dist_m <= connect_threshold_m:
                adjacency[i].add(j)
                adjacency[j].add(i)

    visited = set(seed_indices)
    queue = list(seed_indices)
    while queue:
        idx = queue.pop()
        for neighbor in adjacency[idx]:
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)

    kept = [working[idx]["feature"] for idx in sorted(visited)]
    if open_indices and not any(not feature.get("closed", False) for feature in kept):
        nearest_open_idx = min(open_indices, key=lambda idx: working[idx]["min_radius"])
        kept.append(working[nearest_open_idx]["feature"])
    if not kept:
        return features + passthrough
    return kept + passthrough


def _skip_trivial_water_feature(tags: dict, name: str, points: list[tuple[float, float]],
                                *, closed: bool) -> bool:
    """Drop ornamental or tiny non-shoreline water features from .radarloc output."""
    lowered_name = (name or "").strip().lower()
    if any(keyword in lowered_name for keyword in _EXCLUDED_NAME_KEYWORDS):
        return True

    amenity = str(tags.get("amenity", "")).strip().lower()
    leisure = str(tags.get("leisure", "")).strip().lower()
    man_made = str(tags.get("man_made", "")).strip().lower()
    water_kind = str(tags.get("water", "")).strip().lower()
    waterway = str(tags.get("waterway", "")).strip().lower()

    if amenity == "fountain":
        return True
    if leisure == "swimming_pool":
        return True
    if man_made in {"basin", "wastewater_plant"}:
        return True
    if water_kind in _EXCLUDED_WATER_KINDS:
        return True
    if waterway in {"drain", "ditch"}:
        return True

    # Small enclosed decorative basins add clutter to harbor scenes but do not
    # produce meaningful shoreline geometry for radar-scale location exports.
    if closed and water_kind in {"basin", "pond"} and _polygon_area_xy(points) < 5_000.0:
        return True
    return False


def find_water_coordinates(location_name: str) -> dict:
    """Find the center coordinates of a water body by name.

    Uses Nominatim to search for water features matching the location name.
    More reliable than Overpass for name-based searches.

    Args:
        location_name: Name of the water body (e.g., "Lake Murray", "San Francisco Bay")

    Returns:
        Dict with 'lat', 'lon', 'name', 'area_km2' or None if not found.
    """
    NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

    headers = {
        "User-Agent": "MarineRadarLocationGenerator/1.0"
    }

    # Search for water features
    params = {
        "q": location_name,
        "format": "json",
        "limit": 20,
        "extratags": 1,
        "namedetails": 1,
    }

    try:
        resp = requests.get(NOMINATIM_URL, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        results = resp.json()
    except Exception as e:
        return {"error": str(e)}

    # Filter for water-related features
    water_types = ["water", "lake", "reservoir", "bay", "lagoon", "pond",
                   "river", "stream", "canal", "coastline", "sea", "ocean"]

    water_features = []
    for r in results:
        feature_type = r.get("type", "").lower()
        feature_class = r.get("class", "").lower()

        # Check if it's a water feature
        is_water = (
            feature_type in water_types or
            feature_class in ["natural", "water", "waterway"] or
            "lake" in r.get("display_name", "").lower() or
            "bay" in r.get("display_name", "").lower() or
            "reservoir" in r.get("display_name", "").lower()
        )

        if not is_water:
            continue

        # Get bounding box for area estimate
        bbox = r.get("boundingbox", [])
        if len(bbox) == 4:
            lat_range = float(bbox[1]) - float(bbox[0])
            lon_range = float(bbox[3]) - float(bbox[2])
            center_lat = (float(bbox[0]) + float(bbox[1])) / 2
            area_km2 = lat_range * 111 * lon_range * 111 * math.cos(math.radians(center_lat))
        else:
            area_km2 = 0

        water_features.append({
            "name": r.get("display_name", "").split(",")[0],
            "full_name": r.get("display_name", ""),
            "lat": float(r.get("lat", 0)),
            "lon": float(r.get("lon", 0)),
            "area_km2": area_km2,
            "type": feature_type,
            "osm_id": r.get("osm_id", "")
        })

    if not water_features:
        return {"error": f"No water features found matching '{location_name}'"}

    # Return the largest water feature
    largest = max(water_features, key=lambda x: x["area_km2"])
    return {
        "lat": round(largest["lat"], 6),
        "lon": round(largest["lon"], 6),
        "name": largest["name"],
        "area_km2": round(largest["area_km2"], 2),
        "all_matches": water_features
    }


def _douglas_peucker(points: list, epsilon: float) -> list:
    """Simplify a polyline using the Douglas-Peucker algorithm.

    Args:
        points: List of (x, y) tuples.
        epsilon: Maximum allowed deviation in meters.

    Returns:
        Simplified list of (x, y) tuples.
    """
    if len(points) <= 2 or epsilon <= 0.0:
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
                         radius_m: float, simplify_epsilon: float | None = None,
                         detail_profile: str = "default") -> list:
    """Query OSM for water boundaries near a location.

    Fetches coastlines, lakes, reservoirs, and riverbanks within the radius.
    Properly assembles multipolygon relations (like large lakes). For
    short-range harbor/tidal exports, `detail_profile="harbor_tidal"` adds a
    denser near-range query and marks shoreline-relevant features so downstream
    simplification stays gentle.

    Args:
        center_lat, center_lon: Center point.
        radius_m: Search radius in meters.
        simplify_epsilon: Douglas-Peucker simplification tolerance in meters.
        detail_profile: Either "default" or "harbor_tidal".

    Returns:
        List of dicts, each with:
            id: str identifier
            name: str feature name
            points: list of {"x": float, "y": float} in local meters
            closed: bool whether the polygon is closed
    """
    if simplify_epsilon is None:
        # Scale simplification with range: a 12 nm lake sweep tolerates ~18 m,
        # a 1.5 nm harbor needs vertex-accurate shoreline. The old fixed 50 m
        # default silently erased piers, coves, and narrow channels.
        simplify_epsilon = max(3.0, min(25.0, radius_m * 0.0008))

    preserve_exact_linework = detail_profile == "harbor_tidal" and simplify_epsilon <= 0.0
    groups = _build_overpass_query_groups(center_lat, center_lon, radius_m, detail_profile)

    elements: list[dict] = []
    seen_elements: set[tuple[str, int]] = set()
    for group in groups:
        try:
            data = _post_overpass(group["query"])
        except Exception as exc:
            if group["required"]:
                raise
            print(f"  Warning: optional OSM query group '{group['name']}' "
                  f"failed ({exc}); continuing without it", file=sys.stderr)
            continue
        for el in data.get("elements", []):
            key = (el.get("type", ""), el.get("id", 0))
            if key in seen_elements:
                continue
            seen_elements.add(key)
            elements.append(el)

    # Build node lookup
    nodes = {}
    for el in elements:
        if el["type"] == "node":
            nodes[el["id"]] = (el["lat"], el["lon"])

    # Build way lookup
    way_data = {}
    for el in elements:
        if el["type"] == "way" and "nodes" in el:
            way_data[el["id"]] = {
                'nodes': el["nodes"],
                'tags': el.get("tags", {})
            }

    # Track which ways belong to relations (don't add them separately)
    relation_way_ids = set()

    # Process relations first - assemble multipolygons
    results = []
    for el in elements:
        if el["type"] != "relation":
            continue

        tags = el.get("tags", {})
        rel_name = tags.get("name", "")
        rel_type = _feature_class_from_tags(tags, tags.get("water", tags.get("natural", "water")))

        # Collect outer and inner way members. Inner rings are islands within
        # the water polygon -- previously they were dropped outright, which is
        # why harbor exports lost islands RustCore needed to label as land.
        outer_ways = []
        inner_ways = []
        for member in el.get("members", []):
            if member["type"] == "way":
                way_id = member["ref"]
                role = member.get("role", "")
                if role in ("outer", ""):
                    relation_way_ids.add(way_id)
                    if way_id in way_data:
                        outer_ways.append((way_id, way_data[way_id]['nodes']))
                elif role == "inner":
                    relation_way_ids.add(way_id)
                    if way_id in way_data:
                        inner_ways.append((way_id, way_data[way_id]['nodes']))

        # Assemble the outer ring(s)
        assembled = _assemble_multipolygon(
            outer_ways,
            center_lat,
            center_lon,
            nodes,
            0.0 if preserve_exact_linework else simplify_epsilon,
        )
        for i, (points, is_closed) in enumerate(assembled):
            if _skip_trivial_water_feature(tags, rel_name, points, closed=is_closed):
                continue
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
                "feature_class": rel_type,
                **({
                    "preserve_detail": True
                } if _should_preserve_detail(
                    points,
                    feature_class=rel_type,
                    closed=is_closed,
                    radius_m=radius_m,
                    detail_profile=detail_profile,
                ) else {}),
            })

        # Emit inner rings as land islands so the loader can label them.
        assembled_inner = _assemble_multipolygon(
            inner_ways,
            center_lat,
            center_lon,
            nodes,
            0.0 if preserve_exact_linework else simplify_epsilon,
        )
        island_index = 0
        for points, is_closed in assembled_inner:
            if not is_closed or len(points) < 3:
                continue
            if not any(math.hypot(p[0], p[1]) <= radius_m * 1.2 for p in points):
                continue
            if _polygon_area_xy(points) < 150.0:
                continue  # sub-radar-cell islet; noise at export scale
            island_index += 1
            base = rel_name or f"{rel_type}_{el['id']}"
            results.append({
                "id": f"relation_{el['id']}_inner{island_index}",
                "name": f"{base} island {island_index}",
                "points": [{"x": round(p[0], 1), "y": round(p[1], 1)} for p in points],
                "closed": True,
                "feature_class": "small_land_feature",
                "preserve_detail": True,
            })

    # Process standalone ways (not part of any relation)
    for way_id, wd in way_data.items():
        if way_id in relation_way_ids:
            continue  # Skip - already part of a relation

        way_nodes = wd['nodes']
        tags = wd['tags']
        name = tags.get("name", "")
        water_type = _feature_class_from_tags(tags, tags.get("water", tags.get("natural", "shoreline")))
        is_structure = water_type in _DETAIL_STRUCTURE_CLASSES

        raw_points = []
        for nid in way_nodes:
            if nid in nodes:
                lat, lon = nodes[nid]
                x, y = latlon_to_xy(lat, lon, center_lat, center_lon)
                raw_points.append((x, y))
        # Piers/breakwaters are legitimately mapped as 2-node line stubs.
        min_points = 2 if is_structure else 3
        if len(raw_points) < min_points:
            continue

        # Check if closed
        closed = (way_nodes[0] == way_nodes[-1]) and len(way_nodes) > 3

        # Simplify. Structures and wetland edges hold radar-scale detail, so
        # they always use a tight tolerance regardless of the scene epsilon.
        if preserve_exact_linework:
            effective_epsilon = 0.0
        elif water_type in _PASSTHROUGH_CLASSES:
            effective_epsilon = min(simplify_epsilon, 3.0)
        else:
            effective_epsilon = simplify_epsilon
        simplified = _douglas_peucker(raw_points, effective_epsilon)
        if len(simplified) < min_points:
            continue

        # Filter: keep only features that have points within range
        in_range = any(
            math.sqrt(p[0]**2 + p[1]**2) <= radius_m * 1.2
            for p in simplified
        )
        if not in_range:
            continue

        if _skip_trivial_water_feature(tags, name, simplified, closed=closed):
            continue

        results.append({
            "id": f"way_{way_id}",
            "name": name or f"{water_type}_{way_id}",
            "points": [{"x": round(p[0], 1), "y": round(p[1], 1)} for p in simplified],
            "closed": closed,
            "feature_class": water_type,
            **({
                "preserve_detail": True
            } if water_type in _PASSTHROUGH_CLASSES or _should_preserve_detail(
                simplified,
                feature_class=water_type,
                closed=closed,
                radius_m=radius_m,
                detail_profile=detail_profile,
            ) else {}),
        })

    merged = results if preserve_exact_linework else _merge_open_feature_geometries(
        results,
        simplify_epsilon=simplify_epsilon,
        radius_m=radius_m,
    )
    if detail_profile == "harbor_tidal" and preserve_exact_linework:
        # Exact harbor exports need every queried boundary fragment available
        # during closure. Pruning first can remove the segment that a dangling
        # OSM way is supposed to meet and leave an artificial land-fill leak.
        resolved, _ = _resolve_harbor_topology(merged, radius_m=radius_m)
        return resolved
    if detail_profile == "harbor_tidal":
        selected = _prune_origin_connected_harbor_network(
            merged,
            radius_m=radius_m,
            simplify_epsilon=simplify_epsilon,
        )
        resolved, _ = _resolve_harbor_topology(selected, radius_m=radius_m)
        return resolved
    # Default-profile coastal scenes still need resolved shoreline topology:
    # dangling open fragments give the loader no side information, and its
    # flood-fill classifier then mislabels large water areas. Resolution only
    # touches shore-boundary classes, so lake exports pass through unchanged.
    has_open_shore = any(
        not feature.get("closed", False)
        and _normalize_feature_class(feature.get("feature_class", "")) in _SHORE_BOUNDARY_CLASSES
        for feature in merged
    )
    if has_open_shore:
        resolved, _ = _resolve_harbor_topology(merged, radius_m=radius_m)
        return resolved
    return merged
