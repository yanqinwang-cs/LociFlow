# Development Guide

## Setup

```bash
python3 -m pip install -e ".[web,web-test]"
PYTHONPATH=src python3 -m unittest discover -s tests
```

The suite uses only `unittest`. The optional `web` and `web-test` extras are needed for the web tests.

## Branch Workflow

```bash
git switch previous-stage-branch
git pull origin previous-stage-branch
git switch -c next-stage-branch
git status
git diff
git add docs/README.md
git commit -m "Add stage documentation"
git push -u origin next-stage-branch
```

Add only intended files. Do not add `test_scan/`.

## Coding Conventions

- Use `pathlib`.
- Use dataclasses for shared records.
- Use type hints.
- Prefer the standard library.
- Sort deterministically before returning user-visible lists.
- Keep runtime behavior dry-run unless the stage explicitly introduces approved execution.

## Adding A Review Heuristic

Add detection in `review.py`, keep one category per file, add tests, and use cautious language.

## Adding A Grouping Signal

Add deterministic extraction or grouping in `grouping.py`, preserve one-group-per-file rules, and test ordering.

## Adding An LLM Provider

Add a provider client beside `ollama_client.py`, keep prompts in `llm_refinement.py`, and validate output before use.

## Adding A CLI Flag

Wire the flag in `cli.py`, keep undo as a separate operation, and add subprocess tests.
