#!/usr/bin/env python3
"""Build compact vector-globe land meshes from polygon vector data.

GVB1 format (little endian):
  4s magic = b"GVB1"
  uint32 vertex_count
  uint32 index_count
  vertex_count * (uint16 q_lon, uint16 q_lat)
  index_count * uint32 triangle index

Coordinates are quantized relative to each tile's lon/lat bounds stored in manifest.json.
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import zipfile
from collections import defaultdict
from pathlib import Path

import fiona
import mapbox_earcut as earcut
import numpy as np
from pyproj import CRS, Transformer
from shapely.geometry import Polygon, MultiPolygon, GeometryCollection, box, shape
from shapely.ops import transform as geom_transform

MAGIC = b"GVB1"


def _safe_extract_zip(archive_path: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    root = output_dir.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            target = (output_dir / member.filename).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError(f"Unsafe archive member: {member.filename}")
        archive.extractall(output_dir)


def _expand_archives(root: Path, max_depth: int = 3) -> None:
    seen: set[Path] = set()
    for _ in range(max_depth):
        archives = [
            p for p in root.rglob("*")
            if p.is_file()
            and p not in seen
            and p.suffix.lower() in {".zip", ".mpk", ".mpkx"}
        ]
        if not archives:
            return
        for archive_path in archives:
            seen.add(archive_path)
            expanded_dir = archive_path.with_name(archive_path.name + ".expanded")
            if expanded_dir.exists() and any(expanded_dir.iterdir()):
                continue
            print(f"expanding archive: {archive_path}")
            try:
                _safe_extract_zip(archive_path, expanded_dir)
            except zipfile.BadZipFile:
                print(f"warning: not a ZIP-compatible archive: {archive_path}")


def _discover_vector_datasets(root: Path) -> list[str]:
    datasets: list[str] = []
    for path in sorted(root.rglob("*")):
        suffix = path.suffix.lower()
        if path.is_dir() and suffix == ".gdb":
            datasets.append(str(path.resolve()))
            continue
        if path.is_file() and suffix in {".shp", ".gpkg", ".geojson", ".json"}:
            # Avoid treating metadata JSON as vector data unless Fiona can open it later.
            datasets.append(str(path.resolve()))
    return datasets


def find_sources(path: Path) -> list[str]:
    path = path.resolve()
    if path.is_file() and path.suffix.lower() in {".shp", ".gpkg", ".geojson"}:
        return [str(path)]
    if path.is_dir() and path.suffix.lower() == ".gdb":
        return [str(path)]

    if path.is_file() and path.suffix.lower() in {".zip", ".mpk", ".mpkx"}:
        expansion_root = path.parent / (path.name + ".expanded")
        if not expansion_root.exists() or not any(expansion_root.iterdir()):
            _safe_extract_zip(path, expansion_root)
        _expand_archives(expansion_root)
        datasets = _discover_vector_datasets(expansion_root)
        if datasets:
            return datasets
        raise FileNotFoundError(f"No readable vector dataset found after expanding {path}")

    if path.is_dir():
        _expand_archives(path)
        datasets = _discover_vector_datasets(path)
        if datasets:
            return datasets

    raise FileNotFoundError(f"No vector dataset found under {path}")

def polygon_parts(geom):
    if geom.is_empty:
        return
    if isinstance(geom, Polygon):
        yield geom
    elif isinstance(geom, MultiPolygon):
        yield from geom.geoms
    elif isinstance(geom, GeometryCollection):
        for part in geom.geoms:
            yield from polygon_parts(part)


def source_layers(source: str) -> list[str | None]:
    try:
        layers = fiona.listlayers(source)
    except Exception:
        return [None]
    return list(layers) or [None]


def layer_is_polygon(source: str, layer: str | None) -> bool:
    kwargs = {"layer": layer} if layer else {}
    with fiona.open(source, **kwargs) as collection:
        geom_type = str(collection.schema.get("geometry") or "")
        return "Polygon" in geom_type


def transformer_for(collection) -> Transformer | None:
    crs_input = collection.crs_wkt or collection.crs
    if not crs_input:
        return None
    src = CRS.from_user_input(crs_input)
    dst = CRS.from_epsg(4326)
    if src == dst:
        return None
    return Transformer.from_crs(src, dst, always_xy=True)


def read_geometries(source: str, bbox4326: tuple[float, float, float, float]):
    for layer in source_layers(source):
        if not layer_is_polygon(source, layer):
            continue
        kwargs = {"layer": layer} if layer else {}
        with fiona.open(source, **kwargs) as collection:
            transformer = transformer_for(collection)
            # Fiona's bbox filter is applied in layer CRS. If the layer is not geographic,
            # fall back to streaming and post-filtering rather than passing the wrong bbox.
            iterator = collection.filter(bbox=bbox4326) if transformer is None else collection
            for feat in iterator:
                if not feat.get("geometry"):
                    continue
                geom = shape(feat["geometry"])
                if transformer is not None:
                    geom = geom_transform(transformer.transform, geom)
                if geom.is_empty or not geom.intersects(box(*bbox4326)):
                    continue
                yield geom


def earcut_polygon(poly: Polygon) -> tuple[np.ndarray, np.ndarray] | None:
    rings = [np.asarray(poly.exterior.coords[:-1], dtype=np.float32)]
    rings.extend(np.asarray(r.coords[:-1], dtype=np.float32) for r in poly.interiors)
    rings = [r for r in rings if len(r) >= 3]
    if not rings:
        return None
    vertices = np.vstack(rings)
    ends = np.cumsum([len(r) for r in rings], dtype=np.uint32)
    try:
        indices = earcut.triangulate_float32(vertices, ends)
    except Exception:
        fixed = poly.buffer(0)
        if fixed.is_empty or not isinstance(fixed, Polygon):
            return None
        return earcut_polygon(fixed)
    if len(indices) < 3:
        return None
    return vertices, indices.astype(np.uint32, copy=False)


def encode_tile(polygons: list[Polygon], bounds: tuple[float, float, float, float], out_path: Path):
    lon0, lat0, lon1, lat1 = bounds
    all_q = []
    all_indices = []
    vertex_base = 0
    for poly in polygons:
        triangulated = earcut_polygon(poly)
        if triangulated is None:
            continue
        vertices, indices = triangulated
        q_lon = np.rint((vertices[:, 0] - lon0) * 65535.0 / (lon1 - lon0)).clip(0, 65535).astype("<u2")
        q_lat = np.rint((vertices[:, 1] - lat0) * 65535.0 / (lat1 - lat0)).clip(0, 65535).astype("<u2")
        q = np.column_stack((q_lon, q_lat)).astype("<u2", copy=False)
        all_q.append(q)
        all_indices.append((indices + vertex_base).astype("<u4", copy=False))
        vertex_base += len(vertices)
    if not all_q:
        return None
    q_all = np.vstack(all_q)
    i_all = np.concatenate(all_indices)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as out:
        out.write(MAGIC)
        out.write(struct.pack("<II", len(q_all), len(i_all)))
        out.write(q_all.tobytes(order="C"))
        out.write(i_all.tobytes(order="C"))
    return {"vertices": int(len(q_all)), "indices": int(len(i_all)), "triangles": int(len(i_all) // 3), "bytes": out_path.stat().st_size}


def tile_name(lon0: float, lat0: float) -> str:
    ns = "N" if lat0 >= 0 else "S"
    ew = "E" if lon0 >= 0 else "W"
    return f"{ns}{abs(int(lat0)):02d}_{ew}{abs(int(lon0)):03d}.gvb"


def tile_origins(bboxv, tile_deg: int):
    min_lon, min_lat, max_lon, max_lat = bboxv
    lon_start = math.floor(min_lon / tile_deg) * tile_deg
    lat_start = math.floor(min_lat / tile_deg) * tile_deg
    lon = lon_start
    while lon < max_lon:
        lat = lat_start
        while lat < max_lat:
            yield lon, lat
            lat += tile_deg
        lon += tile_deg


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--bbox", default="122,24,146,46")
    parser.add_argument("--tile-deg", type=int, default=10)
    parser.add_argument("--lods", default="0.15,0.05,0.015,0.004,0")
    args = parser.parse_args()

    bboxv = tuple(float(v) for v in args.bbox.split(","))
    if len(bboxv) != 4:
        raise ValueError("bbox must be minLon,minLat,maxLon,maxLat")
    tolerances = [float(v) for v in args.lods.split(",")]
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    sources = find_sources(Path(args.source))
    print("sources:", sources)
    clip_bbox = box(*bboxv)
    raw_geometries = []
    for src in sources:
        for geom in read_geometries(src, bboxv):
            clipped = geom.intersection(clip_bbox)
            if not clipped.is_empty:
                raw_geometries.append(clipped)
    if not raw_geometries:
        raise RuntimeError("No polygon geometry intersected the requested bbox")
    print(f"selected geometries: {len(raw_geometries)}")

    manifest = {
        "format": "GVB1",
        "version": 1,
        "tileDegres": args.tile_deg,
        "bbox": bboxv,
        "source": {
            "name": "USGS Global Islands",
            "itemId": "63bdf25dd34e92aad3cda273",
            "doi": "10.5066/P91ZCSGM",
            "resolutionMeters": 30,
            "imageryYear": 2014,
            "license": "Public Domain (U.S. Government)",
        },
        "lods": [],
        "tiles": [],
    }

    for lod, tolerance in enumerate(tolerances):
        lod_geoms = []
        for geom in raw_geometries:
            g = geom if tolerance <= 0 else geom.simplify(tolerance, preserve_topology=True)
            if not g.is_empty:
                lod_geoms.append(g)
        manifest["lods"].append({"id": lod, "simplifyDegrees": tolerance})
        for lon0, lat0 in tile_origins(bboxv, args.tile_deg):
            bounds = (lon0, lat0, lon0 + args.tile_deg, lat0 + args.tile_deg)
            tile_box = box(*bounds)
            polygons = []
            for geom in lod_geoms:
                if not geom.intersects(tile_box):
                    continue
                clipped = geom.intersection(tile_box)
                for poly in polygon_parts(clipped):
                    if poly.area > 0:
                        polygons.append(poly)
            if not polygons:
                continue
            rel = Path(f"lod{lod}") / tile_name(lon0, lat0)
            stats = encode_tile(polygons, bounds, out_dir / rel)
            if stats:
                manifest["tiles"].append({"lod": lod, "path": rel.as_posix(), "bounds": bounds, **stats})
                print(f"lod{lod} {rel}: {stats}")

    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(manifest['tiles'])} tiles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
