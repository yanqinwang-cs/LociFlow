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

The script removes only `packaging/macos/build/` and
`packaging/macos/dist/` before rebuilding. It does not sign, notarise, staple,
or create a DMG.

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
