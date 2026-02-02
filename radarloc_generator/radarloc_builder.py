"""Assemble and save .radarloc JSON files."""
import json
from datetime import datetime, timezone


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
