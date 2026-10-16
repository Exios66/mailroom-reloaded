# Parity record: vendored llm-dojo-scoring subset

- Upstream: github.com/Exios66/llm-dojo-scoring
- Commit: `8c7b7f1c9c3b18a7036ee58addcfaa35d816177d` (branch `claude/cool-wozniak-o044u4`)
- Tag: none yet. **Interim:** this pin becomes `v0.22.0` once the dojo tags its release; re-pin then and update this file and the references listed under "Pins" below.
- Previous pin: `v0.21.0` (`6a3053ccc32b4f3290a979ac75265affa77a6503`).

Modules are copied from `llm_dojo_scoring/` at the pinned commit. Sha256 columns: upstream file, vendored file.

Re-vendor procedure (no script; the repo has none): for each module in the table, copy `llm_dojo_scoring/<module>.py` from the pinned commit verbatim, then apply only the edits listed in the `change` column. `diff` each vendored file against its upstream file; only those edits may show.

| module | upstream sha256 | vendored sha256 | change |
| --- | --- | --- | --- |
| config.py | 3ef62e7eb87970d167c3e050bb951b9cb8a33b4c4feac2ac5c302252ab301733 | 3ef62e7eb87970d167c3e050bb951b9cb8a33b4c4feac2ac5c302252ab301733 | verbatim |
| gt_metadata.py | 4def2e5f1e9240e62aa6a80174a77b3150265e70b2cc59140acd92d1563ef283 | 4def2e5f1e9240e62aa6a80174a77b3150265e70b2cc59140acd92d1563ef283 | verbatim |
| intents.py | 8f762c02785b7bfecf3c73864c756c7f64a442edcfd954c50d556a2c38d95411 | 8f762c02785b7bfecf3c73864c756c7f64a442edcfd954c50d556a2c38d95411 | verbatim |
| equivalences.py | 3a8dabc75fce5eba791feb836b8143d813405d7c218c88949b03f85bbd5a5ad3 | 3a8dabc75fce5eba791feb836b8143d813405d7c218c88949b03f85bbd5a5ad3 | verbatim |
| corpus.py | b5ec80a51e63a33db6e1dd9cb3e543ea92ce6d2b42589cecb7d704c427474479 | b5ec80a51e63a33db6e1dd9cb3e543ea92ce6d2b42589cecb7d704c427474479 | verbatim |
| field_scoring.py | 2589ce6216880329227b0c222eb366a37ce0cf8c97501866766dac7d004a411f | d6a5e6a6e0e7e3bf950267c2b14e92d65f062a9847f19f170d31d4e6ef0e510c | lazy `from .mailroom import EXTRACT_CLASS_ALIASES` replaced by module-level empty dict (upstream value is `{}`). The judge-escalation band is no longer a local patch: upstream `score_extraction` calls `field_is_ambiguous(original_field_type, score)` (dojo CHANGELOG `[Unreleased]`, "Judge escalation uses the per-type bands"). |
| extraction_metrics.py | 3a7287881cc79cf5efdedc0deacf4d65c2de548d57e9416aa311efef80e8288e | 3a7287881cc79cf5efdedc0deacf4d65c2de548d57e9416aa311efef80e8288e | verbatim |
| classification.py | afa755de5abf0c644f6e5d89f10bb483204dea89470c0f4c81ec6e400b46bbf6 | 1f1ef8914313a9f324b686ca2bb7d5bba1be07a02951e2da2785716e15e7e4de | `from llm_dojo_scoring.config` -> `from .config` |
| maud.py | 2dae49ace7d7f6aa8dbeff546bba9c89fa292920032f457819176e3e0d1a49b7 | 2dae49ace7d7f6aa8dbeff546bba9c89fa292920032f457819176e3e0d1a49b7 | verbatim |
| intake.py | 0f2935159c49c67fa8c2e8e71cd19d88a1779a57363f772fa877d364837a42ee | 0f2935159c49c67fa8c2e8e71cd19d88a1779a57363f772fa877d364837a42ee | verbatim |
| trace_knobs.py | 34bc2ba5f5c50086a3653c7f6dd722f844ba357b15f57177a4919eae3b3ce3bb | 34bc2ba5f5c50086a3653c7f6dd722f844ba357b15f57177a4919eae3b3ce3bb | verbatim; added to the set because `score_extraction` calls `capture_trace_knobs` |

Behaviour changes since v0.21.0 that arrive with the re-vendor (all upstream, none local):

- `field_scoring.score_extraction`: insurance `claim_type` folds a CMS table token onto `health` when the GT is `health` (`canonical_claim_type`).
- `gt_metadata.ANNOTATION_KEYS` adds `expected_subclass`, so it is no longer scored as a field.
- `maud`: comparators are spelled out before folding, multi-class answers are order-insensitive, and composite classes canonicalize to the sorted set of their parts.
- `field_scoring.embed_texts` is new upstream; it is vendored but no mailroom code calls it.

`scoring/__init__.py` is new (re-exports plus `subclass_vocab`, which reads `corpus.DOC_TYPE_SUBCLASSES`, built from `config.CONTRACT_SUBTYPE_KEYS` and `config.MAUD_CONSIDERATION_TYPES`).

Not vendored: every other upstream module (suites, mailroom, reporting, sync, CLI, emitter, registry, archive, serving).

Parity test: `tests/scoring/test_parity.py` (needs the `parity` extra; skipped otherwise). Fixtures: `tests/scoring/fixtures/pairs.json`.

## Pins

Every place that records the vendored pin, all at `8c7b7f1` (or the interim commit above):

- `pyproject.toml` `[project.optional-dependencies] parity` and the `[tool.ruff]` comment.
- `uv.lock` `llm-dojo-scoring` source (regenerated with `uv lock --upgrade-package llm-dojo-scoring`, not hand-edited).
- `scoring/__init__.py` module docstring.
- `README.md` release line.
- `AGENTS.md` §4 directory map and §6 parity note.
- `docs/ARCHITECTURE.md` module map.
- this file.
