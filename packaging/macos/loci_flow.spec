# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


PROJECT_ROOT = Path(SPECPATH).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
WEB_ROOT = SOURCE_ROOT / "organizer" / "web"

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

datas = [
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
    name="Loci Flow",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
collected = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="Loci Flow",
)
app = BUNDLE(
    collected,
    name="Loci Flow.app",
    icon=None,
    bundle_identifier="com.yanqinwang.lociflow",
    info_plist={
        "CFBundleName": "Loci Flow",
        "CFBundleDisplayName": "Loci Flow",
        "CFBundleShortVersionString": "0.1.0-beta.1",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
    },
)
