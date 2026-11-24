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

This stage produces an Apple Silicon `arm64` application only. Intel Macs are
not supported by this artifact, Rosetta is not the support strategy, and no
`x86_64` or universal2 compatibility is claimed. A future x86_64 artifact
would require its own controlled Intel builder and physical test machine.

After PyInstaller finishes, the build runs the checked-in bundle validator.
It verifies exact bundle metadata and required resources, discovers every
embedded Mach-O recursively, requires every native file to contain exactly one
`arm64` slice, and runs the hidden frozen smoke check. The build reports
success only after all checks pass.

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

## Bundle and frozen-workflow acceptance

Validate an explicit application copy:

```sh
uv run --extra macos-build \
  python packaging/macos/validate_app_bundle.py \
  "/path/to/Loci Flow.app"
```

The deterministic report contains only release identity, architecture and
resource summaries. It contains no repository, home or selected-root path.

An unsigned artifact copied outside the repository can run the controlled
workflow acceptance:

```sh
"/path/to/Loci Flow.app/Contents/MacOS/Loci Flow" \
  --packaging-workflow-acceptance
```

This hidden check uses disposable application-home and selected-root
directories. It exercises the real loopback server, authenticated launch,
Home, one Check request, process leases and coordinated shutdown without
opening the default browser. It removes its disposable data afterward.

## Local normal-launch check

Open the application locally and select a disposable folder. Confirm that one
browser window opens, Home loads, a second native instance is rejected, and
Quit completes before another process can acquire the selected-root lease.

## Packaged launcher diagnostics

Normal windowed application launches keep a local, bounded launcher log at:

```text
~/Library/Logs/Loci Flow/launcher.log
```

The active file is limited to 256 KiB. Rotation retains three files total:
`launcher.log`, `launcher.log.1`, and `launcher.log.2`. The log directory and
files are private to the current macOS user.

Records contain only a UTC timestamp, application version, build number,
bounded launch phase and failure code, and an allowlisted exception class when
useful. They do not contain selected-root paths, filenames, browser URLs,
tokens, secrets, content hashes, operation identifiers, tracebacks, exception
messages, or workflow records. Unknown values are replaced with generic
categories.

There is no telemetry, networking, or automatic upload. A tester may
voluntarily provide these launcher log files when reporting a startup problem;
selected folders and `AI_Review` records should not be supplied by default.
The diagnostics are troubleshooting evidence only and are never workflow
authority. If the log cannot be created or written safely, launch and shutdown
continue without it.

This unsigned artifact is for local acceptance only. macOS may report that it
is from an unidentified developer. Do not disable Gatekeeper globally. Signed
closed-beta distribution requires the later signing, Hardened Runtime,
notarisation, and distribution stages.

Complete the manual OS integration checks in
`docs/LOCI_FLOW_MACOS_RELEASE_ACCEPTANCE.md` before any external beta
distribution.
