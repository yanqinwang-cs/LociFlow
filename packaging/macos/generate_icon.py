from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

from PIL import Image


PACKAGING_ROOT = Path(__file__).resolve().parent
SOURCE_ICON = PACKAGING_ROOT / "icon" / "loci-flow-app-icon-1024.png"
BUILD_ROOT = PACKAGING_ROOT / "build"
ICONSET_NAME = "LociFlow.iconset"
ICNS_NAME = "LociFlow.icns"
ICONUTIL = Path("/usr/bin/iconutil")

ICONSET_FILES = (
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
)


class IconGenerationError(RuntimeError):
    pass


def generate_icon(
    *,
    source_icon: Path = SOURCE_ICON,
    build_root: Path = BUILD_ROOT,
    iconutil: Path = ICONUTIL,
) -> Path:
    if sys.platform != "darwin":
        raise IconGenerationError("macOS icon generation requires macOS")
    if not source_icon.is_file():
        raise IconGenerationError("release icon source is unavailable")
    if not iconutil.is_file():
        raise IconGenerationError("macOS iconutil is unavailable")

    try:
        with Image.open(source_icon) as opened:
            opened.load()
            if opened.size != (1024, 1024):
                raise IconGenerationError(
                    "release icon source must be exactly 1024 by 1024"
                )
            if opened.mode != "RGBA":
                raise IconGenerationError(
                    "release icon source must use RGBA colour"
                )
            source = opened.copy()
    except IconGenerationError:
        raise
    except (OSError, ValueError) as error:
        raise IconGenerationError("release icon source is invalid") from error

    iconset = build_root / ICONSET_NAME
    icns = build_root / ICNS_NAME
    if iconset.exists():
        shutil.rmtree(iconset)
    if icns.exists():
        icns.unlink()
    iconset.mkdir(parents=True)

    for filename, size in ICONSET_FILES:
        image = (
            source.copy()
            if size == 1024
            else source.resize(
                (size, size),
                resample=Image.Resampling.LANCZOS,
                reducing_gap=3.0,
            )
        )
        destination = iconset / filename
        image.save(
            destination,
            format="PNG",
            optimize=False,
            compress_level=9,
        )
        with Image.open(destination) as generated:
            if generated.size != (size, size) or generated.mode != "RGBA":
                raise IconGenerationError(
                    f"generated icon size is invalid: {filename}"
                )

    completed = subprocess.run(
        [
            str(iconutil),
            "--convert",
            "icns",
            "--output",
            str(icns),
            str(iconset),
        ],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise IconGenerationError("macOS iconutil could not assemble the icon")
    _validate_icns(icns)
    return icns


def _validate_icns(path: Path) -> None:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise IconGenerationError("generated ICNS is unavailable") from error
    if (
        len(payload) < 8
        or payload[:4] != b"icns"
        or int.from_bytes(payload[4:8], "big") != len(payload)
    ):
        raise IconGenerationError("generated ICNS is invalid")


def main() -> int:
    try:
        generated = generate_icon()
    except IconGenerationError as error:
        print(f"Loci Flow icon generation failed: {error}", file=sys.stderr)
        return 1
    print(generated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

