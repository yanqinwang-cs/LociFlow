# Loci Flow unsigned macOS build

This procedure creates a local, native-architecture, unsigned Loci Flow
application for packaging acceptance. The result is not ready for external
distribution.

## Prerequisites

- macOS 13 or later
- the repository’s supported Python and `uv`
- the current checkout on the intended native build architecture

Install the isolated build dependencies:

```sh
uv sync --extra web --extra macos-launcher --extra macos-build
```

## Build

From the repository root:

```sh
uv run --extra web --extra macos-launcher --extra macos-build \
  python packaging/macos/build_unsigned_app.py
```

The generated application is:

```text
packaging/macos/dist/Loci Flow.app
```

The build generates the LF application icon automatically before PyInstaller
runs. Its reviewed 1024×1024 source raster is:

```text
packaging/macos/icon/loci-flow-app-icon-1024.png
```

The deterministic iconset and assembled ICNS are generated at:

```text
packaging/macos/build/LociFlow.iconset/
packaging/macos/build/LociFlow.icns
```

Those generated files are ignored build outputs and must not be staged. The
source raster is a tracked release resource that reproduces the accepted LF
mark; it is not a second independently editable logo.

The script removes only `packaging/macos/build/` and
`packaging/macos/dist/` before rebuilding. It does not sign, notarise, staple,
or create a DMG.

This stage produces an `arm64` application only. It makes no `x86_64` or
universal2 compatibility claim.

The bundle identifier remains `com.yanqinwang.lociflow`. Human-readable beta
versions advance as tester-visible releases are published, while the numeric
`CFBundleVersion` increases monotonically for every distributed artifact
revision. These values are checked in centrally and are not inferred from the
local Git checkout.

## Packaged-resource smoke check

Copy the generated application to a disposable directory outside the
repository, then run:

```sh
"/path/to/Loci Flow.app/Contents/MacOS/Loci Flow" --packaging-smoke-test
```

The command exits zero only when the embedded Python runtime, production
imports, PyObjC modules, web templates, static resources, LF assets, vendor
notice, and application-owned path construction are available. It does not
open the chooser, select a root, start the server, create a browser token, or
open a browser.

## Local normal-launch check

Open the application locally and select a disposable folder. Confirm that one
browser window opens, Home loads, a second native instance is rejected, and
Quit completes before another process can acquire the selected-root lease.

This unsigned artifact is for local acceptance only. macOS may report that it
is from an unidentified developer. Do not disable Gatekeeper globally. Signed
closed-beta distribution requires the later signing, Hardened Runtime,
notarisation, and distribution stages.
