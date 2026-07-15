# Loci Flow Prototype-First Stage B2

Stage B2 keeps the committed Stage B1 renderers as the authoritative visual and
interaction contract. The production `/loci-flow` bootstrap now supplies those
renderers through a validated browser adapter backed by the existing organization
application services.

## Connected in this stage

- `POST /scan?surface=home` starts the existing authenticated scan without leaving
  the Loci Flow shell. The browser polls the existing organization endpoint at a
  fixed interval until the scan completes or fails.
- `GET /api/organization/current` supplies the real scope, freshness, deterministic
  recommendations, destination and exact-duplicate choices, Standards protections,
  movement tree, decision revision, and approval summary.
- `POST /api/organization/decisions` records temporary recommendation, destination,
  and duplicate decisions. Bulk acceptance submits the exact current pending IDs.
- `POST /api/organization/approval` creates the existing real, in-memory
  Ready-to-move state. Reload restores that state while the root-bound process and
  authenticated browser session remain active.

Production does not load `fixtures.json` or select a Stage B1 scenario. The fixture
data source remains available through an explicit asynchronous test helper only;
there is no runtime fallback when the backend is unavailable or malformed.

## Adapter and safety boundary

The API client uses fixed same-origin URLs, same-origin credentials, JSON response
acceptance, URL-encoded POST bodies, and the CSRF token rendered into the shell.
Mutations submit only the server-issued scan generation, decision revision, change,
choice, option, and duplicate-copy IDs required by the existing contract. They never
submit a root, source path, destination path, plan, count, hash, or organization
style.

The data source validates every completed response before view-model construction.
It rejects missing fields, inconsistent exact counts, unsafe root-relative paths,
duplicate category membership, invalid option/copy membership, and inconsistent
review summaries. The browser formats timestamps, sizes, breadcrumbs, file-kind
labels, and known reason codes without changing authoritative values or inferring
destinations, duplicate identity, readiness, or the movement tree.

Only one scan poll loop and one decision/approval mutation may be active at a time.
Generation, revision, changed recommendation set, and unavailable-option failures
reload or surface the accepted stale states without optimistic authoritative edits.

## Deterministic validation fixture

The fixture is inherited from
`tests/test_application_organization_slice_service.py` and
`tests/test_application_organization_decision_service.py`:

```sh
ROOT="$(mktemp -d /tmp/loci-flow-stage-b2.XXXXXX)"
mkdir -p "$ROOT/Organized/EvoSim_project_slides/slides"
printf a > "$ROOT/EvoSim_project_slides.pptx"
printf b > "$ROOT/EvoSim_project_slides_final.pptx"
```

The expected result is exactly two ordinary recommendations, zero choices, and the
reason code `matched_existing_group`. The destinations are:

- `Organized/EvoSim_project_slides/slides/EvoSim_project_slides.pptx`
- `Organized/EvoSim_project_slides/slides/EvoSim_project_slides_final.pptx`

The destination folder exists, neither destination file exists, the sources are not
protected or duplicates, and the planner needs no fixture-only behavior.

## Deliberately disconnected

Movement remains disabled as **Move files — not connected yet** and the Ready state
continues to say **No files have moved yet.** Stage B2 makes no movement-preview,
movement, Activity, Undo, or Duplicate Bin execution request. Activity shows no file
moves, Duplicate Bin shows no fixture files, and Standards editing remains
disconnected. Light and Dark appearance remains in memory only; organization styles
do not alter backend behavior.

The next integration work connects D2A Activity and durable Undo, followed by the
separately preserved D2B browser-movement release gate. Executor ownership and all
filesystem movement remain outside this stage.
