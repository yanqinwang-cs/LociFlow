# Loci Flow Prototype-First Stage B3

Stage B3 connects the accepted Activity page to the existing durable Activity
and Undo backend. `GET /api/activity` is now the production source for both
organization movement records and their linked restore records. The backend
continues to own chronological ordering and is the sole owner of Undo
eligibility through each entry's `undoable` value.

The browser validates the complete browser-safe Activity response before it
renders anything. It accepts canonical operation IDs, documented statuses and
counts, timezone-aware timestamps, and safe root-relative detail paths. It
does not inspect the filesystem, select an older movement, compare file
metadata, reconstruct a restore plan, expose internal artifact paths, or infer
eligibility from status or list position. Machine statuses are adapted to the
accepted product labels and count summaries without changing the locked
Activity renderer or stylesheet.

Undo is an explicit two-request flow. The browser first posts only CSRF to the
operation-bound preview route. A valid preview supplies a bounded opaque token
and authoritative file count for the confirmation dialog. Confirming posts
only CSRF, that token, and the exact server-required confirmation. Closing the
dialog discards the token, and the final Undo request is never retried
automatically. After success, a known stale result, or an uncertain network
result, Activity reloads from the durable backend rather than creating a
client-only restore row.

Activity has production loading, empty, retryable error, expansion, and
collapse behavior. Known Undo errors are translated to concise safe language;
raw executor messages and machine codes are not rendered. The final action is
announced through the existing live region.

Browser movement remains disabled. Stage B3 does not add a movement preview or
movement POST, does not connect the D1 execution service to web routes, and
does not change the Stage B2 Ready state. Duplicate Bin actions and restoration
also remain disconnected. No prototype, template, renderer, fixture, SVG, or
CSS redesign is part of this stage.

The next planned Stage B4 can connect the already-proven browser movement
service while preserving the same execution authorization, shared lock,
durable operation, and verification boundaries.
