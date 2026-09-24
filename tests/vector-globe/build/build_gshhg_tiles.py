#!/usr/bin/env python3
"""Build GVB1 vector-globe tiles directly from processed GSHHG Shapefiles.

GSHHG already ships five native resolutions:
  lod0=c (crude), lod1=l (low), lod2=i (intermediate), lod3=h (high), lod4=f (full)

Each tile is composed as L1 land - L2 lakes + L3 lake islands - L4 ponds.
Antarctica uses L5 ice-front polygons by default, with L6 grounding-line available.

For global builds, each hierarchy level is indexed with Shapely STRtree so a tile
queries only intersecting polygons instead of scanning the entire world dataset.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

import fiona
from pyproj import Transformer
from shapely import make_valid
from shapely.geometry import Polygon, box, shape
from shapely.ops import transform as geometry_transform, unary_union
from shapely.strtree import STRtree

from build_tiles import encode_tile, polygon_parts, tile_name, tile_origins

GSHHG_VERSION = "2.3.7"
DEFAULT_RESOLUTIONS = "c,l,i,h,f"
RESOLUTION_NAMES = {
    "c": "crude",
    "l": "low",
    "i": "intermediate",
    "h": "high",
    "f": "full",
}
SHAPE_RE = re.compile(r"^GSHHS_([clihf])_L([1-6])\.shp$", re.IGNORECASE)

WGS84_TO_ANTARCTIC = Transformer.from_crs("EPSG:4326", "EPSG:3031", always_xy=True)
ANTARCTIC_TO_WGS84 = Transformer.from_crs("EPSG:3031", "EPSG:4326", always_xy=True)


def safe_extract_zip(archive_path: Path, output_dir: Path) -> None:
    marker = output_dir / ".complete"
    if marker.is_file():
        return
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    root = output_dir.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            destination = (output_dir / member.filename).resolve()
            if root != destination and root not in destination.parents:
                raise RuntimeError(f"unsafe ZIP member: {member.filename}")
        archive.extractall(output_dir)
    marker.write_text("ok\n", encoding="utf-8")


def discover_layers(root: Path) -> dict[str, dict[int, Path]]:
    found: dict[str, dict[int, Path]] = {key: {} for key in RESOLUTION_NAMES}
    for path in root.rglob("*.shp"):
        match = SHAPE_RE.match(path.name)
        if not match:
            continue
        resolution_code = match.group(1).lower()
        hierarchy_level = int(match.group(2))
        found[resolution_code][hierarchy_level] = path
    return found


def valid_geometry(geometry):
    if geometry.is_empty or geometry.is_valid:
        return geometry
    return geometry.buffer(0)


def read_level_polygons(path: Path, bounds: tuple[float, float, float, float]):
    build_box = box(*bounds)
    geometries = []
    with fiona.open(path) as collection:
        for feature in collection.filter(bbox=bounds):
            raw_geometry = feature.get("geometry")
            if not raw_geometry:
                continue
            geometry = valid_geometry(shape(raw_geometry))
            if geometry.is_empty or not geometry.intersects(build_box):
                continue
            clipped = valid_geometry(geometry.intersection(build_box))
            if not clipped.is_empty:
                geometries.append(clipped)
    return geometries


def read_antarctica_polar(path: Path):
    """Read Antarctica in EPSG:3031 before repairing its dateline-split polygons.

    GSHHG's shapefile distribution splits the polar mainland into east/west
    Cartesian polygons.  Repairing those directly in lon/lat can discard one
    side of the polar cap.  In a polar projection the two pieces share the
    South Pole normally and can be made valid and unioned safely.
    """
    projected = []
    with fiona.open(path) as collection:
        for feature in collection:
            raw_geometry = feature.get("geometry")
            if not raw_geometry:
                continue
            geometry = shape(raw_geometry)
            geometry = geometry_transform(WGS84_TO_ANTARCTIC.transform, geometry)
            geometry = make_valid(geometry)
            if not geometry.is_empty:
                projected.append(geometry)
    if not projected:
        return None
    return make_valid(unary_union(projected))


def dense_tile_polygon(bounds: tuple[float, float, float, float], step: float = 0.5):
    lon0, lat0, lon1, lat1 = bounds

    def samples(start: float, stop: float):
        distance = abs(stop - start)
        count = max(1, int(round(distance / step)))
        return [start + (stop - start) * i / count for i in range(count + 1)]

    west = [(lon0, lat) for lat in samples(lat0, lat1)]
    north = [(lon, lat1) for lon in samples(lon0, lon1)[1:]]
    east = [(lon1, lat) for lat in samples(lat1, lat0)[1:]]
    south = [(lon, lat0) for lon in samples(lon1, lon0)[1:]]
    return Polygon(west + north + east + south)


def normalize_polar_tile_longitudes(geometry, bounds):
    lon0, _, lon1, _ = bounds
    center = (lon0 + lon1) * 0.5

    def normalize_one(lon: float, lat: float) -> float:
        if lat <= -89.999999:
            return center
        while lon - center > 180:
            lon -= 360
        while lon - center < -180:
            lon += 360
        # pyproj may return the opposite representation exactly at the
        # antimeridian.  Pick the equivalent longitude inside this tile.
        candidates = (lon - 360, lon, lon + 360)
        lon = min(candidates, key=lambda value: abs(value - center))
        if lon < lon0 and lon0 == -180:
            lon += 360
        if lon > lon1 and lon1 == 180:
            lon -= 360
        return min(lon1, max(lon0, lon))

    def transform_xy(x, y, z=None):
        try:
            xx = [normalize_one(float(lon), float(lat)) for lon, lat in zip(x, y)]
            if z is None:
                return xx, y
            return xx, y, z
        except TypeError:
            xx = normalize_one(float(x), float(y))
            if z is None:
                return xx, y
            return xx, y, z

    return geometry_transform(transform_xy, geometry)


@dataclass
class PolarAntarcticaLevel:
    geometry: object | None

    @classmethod
    def from_path(cls, path: Path):
        return cls(read_antarctica_polar(path))

    def clipped(self, bounds: tuple[float, float, float, float]):
        if self.geometry is None or bounds[1] >= -55:
            return []

        # Adjacent polar tiles are clipped in EPSG:3031 and then transformed
        # back to lon/lat.  Floating-point reprojection can otherwise leave a
        # sub-pixel gap along a shared meridian that becomes visible at 1000%.
        # Clip with a tiny overlap, then quantization clamps both tiles back to
        # their nominal GVB1 bounds.  Overlap is harmless because land is
        # rendered opaquely, while gaps are visible as ocean slivers.
        overlap = 0.05
        lon0, lat0, lon1, lat1 = bounds
        expanded_bounds = (
            lon0 if lon0 <= -180 else lon0 - overlap,
            lat0 if lat0 <= -90 else lat0 - overlap,
            lon1 if lon1 >= 180 else lon1 + overlap,
            lat1 if lat1 >= 90 else lat1 + overlap,
        )

        tile_wgs84 = dense_tile_polygon(expanded_bounds, step=0.25)
        tile_polar = geometry_transform(WGS84_TO_ANTARCTIC.transform, tile_wgs84)
        tile_polar = make_valid(tile_polar)
        clipped = make_valid(self.geometry.intersection(tile_polar))
        if clipped.is_empty:
            return []

        clipped_wgs84 = geometry_transform(ANTARCTIC_TO_WGS84.transform, clipped)
        clipped_wgs84 = normalize_polar_tile_longitudes(clipped_wgs84, bounds)
        clipped_wgs84 = make_valid(clipped_wgs84)
        if clipped_wgs84.is_empty:
            return []
        return [clipped_wgs84]


@dataclass
class SpatialLevel:
    geometries: list
    tree: STRtree | None

    @classmethod
    def from_geometries(cls, geometries: list):
        return cls(geometries=geometries, tree=STRtree(geometries) if geometries else None)

    def clipped(self, bounds: tuple[float, float, float, float]):
        if self.tree is None:
            return []
        tile_box = box(*bounds)
        candidate_indexes = self.tree.query(tile_box, predicate="intersects")
        # STRtree query order is not an API guarantee. Sort indexes to keep tile output
        # deterministic and as close as possible to source feature order.
        sorted_indexes = sorted(int(value) for value in candidate_indexes)
        pieces = []
        for geometry_index in sorted_indexes:
            geometry = self.geometries[geometry_index]
            clipped = valid_geometry(geometry.intersection(tile_box))
            if not clipped.is_empty:
                pieces.append(clipped)
        return pieces


def union_or_empty(geometries):
    if not geometries:
        return None
    return valid_geometry(unary_union(geometries))


def compose_land_for_tile(
    indexed_levels: dict[int, SpatialLevel],
    bounds: tuple[float, float, float, float],
    antarctica_level: int,
):
    base_parts = indexed_levels.get(1, SpatialLevel([], None)).clipped(bounds)
    antarctica_index = indexed_levels.get(antarctica_level)
    if antarctica_index is not None:
        base_parts.extend(antarctica_index.clipped(bounds))
    land_geometry = union_or_empty(base_parts)
    if land_geometry is None or land_geometry.is_empty:
        return None

    for hierarchy_level, operation in ((2, "subtract"), (3, "add"), (4, "subtract")):
        level_index = indexed_levels.get(hierarchy_level)
        if level_index is None:
            continue
        modifier = union_or_empty(level_index.clipped(bounds))
        if modifier is None or modifier.is_empty:
            continue
        if operation == "subtract":
            land_geometry = land_geometry.difference(modifier)
        else:
            land_geometry = land_geometry.union(modifier)
        land_geometry = valid_geometry(land_geometry)
        if land_geometry.is_empty:
            return None
    return land_geometry


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--bbox", default="122,24,146,46")
    parser.add_argument("--tile-deg", type=int, default=10)
    parser.add_argument("--resolutions", default=DEFAULT_RESOLUTIONS)
    parser.add_argument("--extract-dir", default=".cache/gshhg/extracted")
    parser.add_argument("--antarctica", choices=("ice-front", "grounding-line"), default="ice-front")
    args = parser.parse_args()

    bbox_values = tuple(float(value) for value in args.bbox.split(","))
    if len(bbox_values) != 4:
        raise ValueError("bbox must be minLon,minLat,maxLon,maxLat")
    min_lon, min_lat, max_lon, max_lat = bbox_values
    if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
        raise ValueError(f"unsupported bbox: {bbox_values}")
    if args.tile_deg <= 0:
        raise ValueError("tile-deg must be positive")

    resolution_codes = [value.strip().lower() for value in args.resolutions.split(",") if value.strip()]
    if not resolution_codes or any(code not in RESOLUTION_NAMES for code in resolution_codes):
        raise ValueError(f"resolutions must be a subset of {','.join(RESOLUTION_NAMES)}")

    archive_path = Path(args.archive).resolve()
    extract_dir = Path(args.extract_dir).resolve()
    output_dir = Path(args.output)
    safe_extract_zip(archive_path, extract_dir)
    discovered = discover_layers(extract_dir)

    for resolution_code in resolution_codes:
        if 1 not in discovered[resolution_code]:
            raise RuntimeError(f"missing GSHHS_{resolution_code}_L1.shp in {archive_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format": "GVB1",
        "version": 1,
        "tileDegrees": args.tile_deg,
        "bbox": bbox_values,
        "source": {
            "name": "GSHHG / GSHHS shoreline polygons",
            "version": GSHHG_VERSION,
            "upstreamRepository": "GenericMappingTools/gshhg-gmt",
            "asset": f"gshhg-shp-{GSHHG_VERSION}.zip",
            "license": "GNU LGPL v3 (see upstream LICENSE)",
            "antarctica": args.antarctica,
        },
        "lods": [],
        "tiles": [],
    }

    tile_bounds = [
        (lon0, lat0, lon0 + args.tile_deg, lat0 + args.tile_deg)
        for lon0, lat0 in tile_origins(bbox_values, args.tile_deg)
    ]
    antarctica_level = 5 if args.antarctica == "ice-front" else 6

    for lod_id, resolution_code in enumerate(resolution_codes):
        levels = discovered[resolution_code]
        manifest["lods"].append({
            "id": lod_id,
            "gshhgResolution": resolution_code,
            "name": RESOLUTION_NAMES[resolution_code],
            "hierarchyLevels": sorted(levels),
        })
        print(f"LOD{lod_id}: {resolution_code} ({RESOLUTION_NAMES[resolution_code]})")

        indexed_levels: dict[int, object] = {}
        for hierarchy_level, level_path in sorted(levels.items()):
            if hierarchy_level not in {1, 2, 3, 4, antarctica_level}:
                continue
            if hierarchy_level == antarctica_level:
                if min_lat < -55:
                    indexed_levels[hierarchy_level] = PolarAntarcticaLevel.from_path(level_path)
                    print(f"  indexed L{hierarchy_level}: polar EPSG:3031 Antarctica geometry")
                continue
            geometries = read_level_polygons(level_path, bbox_values)
            indexed_levels[hierarchy_level] = SpatialLevel.from_geometries(geometries)
            print(f"  indexed L{hierarchy_level}: {len(geometries)} geometry object(s)")

        for bounds in tile_bounds:
            land_geometry = compose_land_for_tile(indexed_levels, bounds, antarctica_level)
            if land_geometry is None or land_geometry.is_empty:
                continue
            polygons = [polygon for polygon in polygon_parts(land_geometry) if polygon.area > 0]
            if not polygons:
                continue

            lon0, lat0, _, _ = bounds
            relative_path = Path(f"lod{lod_id}") / tile_name(lon0, lat0)
            stats = encode_tile(polygons, bounds, output_dir / relative_path)
            if stats is None:
                continue
            manifest["tiles"].append({
                "lod": lod_id,
                "gshhgResolution": resolution_code,
                "path": relative_path.as_posix(),
                "bounds": bounds,
                **stats,
            })
            print(f"  {relative_path}: {stats}")

        # Release one resolution before loading the next full-resolution hierarchy.
        indexed_levels.clear()

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(manifest['tiles'])} tiles to {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
