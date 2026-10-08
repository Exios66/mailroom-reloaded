# Parity record: vendored llm-dojo-scoring subset

- Upstream: `/home/user/src-ro/llm-dojo-scoring` (github.com/Exios66/llm-dojo-scoring)
- Tag: `v0.21.0`
- Commit: `6a3053ccc32b4f3290a979ac75265affa77a6503`

Modules are copied from `llm_dojo_scoring/`. Sha256 columns: upstream file, vendored file.

| module | upstream sha256 | vendored sha256 | change |
| --- | --- | --- | --- |
| config.py | 3ef62e7eb87970d167c3e050bb951b9cb8a33b4c4feac2ac5c302252ab301733 | 3ef62e7eb87970d167c3e050bb951b9cb8a33b4c4feac2ac5c302252ab301733 | verbatim |
| gt_metadata.py | 51726dd79d12096c720be98deeae7bb7f4d433c79454acde83c126549160fd4f | 51726dd79d12096c720be98deeae7bb7f4d433c79454acde83c126549160fd4f | verbatim |
| intents.py | 8f762c02785b7bfecf3c73864c756c7f64a442edcfd954c50d556a2c38d95411 | 8f762c02785b7bfecf3c73864c756c7f64a442edcfd954c50d556a2c38d95411 | verbatim |
| equivalences.py | 3a8dabc75fce5eba791feb836b8143d813405d7c218c88949b03f85bbd5a5ad3 | 3a8dabc75fce5eba791feb836b8143d813405d7c218c88949b03f85bbd5a5ad3 | verbatim |
| corpus.py | b5ec80a51e63a33db6e1dd9cb3e543ea92ce6d2b42589cecb7d704c427474479 | b5ec80a51e63a33db6e1dd9cb3e543ea92ce6d2b42589cecb7d704c427474479 | verbatim |
| field_scoring.py | 439998e1f38254be42acb64066b484122b6a266f9ed152e656534eb89135e426 | dcc5a4b4794227e2a8be0781d4e15a1087683b29a6b01992139f432df3562a9a | lazy `from .mailroom import EXTRACT_CLASS_ALIASES` replaced by module-level empty dict (upstream value is `{}`); ambiguity uses `field_is_ambiguous` with the original field type before containment override |
| extraction_metrics.py | 3a7287881cc79cf5efdedc0deacf4d65c2de548d57e9416aa311efef80e8288e | 3a7287881cc79cf5efdedc0deacf4d65c2de548d57e9416aa311efef80e8288e | verbatim |
| classification.py | afa755de5abf0c644f6e5d89f10bb483204dea89470c0f4c81ec6e400b46bbf6 | 1f1ef8914313a9f324b686ca2bb7d5bba1be07a02951e2da2785716e15e7e4de | `from llm_dojo_scoring.config` -> `from .config` |
| maud.py | 80df2b212da16820c5aa9b1315a6a7c3f3572906b33fb1c9381095220d05e3dd | 80df2b212da16820c5aa9b1315a6a7c3f3572906b33fb1c9381095220d05e3dd | verbatim |
| intake.py | 0f2935159c49c67fa8c2e8e71cd19d88a1779a57363f772fa877d364837a42ee | 0f2935159c49c67fa8c2e8e71cd19d88a1779a57363f772fa877d364837a42ee | verbatim |
| trace_knobs.py | 34bc2ba5f5c50086a3653c7f6dd722f844ba357b15f57177a4919eae3b3ce3bb | 34bc2ba5f5c50086a3653c7f6dd722f844ba357b15f57177a4919eae3b3ce3bb | verbatim; added to the set because `score_extraction` calls `capture_trace_knobs` |

`scoring/__init__.py` is new (re-exports plus `subclass_vocab`, which reads `corpus.DOC_TYPE_SUBCLASSES`, built from `config.CONTRACT_SUBTYPE_KEYS` and `config.MAUD_CONSIDERATION_TYPES`).

Not vendored: every other upstream module (suites, mailroom, reporting, sync, CLI).

Parity test: `tests/scoring/test_parity.py` (needs the `parity` extra; skipped otherwise). Fixtures: `tests/scoring/fixtures/pairs.json`.
