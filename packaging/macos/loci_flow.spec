# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_submodules


PACKAGING_ROOT = Path(SPECPATH).resolve()
PROJECT_ROOT = PACKAGING_ROOT.parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
WEB_ROOT = SOURCE_ROOT / "organizer" / "web"
ICON_PATH = PACKAGING_ROOT / "build" / "LociFlow.icns"
RUNTIME_IDENTITY_PATH = PACKAGING_ROOT / "build" / "release_identity.json"

sys.path.insert(0, str(PACKAGING_ROOT))
from release_metadata import (  # noqa: E402
    APP_NAME,
    BUILD_NUMBER,
    BUNDLE_IDENTIFIER,
    MINIMUM_MACOS_VERSION,
    SEMANTIC_VERSION,
    SUPPORTED_ARCHITECTURE,
)

required_resources = (
    WEB_ROOT / "templates",
    WEB_ROOT / "static" / "css",
    WEB_ROOT / "static" / "js",
    WEB_ROOT / "static" / "loci_flow",
    WEB_ROOT / "static" / "vendor",
    WEB_ROOT / "static" / "THIRD_PARTY_NOTICES.txt",
)
missing_resources = [path for path in required_resources if not path.exists()]
if missing_resources:
    raise SystemExit("Required Loci Flow resources are unavailable.")
if not ICON_PATH.is_file():
    raise SystemExit("Generated Loci Flow icon is unavailable.")
if not RUNTIME_IDENTITY_PATH.is_file():
    raise SystemExit("Generated Loci Flow runtime identity is unavailable.")

datas = [
    (
        str(RUNTIME_IDENTITY_PATH),
        "organizer/macos_launcher",
    ),
    (str(WEB_ROOT / "templates"), "organizer/web/templates"),
    (str(WEB_ROOT / "static" / "css"), "organizer/web/static/css"),
    (str(WEB_ROOT / "static" / "js"), "organizer/web/static/js"),
    (
        str(WEB_ROOT / "static" / "loci_flow"),
        "organizer/web/static/loci_flow",
    ),
    (str(WEB_ROOT / "static" / "vendor"), "organizer/web/static/vendor"),
    (
        str(WEB_ROOT / "static" / "THIRD_PARTY_NOTICES.txt"),
        "organizer/web/static",
    ),
]

hiddenimports = sorted(
    set(
        collect_submodules("organizer")
        + ["AppKit", "Foundation", "objc", "PyObjCTools"]
    )
)

analysis = Analysis(
    [str(SOURCE_ROOT / "organizer" / "macos_launcher" / "frozen_main.py")],
    pathex=[str(SOURCE_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", "prototype", "research"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=SUPPORTED_ARCHITECTURE,
    codesign_identity=None,
    entitlements_file=None,
)
collected = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
app = BUNDLE(
    collected,
    name=f"{APP_NAME}.app",
    icon=str(ICON_PATH),
    bundle_identifier=BUNDLE_IDENTIFIER,
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": SEMANTIC_VERSION,
        "CFBundleVersion": str(BUILD_NUMBER),
        "CFBundleIconFile": ICON_PATH.name,
        "LSMinimumSystemVersion": MINIMUM_MACOS_VERSION,
        "NSHighResolutionCapable": True,
    },
)
