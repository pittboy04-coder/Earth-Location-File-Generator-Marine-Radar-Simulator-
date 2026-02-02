"""Geocoding via Nominatim (free OpenStreetMap geocoder)."""
import time
import requests

_LAST_REQUEST_TIME = 0.0
_USER_AGENT = "MarineRadarLocationGenerator/1.0"


def _rate_limit():
    """Enforce 1 request/sec rate limit for Nominatim."""
    global _LAST_REQUEST_TIME
    elapsed = time.time() - _LAST_REQUEST_TIME
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _LAST_REQUEST_TIME = time.time()


def geocode(location_name: str) -> dict:
    """Convert a location name to lat/lon coordinates.

    Args:
        location_name: Place name, e.g. "Lake Murray, South Carolina"

    Returns:
        dict with keys: lat, lon, display_name

    Raises:
        ValueError: If location not found.
        requests.RequestException: On network error.
    """
    _rate_limit()
    resp = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": location_name, "format": "json", "limit": 1},
        headers={"User-Agent": _USER_AGENT},
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise ValueError(f"Location not found: {location_name}")
    r = results[0]
    return {
        "lat": float(r["lat"]),
        "lon": float(r["lon"]),
        "display_name": r.get("display_name", location_name),
    }
