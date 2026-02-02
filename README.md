# Earth Location File Generator (Marine Radar Simulator)

Generate `.radarloc` location files from real-world coordinates for use with the Marine Radar Simulator.

## What it does

Given a location name or coordinates and a radar range, this tool:

1. Geocodes the location using OpenStreetMap Nominatim
2. Fetches water boundaries (coastlines, lakes, rivers) from the Overpass API
3. Optionally fetches terrain elevation data from Open-Elevation
4. Converts everything to local X/Y meters
5. Outputs a `.radarloc` JSON file

The generated file can be loaded directly into the [Validated Terrain-Occluded Radar Simulation](https://github.com/YOUR_USERNAME/Validated-Terrain-Occluded-Radar-Simulation).

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### By location name

```bash
python generate_location.py "Lake Murray, South Carolina" --range 6 --terrain -o lake_murray.radarloc
```

### By coordinates

```bash
python generate_location.py 34.0818,-81.2169 --range 3 -o lake_murray.radarloc
```

### Options

| Flag | Description |
|------|-------------|
| `--range N` | Radar range in nautical miles (default: 6) |
| `--terrain` | Include elevation data (slower, requires Open-Elevation API) |
| `--terrain-grid N` | Terrain grid resolution (default: 128) |
| `-o FILE` | Output filename |

## .radarloc File Format

JSON file containing:

- **metadata** - Location name, center coordinates, range, timestamp
- **coordinate_system** - Local tangent plane projection parameters
- **coastlines** - Shoreline polygons in local X/Y meters
- **terrain** - Optional elevation grid
- **vessels** - Preset vessel positions (empty by default)

## Data Sources

All data comes from free, open APIs:

- **Nominatim** - OpenStreetMap geocoder (1 req/sec rate limit)
- **Overpass API** - OpenStreetMap data queries for water features
- **Open-Elevation** - SRTM-based elevation data
