import builtins
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from layercompressinsight import main as cli
from layercompressinsight.report import (
    analyze_image_tarball,
    generate_json_report,
    generate_markdown_report,
)


def layer_tar(files: dict[str, bytes] | None = None, *, gzip_layer: bool = False) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, content in (files or {}).items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mtime = 0
            archive.addfile(member, io.BytesIO(content))
    raw = stream.getvalue()
    return gzip.compress(raw, compresslevel=9, mtime=0) if gzip_layer else raw


def image_tar(
    path: Path,
    layers: list[bytes],
    *,
    outer_mode: str = "w",
    include_manifest: bool = True,
    manifest_layers: list[str] | None = None,
) -> Path:
    names = manifest_layers or [f"sha256-{index}/layer.tar" for index in range(len(layers))]
    with tarfile.open(path, outer_mode) as archive:
        for name, content in zip(names, layers):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        if include_manifest:
            payload = json.dumps([{
                "Config": "config.json",
                "RepoTags": ["example:latest"],
                "Layers": names,
            }]).encode()
            member = tarfile.TarInfo("manifest.json")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return path


def test_plain_docker_save_outer_tar_discovers_manifest_and_plain_layers(tmp_path: Path):
    archive = image_tar(tmp_path / "image.tar", [layer_tar({"app.txt": b"hello"})])
    result = analyze_image_tarball(archive)
    assert len(result) == 1
    assert result[0]["path"] == "sha256-0/layer.tar"
    assert result[0]["file_content_size"] == 5
    assert result[0]["raw_layer_tar_size"] > 0


def test_optionally_gzipped_outer_archive_is_accepted(tmp_path: Path):
    archive = image_tar(tmp_path / "image.tar.gz", [layer_tar({"a": b"x"})], outer_mode="w:gz")
    assert analyze_image_tarball(archive)[0]["file_content_size"] == 1


def test_compressed_layer_is_normalized_to_same_metric_as_plain_layer(tmp_path: Path):
    files = {"payload": b"repeat" * 1000}
    plain = analyze_image_tarball(image_tar(tmp_path / "plain.tar", [layer_tar(files)]))[0]
    compressed = analyze_image_tarball(
        image_tar(tmp_path / "compressed.tar", [layer_tar(files, gzip_layer=True)]))[0]
    for key in ("raw_layer_tar_size", "estimated_gzip_size", "estimated_gzip_ratio", "file_content_size"):
        assert compressed[key] == plain[key]


def test_metric_matches_deterministic_gzip_of_raw_layer(tmp_path: Path):
    raw = layer_tar({"payload": b"A" * 32768})
    result = analyze_image_tarball(image_tar(tmp_path / "image.tar", [raw]))[0]
    assert result["raw_layer_tar_size"] == len(raw)
    assert result["estimated_gzip_size"] == len(gzip.compress(raw, compresslevel=9, mtime=0))
    assert result["estimated_gzip_ratio"] == result["estimated_gzip_size"] / len(raw)


def test_compressible_content_has_better_estimate_than_poorly_compressible_content(tmp_path: Path):
    compressible = layer_tar({"payload": b"A" * 131072})
    pseudo_random = layer_tar({
        "payload": b"".join(hashlib.sha256(str(index).encode()).digest() for index in range(4096))
    })
    results = analyze_image_tarball(image_tar(tmp_path / "image.tar", [compressible, pseudo_random]))
    assert results[0]["estimated_gzip_ratio"] < results[1]["estimated_gzip_ratio"]


def test_empty_valid_layer_is_handled(tmp_path: Path):
    result = analyze_image_tarball(image_tar(tmp_path / "image.tar", [layer_tar()]))[0]
    assert result["file_content_size"] == 0
    assert result["raw_layer_tar_size"] > 0
    assert result["estimated_gzip_ratio"] >= 0


def test_threshold_is_strictly_greater_for_markdown_and_json():
    layers = [
        {"index": 1, "path": "below/layer.tar", "raw_layer_tar_size": 10,
         "estimated_gzip_size": 4, "estimated_gzip_ratio": 0.49, "file_content_size": 1},
        {"index": 2, "path": "equal/layer.tar", "raw_layer_tar_size": 10,
         "estimated_gzip_size": 5, "estimated_gzip_ratio": 0.5, "file_content_size": 1},
        {"index": 3, "path": "above/layer.tar", "raw_layer_tar_size": 10,
         "estimated_gzip_size": 6, "estimated_gzip_ratio": 0.51, "file_content_size": 1},
    ]
    data = json.loads(generate_json_report(layers, 0.5))
    assert [item["flag"] for item in data["layers"]] == [None, None, "poor_compression"]
    markdown = generate_markdown_report(layers, 0.5)
    assert "| 1 | below/layer.tar" in markdown
    assert "| 2 | equal/layer.tar" in markdown
    assert "| 3 | above/layer.tar | 10 | 6 | 0.510 | Poor compression |" in markdown
    assert markdown.index("below/layer.tar") < markdown.index("equal/layer.tar") < markdown.index("above/layer.tar")


def test_json_uses_truthful_estimate_names(tmp_path: Path):
    layer = analyze_image_tarball(image_tar(tmp_path / "image.tar", [layer_tar({"a": b"a"})]))
    data = json.loads(generate_json_report(layer, 0.5))
    assert data["metric"] == "deterministic_gzip_estimate"
    assert {"raw_layer_tar_size", "estimated_gzip_size", "estimated_gzip_ratio"} <= data["layers"][0].keys()
    assert "compressed_size" not in data["layers"][0]
    assert "uncompressed_size" not in data["layers"][0]


def test_cli_tarball_mode(tmp_path: Path):
    archive = image_tar(tmp_path / "image.tar", [layer_tar({"a": b"a" * 100})])
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "layercompressinsight.main", "--tarball", str(archive),
         "--output-format", "json"], capture_output=True, text=True, env=env, check=True)
    assert json.loads(result.stdout)["layers"][0]["path"] == "sha256-0/layer.tar"


def test_mocked_docker_save_writes_plain_tar_and_image_mode_cleans_up(tmp_path: Path, monkeypatch):
    source = image_tar(tmp_path / "docker-output.tar", [layer_tar({"a": b"data"})])
    created: list[Path] = []

    def fake_run(command, **kwargs):
        assert command[:3] == ["docker", "save", "example:latest"]
        destination = Path(command[command.index("-o") + 1])
        assert destination.suffix == ".tar"
        destination.write_bytes(source.read_bytes())
        created.append(destination)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["layercompressinsight", "--image", "example:latest",
                                      "--output-format", "json"])
    cli.main()
    assert created and not created[0].exists()


def test_docker_save_failure_removes_temporary_archive(monkeypatch):
    destination: list[Path] = []

    def fail(command, **kwargs):
        destination.append(Path(command[command.index("-o") + 1]))
        raise subprocess.CalledProcessError(1, command, stderr=b"daemon unavailable")

    monkeypatch.setattr(cli.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="daemon unavailable"):
        cli.run_docker_save("example:latest")
    assert destination and not destination[0].exists()


def test_malformed_archive_is_rejected(tmp_path: Path):
    path = tmp_path / "bad.tar"
    path.write_bytes(b"not a tar archive")
    with pytest.raises(ValueError, match="malformed"):
        analyze_image_tarball(path)


def test_missing_manifest_is_rejected(tmp_path: Path):
    path = image_tar(tmp_path / "image.tar", [layer_tar({"a": b"a"})], include_manifest=False)
    with pytest.raises(ValueError, match="manifest.json not found"):
        analyze_image_tarball(path)


def test_missing_layer_is_rejected(tmp_path: Path):
    path = image_tar(tmp_path / "image.tar", [], manifest_layers=["missing/layer.tar"])
    with pytest.raises(ValueError, match="not found"):
        analyze_image_tarball(path)


def test_import_does_not_mutate_python_builtins():
    assert not hasattr(builtins, "io")
