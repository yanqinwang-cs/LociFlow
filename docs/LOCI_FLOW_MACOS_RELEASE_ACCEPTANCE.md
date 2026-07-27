# Loci Flow macOS release acceptance

These manual OS-owned checks are required before any external beta
distribution. Run them against the exact copied artifact that passed bundle,
architecture, smoke and frozen-workflow validation.

## Artifact identity

- Confirm the artifact is the expected arm64-only beta build.
- Confirm Finder shows the LF application icon.
- Confirm the Dock shows the LF application icon while running.
- Confirm no Terminal window appears.

## Native launch and root selection

- Open the application and confirm the real macOS folder chooser appears.
- Cancel the chooser and confirm the application exits cleanly.
- Reopen it, select a disposable valid root, and confirm the selection is
  accepted.
- Confirm the default browser opens exactly once.
- Confirm Home loads visually with its CSS, JavaScript and LF mark.
- Confirm a second native instance shows the bounded global-instance alert.
- Confirm another process using the selected root shows the bounded
  root-in-use alert.

## Shutdown and reopen

- Use Command-Q while idle and confirm the browser server closes.
- Use Command-Q during active safe work and confirm the finishing state waits
  for that work before termination.
- Confirm no traceback, private launch URL or token is displayed.
- Reopen the application after Quit and confirm the root and application
  leases were released.

## Local diagnostics

- Confirm the local launcher log is under
  `~/Library/Logs/Loci Flow/launcher.log`.
- Confirm records contain only bounded release, phase, code and exception-class
  fields.
- Confirm no selected-root path, filename, launch URL, token, secret,
  operation identifier, traceback or exception message is present.
- Confirm no telemetry or automatic upload occurs.

Do not bypass Gatekeeper globally. This unsigned checklist does not claim
signing, Hardened Runtime, notarisation, stapling or DMG readiness.
