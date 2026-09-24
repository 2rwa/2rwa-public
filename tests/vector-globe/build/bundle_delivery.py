#!/usr/bin/env python3
"""Create coarse delivery bundles from the canonical 10-degree GVB1 tiles.

The canonical tiles remain untouched.  The viewer may use larger bundle files for
LOD0-LOD3 to reduce HTTP request count while retaining 10-degree LOD4 tiles.

Default bundle layout:
  LOD0: 360 x 180 degrees (one global file)
  LOD1:  90 x  90 degrees
  LOD2:  30 x  30 degrees
  LOD3:  20 x  20 degrees
  LOD4: canonical 10-degree tiles (not bundled)

Each bundle is re-quantized to its own bounds and keeps the original triangles.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
from collections import defaultdict
from pathlib import Path

import numpy as np

MAGIC = b"GVB1"
DEFAULT_SCHEMES = {
    0: (360, 180),
    1: (90, 90),
    2: (30, 30),
    3: (20, 20),
}


def parse_schemes(text: str | None) -> dict[int, tuple[int, int]]:
    if not text:
        return dict(DEFAULT_SCHEMES)
    schemes: dict[int, tuple[int, int]] = {}
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        lod_text, span_text = item.split(":", 1)
        lon_text, lat_text = span_text.lower().split("x", 1)
        lod = int(lod_text)
        lon_span = int(lon_text)
        lat_span = int(lat_text)
        if lod < 0 or lon_span <= 0 or lat_span <= 0:
            raise ValueError(f"invalid scheme: {item}")
        if 360 % lon_span != 0 or 180 % lat_span != 0:
            raise ValueError(f"bundle spans must divide world dimensions exactly: {item}")
        schemes[lod] = (lon_span, lat_span)
    return schemes


def decode_tile(root: Path, tile: dict) -> tuple[np.ndarray, np.ndarray]:
    path = root / tile["path"]
    raw = path.read_bytes()
    if raw[:4] != MAGIC:
        raise RuntimeError(f"bad GVB1 magic: {path}")
    vertex_count, index_count = struct.unpack_from("<II", raw, 4)
    expected = 12 + vertex_count * 4 + index_count * 4
    if len(raw) != expected:
        raise RuntimeError(f"bad GVB1 length: {path}: {len(raw)} != {expected}")

    quantized = np.frombuffer(
        raw, dtype="<u2", count=vertex_count * 2, offset=12
    ).reshape((-1, 2))
    indices = np.frombuffer(
        raw, dtype="<u4", count=index_count, offset=12 + vertex_count * 4
    ).astype("<u4", copy=True)

    lon0, lat0, lon1, lat1 = [float(value) for value in tile["bounds"]]
    coordinates = np.empty((vertex_count, 2), dtype=np.float64)
    coordinates[:, 0] = lon0 + (
        quantized[:, 0].astype(np.float64) / 65535.0
    ) * (lon1 - lon0)
    coordinates[:, 1] = lat0 + (
        quantized[:, 1].astype(np.float64) / 65535.0
    ) * (lat1 - lat0)
    return coordinates, indices


def group_bounds(
    bounds: list[float] | tuple[float, ...],
    lon_span: int,
    lat_span: int,
) -> tuple[int, int, int, int]:
    if lon_span == 360 and lat_span == 180:
        return (-180, -90, 180, 90)

    source_lon0 = float(bounds[0])
    source_lat0 = float(bounds[1])
    lon_index = math.floor((source_lon0 + 180.0 + 1e-9) / lon_span)
    lat_index = math.floor((source_lat0 + 90.0 + 1e-9) / lat_span)
    lon0 = -180 + lon_index * lon_span
    lat0 = -90 + lat_index * lat_span
    lon0 = min(max(lon0, -180), 180 - lon_span)
    lat0 = min(max(lat0, -90), 90 - lat_span)
    return (lon0, lat0, lon0 + lon_span, lat0 + lat_span)


def origin_name(lon0: int, lat0: int) -> str:
    ns = "N" if lat0 >= 0 else "S"
    ew = "E" if lon0 >= 0 else "W"
    return f"{ns}{abs(int(lat0)):02d}_{ew}{abs(int(lon0)):03d}.gvb"


def encode_bundle(
    root: Path,
    source_tiles: list[dict],
    bounds: tuple[int, int, int, int],
    output_path: Path,
) -> dict:
    vertices_parts: list[np.ndarray] = []
    index_parts: list[np.ndarray] = []
    vertex_base = 0

    for tile in sorted(source_tiles, key=lambda item: item["path"]):
        coordinates, indices = decode_tile(root, tile)
        vertices_parts.append(coordinates)
        index_parts.append(indices + vertex_base)
        vertex_base += len(coordinates)

    if not vertices_parts:
        raise RuntimeError(f"bundle has no source geometry: {bounds}")

    coordinates = np.vstack(vertices_parts)
    indices = np.concatenate(index_parts).astype("<u4", copy=False)
    lon0, lat0, lon1, lat1 = bounds

    q_lon = np.rint(
        (coordinates[:, 0] - lon0) * 65535.0 / (lon1 - lon0)
    ).clip(0, 65535).astype("<u2")
    q_lat = np.rint(
        (coordinates[:, 1] - lat0) * 65535.0 / (lat1 - lat0)
    ).clip(0, 65535).astype("<u2")
    quantized = np.column_stack((q_lon, q_lat)).astype("<u2", copy=False)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as target:
        target.write(MAGIC)
        target.write(struct.pack("<II", len(quantized), len(indices)))
        target.write(quantized.tobytes(order="C"))
        target.write(indices.tobytes(order="C"))

    return {
        "vertices": int(len(quantized)),
        "indices": int(len(indices)),
        "triangles": int(len(indices) // 3),
        "bytes": output_path.stat().st_size,
        "sourceTiles": len(source_tiles),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument(
        "--schemes",
        default="",
        help="comma list like 0:360x180,1:90x90,2:30x30,3:20x20",
    )
    args = parser.parse_args()

    root = Path(args.data)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "GVB1":
        raise RuntimeError("manifest is not GVB1")
    if manifest.get("bbox") != [-180.0, -90.0, 180.0, 90.0] and manifest.get("bbox") != [-180, -90, 180, 90]:
        raise RuntimeError("delivery bundling requires a global manifest")

    schemes = parse_schemes(args.schemes)
    delivery_root = root / "delivery"
    if delivery_root.exists():
        shutil.rmtree(delivery_root)

    by_lod: dict[int, list[dict]] = defaultdict(list)
    for tile in manifest.get("tiles") or []:
        by_lod[int(tile["lod"])].append(tile)

    delivery_lods: dict[str, list[dict]] = {}
    scheme_metadata: dict[str, dict] = {}

    for lod in sorted(schemes):
        lon_span, lat_span = schemes[lod]
        source_tiles = by_lod.get(lod) or []
        if not source_tiles:
            raise RuntimeError(f"no canonical source tiles for LOD{lod}")

        groups: dict[tuple[int, int, int, int], list[dict]] = defaultdict(list)
        for tile in source_tiles:
            groups[group_bounds(tile["bounds"], lon_span, lat_span)].append(tile)

        descriptors: list[dict] = []
        for bounds in sorted(groups):
            if bounds == (-180, -90, 180, 90):
                relative = Path("delivery") / f"lod{lod}" / "global.gvb"
            else:
                relative = Path("delivery") / f"lod{lod}" / origin_name(bounds[0], bounds[1])
            stats = encode_bundle(root, groups[bounds], bounds, root / relative)
            descriptor = {
                "lod": lod,
                "gshhgResolution": manifest["lods"][lod].get("gshhgResolution"),
                "path": relative.as_posix(),
                "bounds": list(bounds),
                "deliveryBundle": True,
                "alwaysVisible": bounds == (-180, -90, 180, 90),
                **stats,
            }
            descriptors.append(descriptor)
            print(f"LOD{lod} {relative}: {stats}")

        delivery_lods[str(lod)] = descriptors
        scheme_metadata[str(lod)] = {
            "lonSpanDegrees": lon_span,
            "latSpanDegrees": lat_span,
            "bundleCount": len(descriptors),
            "sourceTileCount": len(source_tiles),
            "bytes": sum(item["bytes"] for item in descriptors),
        }

    manifest["delivery"] = {
        "version": 1,
        "description": "HTTP request optimized bundles; canonical 10-degree tiles remain in tiles[]",
        "schemes": scheme_metadata,
        "lods": delivery_lods,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(manifest["delivery"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
