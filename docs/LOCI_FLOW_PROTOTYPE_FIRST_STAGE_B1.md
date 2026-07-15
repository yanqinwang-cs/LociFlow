# Loci Flow Prototype-First Stage B1

Stage B1 extends the accepted prototype-derived renderers with deterministic
fixture contracts only. The live application still defaults to
`accepted_default`, whose source fixture, visual structure, core stylesheet,
navigation, SVG assets, and sample interactions remain unchanged.

The new fixture contract supplies scope and freshness labels, Home workflow
states, structured recommendation rows, destination options, duplicate-copy
choices, protected-item summaries, an explicit movement tree, approval
readiness and counts, Ready-to-move rendering, stale-state guidance, and
disconnected Duplicate Bin, Activity, and Standards pages. Destination choices
retain opaque option values internally. Duplicate choices support keeping all
copies or selecting a non-empty proper subset for Duplicate Bin; no copy is
treated as an original.

The movement tree has two deliberate paths. The accepted fixture continues to
derive its sample tree exactly as before. Production-shaped fixtures pass a
complete tree model to the renderer, so the renderer does not infer
destinations, add protected items, or add unresolved choices.

Approval and Ready-to-move are display states in this stage. Approval changes
only in-memory fixture state, leaves Activity unchanged, and ends at **Ready to
move — no files have moved yet** with a disabled movement control. The
production-shaped Duplicate Bin, Activity, and Standards fixtures remain
explicitly disconnected and contain no sample records.

Stage B1 makes no HTTP request, reads no filesystem data, persists no browser
state, and adds no scan, decision, approval, movement, Activity, Undo,
Duplicate Bin, or Standards API behavior. Stage B2 can populate these stable
view-model contracts from the existing authenticated application APIs without
redesigning the renderer.
