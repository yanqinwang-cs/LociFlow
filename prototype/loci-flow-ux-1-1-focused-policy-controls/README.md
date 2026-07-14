# Loci Flow UX 1.1 — focused policy controls prototype

This standalone browser prototype keeps the established Loci Flow layout and visual system while making the organization workflow explicit, reversible, and internally deterministic.

## What this pass demonstrates

- Six primary pages: Home, Organize, Duplicate Bin, Activity, Standards, and Settings.
- A decision-first Organize page with separate **Recommended changes** and **Choices needed** sections.
- **Accept all recommended changes** is literal: its count and accepted records come from the same pending-recommendation collection. Ambiguous moves and duplicate choices stay in Choices needed.
- Active Standards filter authored candidate changes before visible classification. Protected files stay in place and may appear only in the non-actionable **Kept by Standards** summary; they do not appear as routine blocked suggestions or in **Where files will go**.
- Cautious, Standard, and Proactive rebuild the authored sample candidates, apply active Standards, and create no Activity record.
- A compact, fixture-driven natural-language interpretation layer on Standards. It converts authored intent into structured **Suggested Standards** for review and confirmation. No LLM is connected, and the guided preset builder remains available.
- The ambiguous “Organize files by name” fixture requires clarification. The vague removal fixture is rejected and offers safe alternatives.
- Final approval, sample movement, and deterministic verification are separate steps: **Review and approve**, **Approve test move**, then **Verify test move**.
- Activity is created only after verification completes. The latest completed organization movement exposes **Undo move** whether its details are collapsed or expanded.
- Undo restores the complete pre-move sample snapshot: file locations, names, created-folder state, Duplicate Bin entries, and planning collections. It adds one **Restored previous organization** Activity entry.
- **Done** returns Home to an idle workflow with no highlighted stage, **Everything is organized**, and **Check again** as the main action.
- Duplicate Bin remains a reversible table; Settings remains limited to Appearance, Notifications, Local AI, Privacy, and Advanced.
- One synchronized Light/Dark theme state, compact rows, familiar local SVG file icons, and the existing indigo visual language.

## Deterministic sample pipeline

The in-memory sample uses one authoritative boundary:

```text
authored sample files
→ organization style
→ candidate changes
→ active Standards
→ remaining allowed changes
→ Recommended changes / Choices needed
→ accepted changes
→ Where files will go
```

All visible counts derive from the collections that render the corresponding rows.

Natural-language fixtures follow a second explicit boundary:

```text
ordinary-language intent
→ suggested structured Standards
→ user confirmation
→ active deterministic Standards
```

Interpretation never moves or renames files, approves changes, alters Duplicate Bin, or creates Activity.

## Safety boundary and limitations

The prototype uses authored sample state only. It has no backend and no persistence, real filesystem access, network calls, LLM connection, scheduling, telemetry, or external runtime. **Approve test move** changes only in-memory sample data; reloading restores defaults.

There is no drag-and-drop, slider, substantial animation, production movement, system Trash integration, or permanent Duplicate Bin deletion.

## Run locally

From the repository root:

```bash
python3 -m http.server 8003 \
  --directory prototype/loci-flow-ux-1-1-focused-policy-controls
```

Open `http://127.0.0.1:8003/#/home`.

## Preserved comparisons

Earlier prototypes remain unchanged comparison records:

- `prototype/loci-flow-ux-1-1-pass-1/`
- `prototype/loci-flow-ux-1-1-focused/`
- `prototype/loci-flow-ux-1-1-policy-controls/`
