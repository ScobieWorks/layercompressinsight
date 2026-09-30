#!/usr/bin/env python3
"""Entry point for the LayerCompressInsight CLI."""

import argparse
import subprocess
import sys
from pathlib import Path

from .report import analyze_image_tarball, generate_markdown_report, generate_json_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Estimate gzip compressibility of layers in a Docker-save archive.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tarball", type=Path, help="Path to a Docker image tarball.")
    group.add_argument("--image", type=str, help="Name of a local Docker image (requires Docker CLI).")
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="Flag estimated gzip ratios strictly above this value (default: 0.5).")
    parser.add_argument("--output-format", choices=["markdown", "json"], default="markdown",
                        help="Output format (default: markdown).")
    parser.add_argument("--output", type=Path, help="File to write the report to (default: stdout).")
    return parser.parse_args()


def run_docker_save(image_name: str) -> Path:
    """Run `docker save` and return the path to the temporary tarball."""
    import tempfile

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar")
    tmp.close()
    try:
        subprocess.run(["docker", "save", image_name, "-o", tmp.name], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError) as exc:
        Path(tmp.name).unlink(missing_ok=True)
        detail=(exc.stderr.decode(errors="replace").strip()
                if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc))
        raise RuntimeError(f"Failed to save image {image_name}: {detail}") from exc
    return Path(tmp.name)


def main() -> None:
    args = parse_args()

    if args.tarball:
        tar_path = args.tarball
        if not tar_path.is_file():
            print(f"Tarball {tar_path} does not exist.", file=sys.stderr)
            sys.exit(1)
    else:
        # image name provided
        try:
            tar_path = run_docker_save(args.image)
        except Exception as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)

    try:
        layers = analyze_image_tarball(tar_path)
    finally:
        if not args.tarball:
            # clean up temporary file
            tar_path.unlink(missing_ok=True)

    if args.output_format == "markdown":
        report = generate_markdown_report(layers, args.threshold)
    else:
        report = generate_json_report(layers, args.threshold)

    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report)


if __name__ == "__main__":
    main()
