#!/usr/bin/env python3
"""Download a pinned GSHHG Shapefile release asset from GitHub Releases.

The workflow resolves the release through the GitHub API instead of depending on the
legacy SOEST FTP host. The downloaded asset is size-checked, ZIP-magic checked, and
SHA-256 hashed. A deterministic JSON sidecar is written next to the archive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import requests

DEFAULT_REPOSITORY = "GenericMappingTools/gshhg-gmt"
DEFAULT_TAG = "2.3.7"
DEFAULT_ASSET = "gshhg-shp-2.3.7.zip"
USER_AGENT = "2rwa-gshhg-vector-globe-builder/1.0 (+https://github.com/2rwa/chatgpt-workspace)"


def request_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def valid_cached_archive(path: Path, expected_size: int) -> bool:
    if not path.is_file():
        return False
    if expected_size > 0 and path.stat().st_size != expected_size:
        return False
    with path.open("rb") as source:
        return source.read(4).startswith(b"PK")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--tag", default=DEFAULT_TAG)
    parser.add_argument("--asset", default=DEFAULT_ASSET)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    api_url = f"https://api.github.com/repos/{args.repository}/releases/tags/{args.tag}"
    session = requests.Session()
    session.headers.update(request_headers())

    response = session.get(api_url, timeout=60)
    response.raise_for_status()
    release = response.json()
    assets = {str(entry.get("name")): entry for entry in release.get("assets") or []}
    if args.asset not in assets:
        available = ", ".join(sorted(assets)) or "(none)"
        raise RuntimeError(f"Release asset {args.asset!r} not found. Available: {available}")

    asset = assets[args.asset]
    download_url = str(asset["browser_download_url"])
    expected_size = int(asset.get("size") or 0)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if valid_cached_archive(output_path, expected_size):
        print(f"using cached archive: {output_path} ({output_path.stat().st_size} bytes)")
    else:
        output_path.unlink(missing_ok=True)
        temp_path = output_path.with_suffix(output_path.suffix + ".part")
        temp_path.unlink(missing_ok=True)
        print(f"downloading: {download_url}")
        with session.get(download_url, timeout=600, stream=True, allow_redirects=True) as download:
            download.raise_for_status()
            with temp_path.open("wb") as target:
                for chunk in download.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        target.write(chunk)
        actual_size = temp_path.stat().st_size
        if expected_size and actual_size != expected_size:
            temp_path.unlink(missing_ok=True)
            raise RuntimeError(f"size mismatch: expected {expected_size}, got {actual_size}")
        with temp_path.open("rb") as source:
            if not source.read(4).startswith(b"PK"):
                temp_path.unlink(missing_ok=True)
                raise RuntimeError("downloaded asset does not have ZIP magic")
        temp_path.replace(output_path)

    digest = sha256_file(output_path)
    metadata = {
        "repository": args.repository,
        "tag": args.tag,
        "releaseUrl": release.get("html_url"),
        "asset": args.asset,
        "assetUrl": download_url,
        "sizeBytes": output_path.stat().st_size,
        "sha256": digest,
    }
    metadata_path = output_path.with_suffix(output_path.suffix + ".json")
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
