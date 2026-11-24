# Loci Flow signed macOS build

This procedure creates a Developer ID-signed, arm64 Loci Flow application in
an ignored release-staging directory. A signed application is not notarized
and is not ready for external distribution until the later notarization and
stapling stage succeeds.

## Prerequisites

- An active Apple Developer Program membership.
- A valid `Developer ID Application` certificate and associated private key
  in the signing user's keychain.
- The complete 40-character SHA-1 fingerprint for that identity.
- The exact 10-character Apple Developer Team ID.
- A fresh P2-B3 application built and validated at:

  ```text
  packaging/macos/dist/Loci Flow.app
  ```

List locally usable code-signing identities with:

```sh
security find-identity -p codesigning -v
```

Select the identity by its complete fingerprint. Do not select by certificate
display name, shortened hash, or list position. Do not copy fingerprints,
certificate exports, keychains, Apple Accounts, passwords, or other signing
credentials into the repository or diagnostic logs.

## Sign a staging copy

From the repository root:

```sh
python packaging/macos/sign_app.py \
  --source-app "packaging/macos/dist/Loci Flow.app" \
  --staging-directory \
    "packaging/macos/release-staging/0.1.0-beta.1-1-arm64" \
  --identity-fingerprint "<complete SHA-1 fingerprint>" \
  --expected-team-id "<10-character Team ID>"
```

The script validates the certificate type, private-key-backed identity,
certificate validity and Team ID before it copies or signs anything. It then:

1. validates the canonical P2-B3 application and its 12-object arm64 native
   closure;
2. records the canonical input digest;
3. copies it with `/usr/bin/ditto` into the owned release-staging directory;
4. signs every embedded native file in deterministic relative-path order;
5. signs the outer application last;
6. verifies Developer ID identity, Team ID, Hardened Runtime, secure
   timestamps, architecture and the absence of entitlements for every native
   item;
7. reruns strict signature, bundle, frozen smoke, frozen workflow, lease and
   shutdown validation;
8. confirms the canonical unsigned input digest is unchanged; and
9. reports the pre-notarization Gatekeeper assessment truthfully.

The canonical application under `packaging/macos/dist/` is never signed in
place. A failed operation removes only its explicitly owned staged application
and emits no signed result path.

## Entitlement policy

P3-B1 uses no entitlements. In particular, it does not grant:

- disabled library validation;
- JIT;
- unsigned executable memory;
- debugger attachment;
- DYLD environment exceptions.

All embedded native objects and the outer application are signed with
Hardened Runtime and secure timestamps using the same Developer ID identity.
If the real same-Team-ID build fails to launch, stop and preserve the exact
bounded failure for a separate security review. Do not add an exception to
make the release pass.

## Result and limitations

Success output contains only the staged path, fixed application identity,
version and build, architecture, native-code count, validation summaries and
the Gatekeeper assessment state. It does not print the supplied fingerprint,
Team account information, certificate serial, private-key details, browser
tokens, launch URLs or selected-root information.

Before notarization, Gatekeeper may reject an otherwise valid Developer
ID-signed application because it has no Apple notarization ticket. That result
must not be described as a signing failure, notarization success, or release
readiness.

P3-B1 does not upload to Apple, staple a ticket, create a submission ZIP or
create a DMG. Those steps belong to later, separately reviewed stages.
