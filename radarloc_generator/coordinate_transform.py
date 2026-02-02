"""Lat/lon to local tangent plane (X/Y meters) conversion.

Uses equirectangular projection, accurate within ~50 km of origin.
"""
import math

METERS_PER_DEGREE = 111132.954


def latlon_to_xy(lat: float, lon: float, origin_lat: float, origin_lon: float) -> tuple:
    """Convert lat/lon to local X/Y meters relative to origin.

    X = East positive, Y = North positive.
    """
    x = (lon - origin_lon) * METERS_PER_DEGREE * math.cos(math.radians(origin_lat))
    y = (lat - origin_lat) * METERS_PER_DEGREE
    return (x, y)


def xy_to_latlon(x: float, y: float, origin_lat: float, origin_lon: float) -> tuple:
    """Convert local X/Y meters back to lat/lon."""
    lat = origin_lat + y / METERS_PER_DEGREE
    lon = origin_lon + x / (METERS_PER_DEGREE * math.cos(math.radians(origin_lat)))
    return (lat, lon)


def nm_to_meters(nm: float) -> float:
    """Convert nautical miles to meters."""
    return nm * 1852.0
