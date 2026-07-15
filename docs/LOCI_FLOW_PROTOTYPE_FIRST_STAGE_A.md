# Loci Flow Prototype-First Frontend Stage A

The accepted UX 1.1 focused policy-controls prototype is the frontend source
of truth for Loci Flow. Its directory is an immutable reference snapshot and
is never loaded by the live application at runtime.

Stage A ports the prototype DOM, CSS, fixtures, store, renderers, interaction
grammar, local SVG assets, and Light/Dark themes into the authenticated
`/loci-flow` shell. The live shell is fixture-driven: sample state resets on
reload and the visible prototype warning states that no real files change.
Home and Organize retain their separate prototype renderers and pass through
separate frontend view-model builders. A small fixture data-source boundary
isolates sample loading so a later stage can replace data without redesigning
the renderers.

Backend organization data integration is deliberately deferred. The Stage A
frontend calls no organization, scan, Activity, decision, movement, or Undo
endpoint. D2B backend movement salvage is also deliberately deferred. The
existing backend handlers remain installed for compatibility, but they are
not used by this fixture shell.
