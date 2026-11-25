from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGING_ROOT = REPOSITORY_ROOT / "packaging" / "macos"
sys.path.insert(0, str(PACKAGING_ROOT))

from generate_icon import (  # noqa: E402
    ICONSET_FILES,
    IconGenerationError,
    generate_icon,
)

SOURCE_ICON = (
    PACKAGING_ROOT / "icon" / "loci-flow-app-icon-1024.png"
)
AUTHORITATIVE_MARK = (
    REPOSITORY_ROOT
    / "src"
    / "organizer"
    / "web"
    / "static"
    / "loci_flow"
    / "assets"
    / "loci-flow-mark.svg"
)
PROTOTYPE_MARK = (
    REPOSITORY_ROOT
    / "prototype"
    / "loci-flow-ux-1-1-focused-policy-controls"
    / "assets"
    / "loci-flow-mark.svg"
)


class MacOSIconGenerationTests(unittest.TestCase):
    def test_authoritative_mark_is_the_accepted_font_free_product_mark(
        self,
    ) -> None:
        source = AUTHORITATIVE_MARK.read_bytes()
        self.assertEqual(source, PROTOTYPE_MARK.read_bytes())
        decoded = source.decode("utf-8").lower()
        self.assertNotIn("<text", decoded)
        self.assertNotIn("@font", decoded)
        self.assertNotIn("gradient", decoded)

    def test_release_raster_is_exact_metadata_free_rgba_source(self) -> None:
        generator = (
            PACKAGING_ROOT / "generate_icon.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("ImageFont", generator)
        self.assertNotIn("font", generator.lower())
        with Image.open(SOURCE_ICON) as image:
            image.load()
            self.assertEqual(image.size, (1024, 1024))
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.info, {})
            self.assertEqual(image.getpixel((0, 0)), (18, 18, 23, 255))
            colours = image.getcolors(maxcolors=1024 * 1024)
            self.assertIsNotNone(colours)
            assert colours is not None
            present = {colour for _count, colour in colours}
            self.assertIn((247, 247, 251, 255), present)
            self.assertIn((91, 91, 214, 255), present)

    @unittest.skipUnless(
        sys.platform == "darwin" and Path("/usr/bin/iconutil").is_file(),
        "macOS icon generation requires iconutil",
    )
    def test_complete_icon_output_is_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory) / "build"
            first_icns = generate_icon(build_root=build)
            first = _output_hashes(build, first_icns)
            second_icns = generate_icon(build_root=build)
            second = _output_hashes(build, second_icns)

        self.assertEqual(first, second)
        self.assertEqual(
            set(first),
            {name for name, _size in ICONSET_FILES} | {"LociFlow.icns"},
        )

    @unittest.skipUnless(
        sys.platform == "darwin" and Path("/usr/bin/iconutil").is_file(),
        "macOS icon generation requires iconutil",
    )
    def test_generated_names_dimensions_modes_and_icns_structure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory) / "build"
            icns = generate_icon(build_root=build)
            iconset = build / "LociFlow.iconset"

            self.assertEqual(
                {path.name for path in iconset.iterdir()},
                {name for name, _size in ICONSET_FILES},
            )
            for filename, size in ICONSET_FILES:
                with self.subTest(filename=filename):
                    with Image.open(iconset / filename) as image:
                        self.assertEqual(image.size, (size, size))
                        self.assertEqual(image.mode, "RGBA")
                        self.assertEqual(image.info, {})
            payload = icns.read_bytes()
            self.assertEqual(payload[:4], b"icns")
            self.assertEqual(int.from_bytes(payload[4:8], "big"), len(payload))

    @unittest.skipUnless(
        sys.platform == "darwin" and Path("/usr/bin/iconutil").is_file(),
        "macOS icon generation requires iconutil",
    )
    def test_missing_or_invalid_input_and_iconutil_fail_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            with self.assertRaisesRegex(
                IconGenerationError,
                "source is unavailable",
            ):
                generate_icon(
                    source_icon=temporary / "missing.png",
                    build_root=temporary / "build",
                )

            wrong_size = temporary / "wrong-size.png"
            Image.new("RGBA", (64, 64)).save(wrong_size)
            with self.assertRaisesRegex(IconGenerationError, "1024 by 1024"):
                generate_icon(
                    source_icon=wrong_size,
                    build_root=temporary / "build",
                )

            wrong_mode = temporary / "wrong-mode.png"
            Image.new("RGB", (1024, 1024)).save(wrong_mode)
            with self.assertRaisesRegex(IconGenerationError, "RGBA"):
                generate_icon(
                    source_icon=wrong_mode,
                    build_root=temporary / "build",
                )

            with self.assertRaisesRegex(
                IconGenerationError,
                "iconutil is unavailable",
            ):
                generate_icon(
                    build_root=temporary / "build",
                    iconutil=temporary / "missing-iconutil",
                )


def _output_hashes(build: Path, icns: Path) -> dict[str, str]:
    iconset = build / "LociFlow.iconset"
    paths = [iconset / name for name, _size in ICONSET_FILES]
    paths.append(icns)
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


if __name__ == "__main__":
    unittest.main()
