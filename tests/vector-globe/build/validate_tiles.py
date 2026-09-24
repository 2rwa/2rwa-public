#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import struct
from collections import defaultdict
from pathlib import Path

import numpy as np


def validate_descriptor(root: Path, tile: dict) -> dict:
    path = root / tile["path"]
    raw = path.read_bytes()
    assert raw[:4] == b"GVB1", path
    vertex_count, index_count = struct.unpack_from("<II", raw, 4)
    expected_bytes = 12 + vertex_count * 4 + index_count * 4
    assert len(raw) == expected_bytes, (path, len(raw), expected_bytes)
    assert len(raw) == tile["bytes"], (path, len(raw), tile["bytes"])

    quantized = np.frombuffer(
        raw, dtype="<u2", count=vertex_count * 2, offset=12
    ).reshape((-1, 2))
    indices = np.frombuffer(
        raw, dtype="<u4", count=index_count, offset=12 + vertex_count * 4
    )
    assert quantized.shape == (vertex_count, 2)
    assert index_count % 3 == 0
    if index_count:
        assert int(indices.max()) < vertex_count
    assert vertex_count == tile["vertices"] and index_count == tile["indices"]

    return {
        "path": tile["path"],
        "lod": int(tile["lod"]),
        "resolution": tile.get("gshhgResolution"),
        "bytes": len(raw),
        "vertices": vertex_count,
        "triangles": index_count // 3,
    }


def aggregate(items: list[dict]) -> dict:
    by_lod = defaultdict(lambda: {"tiles": 0, "vertices": 0, "triangles": 0, "bytes": 0})
    for item in items:
        key = f"lod{item['lod']}-{item.get('resolution') or 'unknown'}"
        stats = by_lod[key]
        stats["tiles"] += 1
        stats["vertices"] += item["vertices"]
        stats["triangles"] += item["triangles"]
        stats["bytes"] += item["bytes"]

    largest = sorted(items, key=lambda item: item["bytes"], reverse=True)
    return {
        "tiles": len(items),
        "vertices": sum(item["vertices"] for item in items),
        "triangles": sum(item["triangles"] for item in items),
        "bytes": sum(item["bytes"] for item in items),
        "mib": round(sum(item["bytes"] for item in items) / (1024 * 1024), 3),
        "byLod": dict(by_lod),
        "largestTiles": largest[:20],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    args = parser.parse_args()

    root = Path(args.data)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "GVB1"

    canonical_items = [
        validate_descriptor(root, tile)
        for tile in manifest.get("tiles") or []
    ]
    canonical = aggregate(canonical_items)

    delivery_items: list[dict] = []
    delivery = manifest.get("delivery") or {}
    for lod_text, descriptors in (delivery.get("lods") or {}).items():
        lod = int(lod_text)
        for tile in descriptors:
            assert int(tile["lod"]) == lod
            delivery_items.append(validate_descriptor(root, tile))
    delivery_report = aggregate(delivery_items) if delivery_items else {
        "tiles": 0,
        "vertices": 0,
        "triangles": 0,
        "bytes": 0,
        "mib": 0.0,
        "byLod": {},
        "largestTiles": [],
    }

    report = {
        "format": manifest["format"],
        "bbox": manifest.get("bbox"),
        "tileDegrees": manifest.get("tileDegrees"),
        "source": manifest.get("source"),
        **canonical,
        "delivery": delivery_report,
        "repositoryBytes": canonical["bytes"] + delivery_report["bytes"],
        "repositoryMiB": round(
            (canonical["bytes"] + delivery_report["bytes"]) / (1024 * 1024), 3
        ),
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
