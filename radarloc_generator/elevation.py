"""Query Open-Elevation API for terrain height data."""
import numpy as np
import requests
from .coordinate_transform import xy_to_latlon

OPEN_ELEVATION_URL = "https://api.open-elevation.com/api/v1/lookup"
BATCH_SIZE = 100


def query_elevation_grid(origin_lat: float, origin_lon: float,
                         range_m: float, grid_size: int = 128) -> dict:
    """Build an elevation grid covering the area around the origin.

    Args:
        origin_lat, origin_lon: Center of the grid.
        range_m: Half-width of the grid in meters.
        grid_size: Number of rows and columns.

    Returns:
        dict with keys: origin_x, origin_y, rows, cols, cell_size, elevations, data_source
        elevations is a list of lists (row-major, [row][col]).
    """
    cell_size = (2 * range_m) / grid_size
    origin_x = -range_m
    origin_y = -range_m

    # Build list of all grid points as lat/lon
    locations = []
    for r in range(grid_size):
        for c in range(grid_size):
            x = origin_x + c * cell_size
            y = origin_y + r * cell_size
            lat, lon = xy_to_latlon(x, y, origin_lat, origin_lon)
            locations.append({"latitude": lat, "longitude": lon})

    # Query in batches
    elevations_flat = [0.0] * len(locations)
    for i in range(0, len(locations), BATCH_SIZE):
        batch = locations[i:i + BATCH_SIZE]
        try:
            resp = requests.post(
                OPEN_ELEVATION_URL,
                json={"locations": batch},
                timeout=30,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            for j, result in enumerate(results):
                elevations_flat[i + j] = max(0.0, float(result.get("elevation", 0)))
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"  Warning: elevation batch {i // BATCH_SIZE} failed: {e}")
            # Leave as 0.0

    # Reshape to 2D grid
    elevations = []
    for r in range(grid_size):
        row = []
        for c in range(grid_size):
            row.append(round(elevations_flat[r * grid_size + c], 1))
        elevations.append(row)

    return {
        "origin_x": round(origin_x, 1),
        "origin_y": round(origin_y, 1),
        "rows": grid_size,
        "cols": grid_size,
        "cell_size": round(cell_size, 1),
        "elevations": elevations,
        "data_source": "open-elevation",
    }
