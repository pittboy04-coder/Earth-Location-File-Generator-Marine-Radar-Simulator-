"""Assemble and save .radarloc JSON files."""
import json
import math
from datetime import datetime, timezone

_WATER_FILL_CLASSES = {
    "water", "river", "pond", "lake", "basin", "reservoir",
    "canal", "stream", "harbour", "harbor", "lagoon", "bay", "fairway",
    "dock", "strait",
}
_CHANNEL_CLASSES = {"river", "canal", "fairway", "strait", "dock"}
_STRONG_LAND_CLASSES = {
    "small_land_feature", "pier", "breakwater", "groyne", "quay", "jetty",
}
_WETLAND_LAND_CLASSES = {
    "wetland", "marsh", "saltmarsh", "tidalflat", "reedbed", "swamp",
    "wet_meadow", "mangrove", "bog", "fen",
}


def _feature_xy(feature: dict) -> list:
    return [
        (float(p["x"]), float(p["y"])) if isinstance(p, dict) else (float(p[0]), float(p[1]))
        for p in feature.get("points", [])
    ]


def _close_chain_along_ring(points: list, radius_m: float) -> list | None:
    """Close an open shoreline chain along the range circle, water enclosed.

    OSM coastline ways are directed with water on the RIGHT of travel. The
    chain's two endpoints sit on (or near) the range ring after clipping; the
    returns BOTH candidate polygons (chain + each arc) with a clockwise flag,
    clockwise-first. Empty list when the chain doesn't reach the ring.
    """
    if len(points) < 2:
        return []
    start, end = points[0], points[-1]
    ring_tol = max(8.0, radius_m * 0.01)
    if (abs(math.hypot(*start) - radius_m) > ring_tol
            or abs(math.hypot(*end) - radius_m) > ring_tol):
        return []

    a_end = math.atan2(end[1], end[0])
    a_start = math.atan2(start[1], start[0])

    def arc(direction: int) -> list:
        # direction +1 = CCW from end angle to start angle
        span = (a_start - a_end) * direction
        while span <= 0.0:
            span += 2.0 * math.pi
        steps = max(8, int(span / math.radians(3.0)))
        return [
            (radius_m * math.cos(a_end + direction * span * i / steps),
             radius_m * math.sin(a_end + direction * span * i / steps))
            for i in range(1, steps)
        ]

    # OSM coastline direction: water on the RIGHT of travel. A polygon whose
    # interior lies right of its traversal is CLOCKWISE, i.e. negative signed
    # area (y-up). Choosing the arc by global orientation is robust to local
    # shoreline bends, unlike a single midpoint side test.
    def signed_area(poly: list) -> float:
        total = 0.0
        for i in range(len(poly)):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % len(poly)]
            total += x1 * y2 - x2 * y1
        return total * 0.5

    out = []
    for direction in (1, -1):
        poly = list(points) + arc(direction)
        out.append((poly, signed_area(poly) < 0.0))
    # Order clockwise-first so a caller taking the head follows OSM convention.
    out.sort(key=lambda t: 0 if t[1] else 1)
    return out


def build_land_water_raster(coastlines: list, range_nm: float,
                            grid_size: int = 768,
                            land_height_m: float = 13.0) -> dict | None:
    """Rasterize the vector scene into an authoritative land/water grid.

    Everything defaults to LAND; only mapped water carves it: sea polygons
    (directed coastline chains closed along the range ring) and closed
    water-fill polygons. Strong land rings (islands, piers) then override
    water; wetland rings override water except where a navigation channel is
    mapped. No flood-fill guessing is involved, so unmapped inland areas stay
    land -- matching how reference charts read.
    """
    try:
        import numpy as np
        from matplotlib.path import Path
    except ImportError:
        return None

    range_m = range_nm * 1852.0
    cell = (2.0 * range_m) / grid_size
    ys, xs = np.mgrid[0:grid_size, 0:grid_size]
    px = (-range_m + (xs + 0.5) * cell).ravel()
    py = (-range_m + (ys + 0.5) * cell).ravel()
    pts = np.column_stack([px, py])

    water = np.zeros(grid_size * grid_size, dtype=bool)
    strong_land = np.zeros_like(water)
    wetland_land = np.zeros_like(water)
    channel_water = np.zeros_like(water)

    def fill(poly: list) -> "np.ndarray":
        xs_p = [p[0] for p in poly]
        ys_p = [p[1] for p in poly]
        box = (np.min(xs_p) - cell, np.max(xs_p) + cell,
               np.min(ys_p) - cell, np.max(ys_p) + cell)
        sel = (px >= box[0]) & (px <= box[1]) & (py >= box[2]) & (py <= box[3])
        out = np.zeros_like(water)
        if np.any(sel):
            out[sel] = Path(poly).contains_points(pts[sel])
        return out

    # The export leaves the mainland shoreline as several snapped fragments.
    # Stitch open coastline/shoreline chains end-to-end (their endpoints were
    # already snapped to shared meeting points) before ring closure.
    open_chains = []
    for feature in coastlines:
        cls = str(feature.get("feature_class", "") or "").strip().lower()
        if not bool(feature.get("closed", False)) and cls in {"coastline", "shoreline"}:
            xy = _feature_xy(feature)
            if len(xy) >= 2:
                open_chains.append(xy)

    stitch_tol = max(10.0, range_m * 0.003)
    merged_any = True
    while merged_any and len(open_chains) > 1:
        merged_any = False
        for i in range(len(open_chains)):
            for j in range(i + 1, len(open_chains)):
                a, b = open_chains[i], open_chains[j]
                # Direction-preserving joins first: reversing a directed
                # coastline fragment flips its land/water sides and breaks
                # the ring-closure orientation choice downstream.
                fwd = (
                    ("ee", math.dist(a[-1], b[0])),   # a end -> b start
                    ("ss", math.dist(a[0], b[-1])),   # b end -> a start
                )
                rev = (
                    ("es", math.dist(a[-1], b[-1])),  # a end -> b end (rev b)
                    ("se", math.dist(a[0], b[0])),    # a start -> b start (rev a)
                )
                # Same-direction bank fragments legitimately sit hundreds of
                # metres apart (creek mouths interrupt the mapped bank); a
                # forward join across such a gap walls the basin flood off the
                # peninsula behind it. Reversal joins stay conservative.
                fwd_tol = max(stitch_tol, 220.0)
                kind, dist = min(fwd, key=lambda t: t[1])
                if dist > fwd_tol:
                    kind, dist = min(rev, key=lambda t: t[1])
                    if dist > stitch_tol:
                        continue
                elif dist > stitch_tol and kind not in ("ee", "ss"):
                    continue
                if kind == "ee":
                    merged = a + b
                elif kind == "es":
                    merged = a + list(reversed(b))
                elif kind == "se":
                    merged = list(reversed(a)) + b
                else:
                    merged = b + a
                open_chains[i] = merged
                del open_chains[j]
                merged_any = True
                break
            if merged_any:
                break

    # Chains that reach the ring at only one end pair up across water gaps
    # (e.g. the two shores of a harbor entrance): joining their inland ends
    # yields a chain with both ends on the ring, closable along the ocean arc.
    ring_tol = max(8.0, range_m * 0.01)

    def ring_ends(chain):
        return (abs(math.hypot(*chain[0]) - range_m) <= ring_tol,
                abs(math.hypot(*chain[-1]) - range_m) <= ring_tol)

    entrance_radii: list = []
    singles = [c for c in open_chains if sum(ring_ends(c)) == 1]
    others = [c for c in open_chains if sum(ring_ends(c)) != 1]
    paired_chains: list = []
    while len(singles) > 1:
        base = singles.pop(0)
        s_ring, e_ring = ring_ends(base)
        inland = base[0] if not s_ring else base[-1]
        best = min(
            range(len(singles)),
            key=lambda k: min(math.dist(inland, singles[k][0]),
                              math.dist(inland, singles[k][-1])),
        )
        partner = singles.pop(best)
        p_start_d = math.dist(inland, partner[0])
        p_end_d = math.dist(inland, partner[-1])
        if p_end_d < p_start_d:
            partner = list(reversed(partner))
        # base runs ring -> inland; partner runs inland -> its own ring end.
        base_rti = base if s_ring else list(reversed(base))
        paired_chains.append(base_rti + partner)
        entrance_radii.append(max(math.hypot(*inland),
                                  math.hypot(*partner[0])))
    others.extend(singles)

    sea_count = 0
    for chain, is_paired in ([(c, False) for c in others]
                             + [(c, True) for c in paired_chains]):
        candidates = _close_chain_along_ring(chain, range_m)
        if not candidates:
            continue
        if is_paired:
            # An entrance-paired chain separates the outer sea from the
            # own-ship basin, so the sea polygon must NOT contain the origin
            # (the basin flood claims that side). Deterministic, unlike
            # voting on possibly direction-mixed fragments.
            from matplotlib.path import Path as _P0
            keep = [poly for poly, _cw in candidates
                    if not _P0(poly).contains_point((0.0, 0.0))]
            if keep:
                water |= fill(keep[0])
                sea_count += 1
                continue
        if len(candidates) == 1:
            water |= fill(candidates[0][0])
            sea_count += 1
            continue
        # Export-time merging can reverse individual fragments, so a single
        # midpoint side test is unreliable -- but the MAJORITY of the chain's
        # length still flows with water on the right (OSM convention). Vote:
        # sample right-hand offsets along the chain, pick the candidate arc
        # whose polygon contains most of them.
        from matplotlib.path import Path as _P
        samples = []
        step = max(1, len(chain) // 200)
        for k in range(0, len(chain) - 1, step):
            x1, y1 = chain[k]
            x2, y2 = chain[k + 1]
            dx, dy = x2 - x1, y2 - y1
            seg = math.hypot(dx, dy)
            if seg < 1e-6:
                continue
            off = max(2.0 * cell, 20.0)
            samples.append((
                (x1 + x2) * 0.5 + (dy / seg) * off,
                (y1 + y2) * 0.5 - (dx / seg) * off,
                seg,
            ))
        best = None
        for poly, is_cw in candidates:
            path = _P(poly)
            votes = sum(w for sx, sy, w in samples if path.contains_point((sx, sy)))
            score = (votes, 1 if is_cw else 0)
            if best is None or score > best[0]:
                best = (score, poly)
        water |= fill(best[1])
        sea_count += 1

    for feature in coastlines:
        cls = str(feature.get("feature_class", "") or "").strip().lower()
        xy = _feature_xy(feature)
        if len(xy) < 3:
            continue
        closed = bool(feature.get("closed", False))
        if not closed:
            pass  # open chains handled above
        elif closed and cls in _WATER_FILL_CLASSES:
            mask = fill(xy)
            water |= mask
            if cls in _CHANNEL_CLASSES:
                channel_water |= mask
        elif closed and cls == "small_land_feature":
            strong_land |= fill(xy)
        elif closed and cls in _STRONG_LAND_CLASSES:
            pass  # pier/dock platforms: echo features standing over water,
                  # not terrain -- the scene keeps water beneath them
        elif closed and cls in _WETLAND_LAND_CLASSES:
            # Small wetland rings (an islet's tidal-flat skirt, e.g. Fort
            # Sumter) outrank mapped channels; broad marsh sheets do not.
            area = 0.0
            for k in range(len(xy)):
                x1, y1 = xy[k]
                x2, y2 = xy[(k + 1) % len(xy)]
                area += x1 * y2 - x2 * y1
            ring_area = abs(area) * 0.5
            if ring_area <= 15_000.0:
                pass  # sub-raster marsh dot: noise at radar scale, skip
            elif ring_area <= 400_000.0:
                strong_land |= fill(xy)
            else:
                wetland_land |= fill(xy)
        elif closed and cls in {"coastline", "shoreline"}:
            # A closed OSM coastline/shoreline ring is an island: land inside.
            strong_land |= fill(xy)

    # Dangling shoreline endpoints (an unmapped seawall or creek-mouth gap)
    # let the basin flood round the end of a wall. Connect each dangling end
    # to the nearest closed water-polygon perimeter point within reach: at a
    # creek mouth the connector spans the gap (and is later peeled where
    # water sits on both sides); at an unmapped seawall it seals the tip.
    water_poly_edges = []
    for feature in coastlines:
        if not bool(feature.get("closed", False)):
            continue
        cls_e = str(feature.get("feature_class", "") or "").strip().lower()
        if cls_e in _WATER_FILL_CLASSES:
            xy_e = _feature_xy(feature)
            step_e = max(1, len(xy_e) // 400)
            water_poly_edges.extend(xy_e[::step_e])
    connectors = []
    if water_poly_edges:
        for chain in others:
            for endpoint in (chain[0], chain[-1]):
                if abs(math.hypot(*endpoint) - range_m) <= ring_tol:
                    continue
                best_pt = min(water_poly_edges,
                              key=lambda q: math.dist(endpoint, q))
                if math.dist(endpoint, best_pt) <= 600.0:
                    connectors.append([endpoint, best_pt])

    # The open basin around the own ship (e.g. Charleston Harbor itself) often
    # has no OSM water polygon -- it is bounded by shoreline chains. Flood it
    # from the origin, walled by every feature edge, and add it to water.
    barrier = np.zeros((grid_size, grid_size), dtype=bool)

    def draw_edges(xy: list, close: bool) -> None:
        n = len(xy)
        for k in range(n - (0 if close else 1)):
            x1, y1 = xy[k]
            x2, y2 = xy[(k + 1) % n]
            steps = max(1, int(math.hypot(x2 - x1, y2 - y1) / (cell * 0.5)))
            for s in range(steps + 1):
                t = s / steps
                cx = int((x1 + (x2 - x1) * t + range_m) / cell)
                cy = int((y1 + (y2 - y1) * t + range_m) / cell)
                if 0 <= cx < grid_size and 0 <= cy < grid_size:
                    barrier[cy, cx] = True

    for feature in coastlines:
        xy = _feature_xy(feature)
        if len(xy) < 2:
            continue
        closed_f = bool(feature.get("closed", False))
        cls_f = str(feature.get("feature_class", "") or "").strip().lower()
        if not closed_f and cls_f not in {
            "coastline", "shoreline", "riverbank",
            "quay", "breakwater", "groyne", "jetty",
        }:
            # Open centerlines (stream/river/canal) ARE waterways; walling
            # them would plug the very creeks they map, and piers stand on
            # piles over water. Solid bank/structure lines DO wall the flood
            # (a quay seawall is exactly the land/water boundary).
            continue
        draw_edges(xy, close=closed_f)
    connector_mark = np.zeros((grid_size, grid_size), dtype=bool)
    if connectors:
        saved = barrier.copy()
        for connector in connectors:
            draw_edges(connector, close=False)
        connector_mark = barrier & ~saved

    from collections import deque
    # The flood must stay inside the basin: mapped water, land rings, and all
    # drawn edges wall it in, so it cannot escape upstream through a river
    # polygon or across an unmapped marsh.
    block = (barrier
             | water.reshape(grid_size, grid_size)
             | strong_land.reshape(grid_size, grid_size)
             | wetland_land.reshape(grid_size, grid_size))
    oc = grid_size // 2
    # Leak control: unmapped shoreline gaps can let the basin flood escape
    # across half the scene. Progressively thicken the walls; accept the first
    # flood whose area is plausible for an own-ship basin, else skip it.
    # Own-ship basin: polar line-of-sight fill. From the origin, march each
    # bearing outward and mark water until the ray crosses any feature edge
    # (bank chain, water-polygon perimeter, connector). This mirrors what the
    # radar itself can see as open water: mapped rivers take over past their
    # perimeter, and unmapped corridors BEHIND a shoreline (e.g. a peninsula
    # whose banks are only mapped as river-polygon edges) are never flooded --
    # the failure mode of a connectivity flood.
    oc = grid_size // 2
    if not block[oc, oc]:
        n_bearings = 4096
        max_steps = grid_size  # out to the corner
        seen = np.zeros((grid_size, grid_size), dtype=bool)
        for bi in range(n_bearings):
            ang = 2.0 * math.pi * bi / n_bearings
            dx = math.cos(ang)
            dy = math.sin(ang)
            for step in range(1, max_steps):
                rr = oc + int(round(dy * step * 0.5))
                cc = oc + int(round(dx * step * 0.5))
                if not (0 <= rr < grid_size and 0 <= cc < grid_size):
                    break
                if block[rr, cc]:
                    break
                seen[rr, cc] = True
        # close pinholes between adjacent rays
        grown = seen.copy()
        grown[1:, :] |= seen[:-1, :]
        grown[:-1, :] |= seen[1:, :]
        grown[:, 1:] |= seen[:, :-1]
        grown[:, :-1] |= seen[:, 1:]
        seen = grown & ~block
        water |= seen.ravel()

    if not np.any(water):
        return None  # nothing mapped as water; let the loader infer instead

    water &= ~strong_land
    water &= ~(wetland_land & ~channel_water)

    # Beyond the range ring nothing is classified (sea polygons stop at the
    # ring), which used to leave default-land triangles in open-ocean corners
    # of the scene view. Extend each bearing's just-inside-ring value outward.
    rad = np.hypot(px, py)
    outside = rad > range_m * 0.985
    if np.any(outside):
        scale = (range_m * 0.97) / np.maximum(rad[outside], 1.0)
        sx_i = np.clip(((px[outside] * scale + range_m) / cell).astype(np.int64), 0, grid_size - 1)
        sy_i = np.clip(((py[outside] * scale + range_m) / cell).astype(np.int64), 0, grid_size - 1)
        w2d = water.reshape(grid_size, grid_size)
        water[np.flatnonzero(outside)] = w2d[sy_i, sx_i]

    # Connector walls are synthetic geometry: where they cross open water
    # (a creek mouth span) they must not read as land; where they seal an
    # unmapped seawall they stay land. Decide by local water majority.
    if np.any(connector_mark):
        w2m = water.reshape(grid_size, grid_size)
        pad = 3
        acc = np.zeros((grid_size, grid_size), dtype=np.int32)
        cnt = 0
        for dr in range(-pad, pad + 1):
            for dc in range(-pad, pad + 1):
                acc += np.roll(np.roll(w2m.astype(np.int32), dr, 0), dc, 1)
                cnt += 1
        majority_water = acc > (cnt * 0.60)
        water |= (connector_mark & majority_water).ravel()

    # Peel one-cell land filaments strung across water (e.g. the sea-polygon
    # closure seam across a harbor entrance). Real land is thicker or part of
    # a strong ring; a 1-cell line with water on both sides is an artifact.
    w2 = water.reshape(grid_size, grid_size)
    sl2 = strong_land.reshape(grid_size, grid_size)
    for _ in range(3):
        landish = ~w2 & ~sl2
        h = np.zeros_like(landish)
        h[:, 1:-1] = w2[:, :-2] & w2[:, 2:]
        v = np.zeros_like(landish)
        v[1:-1, :] = w2[:-2, :] & w2[2:, :]
        filament = landish & (h | v)
        if not np.any(filament):
            break
        w2 |= filament
    water = w2.ravel()

    # Wetlands are land, but at a low encoded elevation so displays can shade
    # marsh differently from firm ground (and it survives the >0.5 land test).
    wet_final = (wetland_land & ~water & ~strong_land)
    elev = np.where(water, 0.0, np.where(wet_final, 5.0, land_height_m))
    grid = elev.reshape(grid_size, grid_size)
    return {
        "origin_x": -range_m,
        "origin_y": -range_m,
        "rows": grid_size,
        "cols": grid_size,
        "cell_size": cell,
        "elevations": [[round(float(v), 1) for v in row] for row in grid],
        "data_source": "vector_rasterization",
        "sea_polygons": sea_count,
    }


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

    unresolved_endpoints = int(doc.get('metadata', {}).get(
        'topology_unresolved_endpoint_count',
        sum(len(c.get('topology_unresolved_endpoints', [])) for c in coastlines),
    ) or 0)
    result['stats']['topology_unresolved_endpoints'] = unresolved_endpoints
    if unresolved_endpoints:
        result['warnings'].append(
            f'{unresolved_endpoints} unresolved shoreline endpoint(s) remain; '
            'harbor land/water fill is not topology-safe'
        )
        result['valid'] = False

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


def _point_in_polygon_xy(px: float, py: float, points: list[dict]) -> bool:
    inside = False
    j = len(points) - 1
    for i in range(len(points)):
        xi = float(points[i]["x"])
        yi = float(points[i]["y"])
        xj = float(points[j]["x"])
        yj = float(points[j]["y"])
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-9) + xi):
            inside = not inside
        j = i
    return inside


def _distance_point_to_segment(px: float, py: float,
                               ax: float, ay: float,
                               bx: float, by: float) -> float:
    dx = bx - ax
    dy = by - ay
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    qx = ax + t * dx
    qy = ay + t * dy
    return math.hypot(px - qx, py - qy)


def _nearest_shore_distance_m(coastlines: list) -> float | None:
    nearest = None
    for coast in coastlines:
        pts = coast.get("points", [])
        if len(pts) < 2:
            continue
        for i in range(len(pts) - 1):
            ax = float(pts[i]["x"])
            ay = float(pts[i]["y"])
            bx = float(pts[i + 1]["x"])
            by = float(pts[i + 1]["y"])
            dist_m = _distance_point_to_segment(0.0, 0.0, ax, ay, bx, by)
            if nearest is None or dist_m < nearest:
                nearest = dist_m
    return nearest


def _infer_origin_context(coastlines: list, range_nm: float) -> dict:
    """Infer how the own-ship origin sits relative to exported shoreline data."""
    closed_features = [c for c in coastlines if c.get("closed") and len(c.get("points", [])) >= 3]
    open_features = [c for c in coastlines if not c.get("closed") and len(c.get("points", [])) >= 2]
    nearest_shore_m = _nearest_shore_distance_m(coastlines)
    range_m = range_nm * 1852.0
    topology_unresolved = sum(
        len(feature.get("topology_unresolved_endpoints", []))
        for feature in coastlines
    )
    topology_extensions = sum(
        len(feature.get("topology_extensions", []))
        for feature in coastlines
    )
    topology_clipped = sum(
        1 for feature in coastlines
        if feature.get("topology_range_clipped", False)
    )

    if any(_point_in_polygon_xy(0.0, 0.0, feature["points"]) for feature in closed_features):
        origin_surface = "water"
        origin_source = "inside_closed_water_polygon"
        scene_topology = "enclosed_water"
    elif open_features:
        # Open shoreline chains are typical harbor / coastal exports where the
        # own-ship origin is intended to sit on navigable water.
        origin_surface = "water"
        origin_source = "open_shoreline_inference"
        scene_topology = "open_shore"
    elif closed_features:
        origin_surface = "land"
        origin_source = "outside_closed_water_polygons"
        scene_topology = "enclosed_water"
    else:
        origin_surface = "unknown"
        origin_source = "insufficient_geometry"
        scene_topology = "sparse"

    return {
        "origin_surface": origin_surface,
        "origin_surface_source": origin_source,
        "scene_topology": scene_topology,
        "nearest_shore_m": round(nearest_shore_m, 1) if nearest_shore_m is not None else None,
        "closed_feature_count": len(closed_features),
        "open_feature_count": len(open_features),
        "origin_near_shore": bool(nearest_shore_m is not None and nearest_shore_m <= max(40.0, range_m * 0.12)),
        "topology_unresolved_endpoint_count": topology_unresolved,
        "topology_extension_count": topology_extensions,
        "topology_range_clipped_feature_count": topology_clipped,
        "topology_audit_passed": topology_unresolved == 0,
    }


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
    origin_context = _infer_origin_context(coastlines, range_nm)
    raster_authoritative = False
    if terrain is None:
        raster = build_land_water_raster(coastlines, range_nm)
        if raster is not None:
            terrain = raster
            raster_authoritative = True
    doc = {
        "version": "1.0",
        "metadata": {
            "location_name": location_name,
            "center_lat": round(center_lat, 6),
            "center_lon": round(center_lon, 6),
            "range_nm": range_nm,
            "generated_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **origin_context,
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

    if raster_authoritative:
        doc["metadata"]["terrain_authoritative"] = True
        doc["terrain"]["authoritative"] = True
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
