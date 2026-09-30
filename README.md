# LayerCompressInsight

LayerCompressInsight estimates how well each layer in a Docker-save archive
compresses with gzip. It reads the archive locally, preserves manifest order,
and emits Markdown or JSON without extracting archive members to disk.

For each layer, the tool normalizes a plain, gzip, bzip2, or xz-compressed
`layer.tar` stream to its raw tar bytes. It then applies deterministic gzip
(compression level 9, timestamp 0) and reports:

- raw layer tar size;
- estimated gzip size;
- estimated gzip ratio (`estimated gzip size / raw layer tar size`); and
- total size of regular-file content recorded inside the layer tar.

This is a repeatable **offline estimate**. It does not query a registry and does
not claim to report the exact compressed blob size stored by Docker Hub or any
other registry. Registry codecs, metadata, and implementation details can
produce different sizes.

## Installation

Python 3.11 or later is required.

```bash
pip install git+https://github.com/ScobieWorks/layercompressinsight.git
```

The Python package has no runtime dependencies outside the standard library.
The `--image` mode additionally requires a working local Docker CLI and daemon.

## Usage

Analyze an existing Docker-save archive:

```bash
docker save example/image:latest -o image.tar
layercompressinsight --tarball image.tar
```

Analyze a local Docker image directly:

```bash
layercompressinsight --image example/image:latest
```

Write JSON output:

```bash
layercompressinsight --tarball image.tar --output-format json --output report.json
```

The default threshold is `0.5`. A layer is flagged only when its estimated gzip
ratio is **strictly greater** than the threshold. A ratio equal to the threshold
is not flagged. Higher ratios mean the local gzip estimate compressed the raw
layer tar less effectively.

## Markdown output

```text
# Layer Compression Estimate

Estimated gzip ratio = deterministic gzip size / raw layer tar size.
Layers strictly above 0.500 are flagged as poorly compressible.

| Layer | Path | Raw layer tar (bytes) | Estimated gzip (bytes) | Estimated ratio | Flag |
|------:|------|----------------------:|-----------------------:|----------------:|------|
| 1 | abc/layer.tar | 10240 | 184 | 0.018 |  |
```

## JSON output

```json
{
  "metric": "deterministic_gzip_estimate",
  "threshold": 0.5,
  "threshold_behavior": "poor_compression_when_estimated_gzip_ratio_is_strictly_greater",
  "layers": [
    {
      "index": 1,
      "path": "abc/layer.tar",
      "raw_layer_tar_size": 10240,
      "estimated_gzip_size": 184,
      "estimated_gzip_ratio": 0.01796875,
      "file_content_size": 1000,
      "flag": null
    }
  ]
}
```

## Limitations

- The estimate is not a registry measurement and does not include registry-side
  processing.
- Reports retain the order of the first image entry in `manifest.json`; they are
  not sorted by ratio.
- Multi-image archives are currently analyzed using the first manifest entry.
- The tool reports measurements and flags. It does not prescribe Dockerfile or
  layer-refactoring changes.
- `--image` invokes `docker save` locally. Tarball mode does not require Docker.

## License

MIT License. See [LICENSE](LICENSE).

## Support

If this project saved you time, optional support is welcome:
https://paypal.me/Damonwill
