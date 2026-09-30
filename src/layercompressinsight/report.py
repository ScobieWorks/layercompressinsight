"""Offline analysis of Docker-save layer archives."""

from __future__ import annotations

import bz2
import gzip
import json
import lzma
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Dict, List

LayerInfo = Dict[str, object]
_COPY_CHUNK = 1024 * 1024


def _copy(source, target) -> None:
    shutil.copyfileobj(source, target, length=_COPY_CHUNK)


def _normalized_layer_tar(layer_file):
    """Return a seekable, uncompressed tar stream without extracting its members."""
    magic = layer_file.read(6)
    layer_file.seek(0)
    if magic.startswith(b"\x1f\x8b"):
        source = gzip.GzipFile(fileobj=layer_file, mode="rb")
    elif magic.startswith(b"BZh"):
        source = bz2.BZ2File(layer_file, mode="rb")
    elif magic.startswith(b"\xfd7zXZ\x00"):
        source = lzma.LZMAFile(layer_file, mode="rb")
    else:
        source = layer_file
    normalized = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    try:
        _copy(source, normalized)
    except (EOFError, OSError) as exc:
        normalized.close()
        raise ValueError("Layer archive compression is malformed") from exc
    finally:
        if source is not layer_file:
            source.close()
    normalized.seek(0)
    return normalized


def _layer_metrics(layer_file, layer_path: str) -> tuple[int, int, int]:
    with _normalized_layer_tar(layer_file) as raw_tar:
        raw_tar.seek(0, 2)
        raw_size = raw_tar.tell()
        raw_tar.seek(0)
        estimated = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        try:
            with gzip.GzipFile(fileobj=estimated, mode="wb", compresslevel=9, mtime=0) as output:
                _copy(raw_tar, output)
            estimated_size = estimated.tell()
        finally:
            estimated.close()
        raw_tar.seek(0)
        try:
            with tarfile.open(fileobj=raw_tar, mode="r:") as inner:
                content_size = sum(member.size for member in inner.getmembers() if member.isfile())
        except (tarfile.TarError, OSError) as exc:
            raise ValueError(f"Layer archive {layer_path} is malformed") from exc
    return raw_size, estimated_size, content_size


def analyze_image_tarball(tar_path: Path) -> List[LayerInfo]:
    """Analyze Docker-save layers using a deterministic local gzip estimate.

    The estimate is gzip(level=9, mtime=0) applied to each normalized raw layer tar
    stream. It is an offline comparison metric, not an observed registry blob size.
    """
    layers: List[LayerInfo] = []
    try:
        archive = tarfile.open(tar_path, "r:*")
    except (tarfile.TarError, OSError) as exc:
        raise ValueError("Docker image archive is malformed") from exc
    with archive:
        try:
            manifest_member = archive.getmember("manifest.json")
        except KeyError as exc:
            raise ValueError("manifest.json not found in Docker image archive") from exc
        if not manifest_member.isfile() or manifest_member.size > 1024 * 1024:
            raise ValueError("manifest.json is not a supported regular file")
        manifest_file = archive.extractfile(manifest_member)
        if manifest_file is None:
            raise ValueError("manifest.json could not be read")
        with manifest_file:
            try:
                manifest = json.load(manifest_file)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("manifest.json is malformed") from exc
        if (not isinstance(manifest, list) or not manifest
                or not isinstance(manifest[0], dict)
                or not isinstance(manifest[0].get("Layers"), list)):
            raise ValueError("manifest.json has an unsupported Docker-save format")
        for index, layer_path in enumerate(manifest[0]["Layers"], start=1):
            if not isinstance(layer_path, str) or not layer_path:
                raise ValueError("manifest.json contains an invalid layer path")
            try:
                layer_member = archive.getmember(layer_path)
            except KeyError as exc:
                raise ValueError(f"Layer file {layer_path} not found in Docker image archive") from exc
            if not layer_member.isfile():
                raise ValueError(f"Layer file {layer_path} is not a regular archive member")
            layer_file = archive.extractfile(layer_member)
            if layer_file is None:
                raise ValueError(f"Layer file {layer_path} could not be read")
            with layer_file:
                raw_size, estimated_size, content_size = _layer_metrics(layer_file, layer_path)
            ratio = estimated_size / raw_size if raw_size else 0.0
            layers.append({
                "index": index,
                "path": layer_path,
                "raw_layer_tar_size": raw_size,
                "estimated_gzip_size": estimated_size,
                "estimated_gzip_ratio": ratio,
                "file_content_size": content_size,
            })
    return layers


def _poor(layer: LayerInfo, threshold: float) -> bool:
    return float(layer["estimated_gzip_ratio"]) > threshold


def generate_markdown_report(layers: List[LayerInfo], threshold: float) -> str:
    """Generate a Markdown report in original manifest order."""
    lines = [
        "# Layer Compression Estimate", "",
        "Estimated gzip ratio = deterministic gzip size / raw layer tar size. ",
        f"Layers strictly above {threshold:.3f} are flagged as poorly compressible.", "",
        "| Layer | Path | Raw layer tar (bytes) | Estimated gzip (bytes) | Estimated ratio | Flag |",
        "|------:|------|----------------------:|-----------------------:|----------------:|------|",
    ]
    for layer in layers:
        flag = "Poor compression" if _poor(layer, threshold) else ""
        lines.append(
            f"| {layer['index']} | {layer['path']} | {layer['raw_layer_tar_size']} | "
            f"{layer['estimated_gzip_size']} | {float(layer['estimated_gzip_ratio']):.3f} | {flag} |"
        )
    return "\n".join(lines)


def generate_json_report(layers: List[LayerInfo], threshold: float) -> str:
    """Generate a machine-readable report with explicit estimate names."""
    return json.dumps({
        "metric": "deterministic_gzip_estimate", "threshold": threshold,
        "threshold_behavior": "poor_compression_when_estimated_gzip_ratio_is_strictly_greater",
        "layers": [{**layer, "flag": "poor_compression" if _poor(layer, threshold) else None}
                   for layer in layers],
    }, indent=2)
