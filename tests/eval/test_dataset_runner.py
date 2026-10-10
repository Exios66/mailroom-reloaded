"""Task 20 tests: the blind/GT dataset loader and the eval runner.

The mini dataset (``fixtures/mini_dataset``) holds 10 documents, two per live
class. Its ground-truth field values are ``gt-canary-*`` strings that never
appear in the document text, so the GT leak test is meaningful.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest
from fakes.openai_server import FakeOpenAI
from helpers import assert_no_gt
from sqlalchemy import text

from mailroom_reloaded.agents.judge import (
    ClassificationFinding,
    FieldFinding,
    JudgeGrade,
    judge_grade,
)
from mailroom_reloaded.eval.dataset import (
    BlindDoc,
    DatasetIntegrityError,
    GroundTruth,
    bert_manifest_overlap,
    doc_id_for_sha,
    load_manifest_sha256,
    load_split,
    sample,
    sha256_text,
)
from mailroom_reloaded.eval.runner import EvalConfig, run_eval, select_graded
from mailroom_reloaded.ingest.bert import BertVerdict, Handoff, SortMode
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.pipeline import flow as flow_mod
from mailroom_reloaded.pipeline.state import MailroomState
from mailroom_reloaded.storage import db
from mailroom_reloaded.tools import ToolContext

FIXTURES = Path(__file__).parent / "fixtures" / "mini_dataset"

JUDGE_GRADE_JSON = json.dumps(
    {
        "fields": [
            {
                "field": "party_or_sender",
                "verdict": "gt_suspect",
                "rationale": "label looks noisy",
            }
        ],
        "classification": {"verdict": "correct", "rationale": "doc_type and subclass match"},
        "overall": 0.9,
    }
)


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def fake_openai():
    """Yield a local fake OpenAI server and stop it after the test."""
    server = FakeOpenAI()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def mock_provider(monkeypatch, fake_openai):
    """Point the mock provider at the local fake OpenAI server."""
    monkeypatch.setenv("DEFAULT_PROVIDER", "mock")
    monkeypatch.setenv("MOCK_BASE_URL", fake_openai.base_url)
    return fake_openai


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate the base directory and reset settings, ledger and SQLite state per test."""
    from mailroom_reloaded import settings
    from mailroom_reloaded.storage.ledger import reset_ledger

    # A ledger writer thread left by an earlier run_eval can call get_settings()
    # while the environment is unset and re-cache the default base_dir after our
    # cache_clear(); stop it before and after each test.
    reset_ledger()
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    settings.get_settings.cache_clear()
    monkeypatch.setattr(db, "_default_engine", None)
    try:
        yield tmp_path
    finally:
        reset_ledger()
        if db._default_engine is not None:
            db._default_engine.dispose()
        db._default_engine = None
        settings.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fast_llm(monkeypatch):
    """Disable retry delays and clear tool-support caches around each test."""
    from mailroom_reloaded.llm import retry, tooling

    monkeypatch.setattr(retry, "_sleep", lambda *_: None)
    tooling.reset_tool_support_cache()
    yield
    tooling.reset_tool_support_cache()


# --------------------------------------------------------------------------- helpers


def _load_mini():
    """Load the local miniature dataset with its ground-truth mapping."""
    return load_split(local_dir=FIXTURES)


def _sampled(per_class=2, seed=42):
    """Return a seeded per-class sample and the miniature dataset's labels."""
    docs, gts = _load_mini()
    return sample(docs, gts, per_class=per_class, seed=seed), gts


def _sorter_payload(doc_type, subclass):
    """Encode a confident sorter response for the supplied class and subclass."""
    return json.dumps(
        {
            "doc_type": doc_type,
            "doc_subclass": subclass,
            "confidence": 0.99,
            "doc_type_disagree": False,
            "doc_type_disagree_reason": None,
        }
    )


def _patch_bert_unavailable(monkeypatch):
    """Force full LLM sorting by simulating disabled BERT inference."""
    verdict = BertVerdict(available=False, reason="flag_off")
    handoff = Handoff(SortMode.FULL, None, "", "bert_unavailable:flag_off")
    monkeypatch.setattr(flow_mod, "classify_primary", lambda text, cfg=None, *, filename=None: verdict)
    monkeypatch.setattr(flow_mod, "decide_handoff", lambda v, cfg: handoff)


def _patch_coverage(monkeypatch):
    """Treat all extraction fields as covered to isolate runner behavior."""
    from mailroom_reloaded.agents import specialists

    monkeypatch.setattr(specialists, "_coverage", lambda doc_type, data: 1.0)


def _patch_judge(monkeypatch):
    """Install a deterministic judge grade with fixed findings and usage."""
    def fake_grade(text_, doc_type, data, ctx):
        """Return a fixed passing grade tied to the evaluation document ID."""
        return JudgeGrade(
            doc_id=ctx.doc_id,
            doc_type=doc_type,
            fields=[FieldFinding(field="party_or_sender", verdict="correct", rationale="ok")],
            classification=ClassificationFinding(verdict="correct", rationale="match"),
            overall=0.9,
            usage=Usage(prompt_tokens=3, completion_tokens=2, calls=1),
        )

    monkeypatch.setattr(flow_mod, "judge_grade", fake_grade)


def _route_by_role(provider, sorter_reply):
    """Answer the sorter with ``sorter_reply`` and every other role with ``{}``, in any order.

    Overlapping documents make requests in no fixed order, so replies are keyed on the
    request's system prompt (the sorter prompt carries its ``sorter_v14`` provenance tag).
    """
    def route(body):
        system = next((m["content"] for m in body["messages"] if m["role"] == "system"), "")
        content = sorter_reply if "sorter_v14" in system else "{}"
        return {"kind": "reply", "content": content, "finish_reason": "stop"}

    provider.route(route)


def _script_pipeline(provider, sampled, gts):
    """Queue sorter and extraction replies for every sampled document."""
    for doc in sampled:
        gt = gts[doc.filename]
        for _ in range(2):
            provider.reply(_sorter_payload(gt.expected, gt.expected_subclass))
        for _ in range(2):
            provider.reply("{}")


def _rows(run_id):
    """Read persisted results for a run in filename order."""
    engine = db.get_engine()
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT * FROM eval_docs WHERE run_id = :run ORDER BY filename"),
            {"run": run_id},
        )
        return [dict(row._mapping) for row in result]


def _gt_values(gts):
    """Collect distinct ground-truth strings long enough to detect leakage."""
    values: set[str] = set()
    for gt in gts.values():
        for value in gt.fields.values():
            if isinstance(value, str) and len(value) >= 4:
                values.add(value)
    return values


def _strip_gt_tool_results(requests, gt_values):
    """Exclude requests containing ground-truth canaries in tool messages."""
    kept = []
    for request in requests:
        is_gt_tool = False
        for message in request.get("messages", []) or []:
            if message.get("role") != "tool":
                continue
            content = message.get("content")
            blob = content if isinstance(content, str) else json.dumps(content, default=str)
            if any(value in blob for value in gt_values):
                is_gt_tool = True
        if not is_gt_tool:
            kept.append(request)
    return kept


# --------------------------------------------------------------------------- dataset


def test_blind_doc_has_no_label_attrs():
    """Verify blind documents are immutable and expose no label fields."""
    docs, gts = _load_mini()
    assert docs and gts
    assert {f.name for f in dataclasses.fields(BlindDoc)} == {
        "filename",
        "doc_text",
        "content_sha256",
    }
    for doc in docs:
        for forbidden in (
            "expected",
            "expected_subclass",
            "fields",
            "label",
            "cuad_clause_labels",
            "maud_clause_labels",
        ):
            assert not hasattr(doc, forbidden)
    with pytest.raises(dataclasses.FrozenInstanceError):
        docs[0].filename = "other.txt"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        docs[0].expected = "contract"  # type: ignore[misc]


def test_sha_mismatch_raises(tmp_path):
    """Verify a blind row with an incorrect content hash is rejected."""
    (tmp_path / "default.jsonl").write_text(
        json.dumps(
            {
                "filename": "a.txt",
                "doc_text": "hello world",
                "content_sha256": "0" * 64,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "ground_truth.jsonl").write_text(
        json.dumps({"filename": "a.txt", "expected": "correspondence"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(DatasetIntegrityError):
        load_split(local_dir=tmp_path)


def _real_columns(tmp_path, *, sha=None, blind_sha=None, with_gt_sha=True):
    """Write a blind/GT pair mirroring the live ``ed7576b6`` columns."""
    body = "REAL COLUMN DOC\ncontract body\n"
    actual = sha256_text(body)
    metadata = {"content_sha256": blind_sha} if blind_sha else {}
    blind = {"filename": "real.txt", "doc_text": body, "prompt": "", "metadata": metadata}
    gt = {
        "filename": "real.txt",
        "expected": "contract",
        "expected_subclass": "services",
        "gt_fields": json.dumps(
            {
                "party_or_sender": "gt-canary-real-party",
                "cuad_clause_labels": json.dumps(
                    {"Governing Law": [{"start": 0, "text": "Delaware"}]}
                ),
                "maud_clause_labels": json.dumps({"consideration_type": ["cash"]}),
                "gt_presence": json.dumps({"party_or_sender": "populated"}),
            }
        ),
        "retry_expected": False,
        "review_expected": False,
        "expected_stage": "proceed",
    }
    if with_gt_sha:
        gt["content_sha256"] = sha if sha is not None else actual
    (tmp_path / "default.jsonl").write_text(json.dumps(blind) + "\n", encoding="utf-8")
    (tmp_path / "ground_truth.jsonl").write_text(json.dumps(gt) + "\n", encoding="utf-8")
    return body, actual


def test_real_columns_join_verifies_and_parses_gt_fields(tmp_path):
    """Verify the Hub-shaped join checks hashes and decodes nested ground truth."""
    body, actual = _real_columns(tmp_path)
    docs, gts = load_split(local_dir=tmp_path)
    assert len(docs) == 1
    doc = docs[0]
    assert (doc.filename, doc.doc_text, doc.content_sha256) == ("real.txt", body, actual)
    for forbidden in ("expected", "fields", "cuad_clause_labels", "maud_clause_labels"):
        assert not hasattr(doc, forbidden)
    gt = gts["real.txt"]
    assert gt.expected == "contract" and gt.expected_subclass == "services"
    assert gt.fields["party_or_sender"] == "gt-canary-real-party"
    assert gt.fields["cuad_clause_labels"] == {
        "Governing Law": [{"start": 0, "text": "Delaware"}]
    }
    assert gt.cuad_clause_labels == ["Governing Law"]
    assert gt.maud_clause_labels == ["consideration_type"]


def test_real_columns_sha_mismatch_raises(tmp_path):
    """Verify a mismatched hash on the joined ground-truth row is rejected."""
    _real_columns(tmp_path, sha="0" * 64)
    with pytest.raises(DatasetIntegrityError):
        load_split(local_dir=tmp_path)


def test_blind_metadata_hash_is_a_fallback(tmp_path):
    """Verify blind metadata supplies the hash when ground truth omits it."""
    _, actual = _real_columns(tmp_path, with_gt_sha=False)
    with pytest.raises(DatasetIntegrityError):
        load_split(local_dir=tmp_path)
    _real_columns(tmp_path, blind_sha=actual, with_gt_sha=False)
    docs, _ = load_split(local_dir=tmp_path)
    assert docs[0].content_sha256 == actual


def test_bert_manifest_overlap(tmp_path):
    """Verify overlap detection for manifest files, directories and missing paths."""
    docs = [
        BlindDoc("a.txt", "alpha", sha256_text("alpha")),
        BlindDoc("b.txt", "beta", sha256_text("beta")),
    ]
    manifest = tmp_path / "documents.jsonl"
    manifest.write_text(
        json.dumps({"filename": "a.txt", "content_sha256": sha256_text("alpha")}) + "\n",
        encoding="utf-8",
    )
    assert load_manifest_sha256(manifest) == {sha256_text("alpha")}
    assert bert_manifest_overlap(docs, manifest) == {"a.txt": True, "b.txt": False}
    assert bert_manifest_overlap(docs, tmp_path) == {"a.txt": True, "b.txt": False}
    assert bert_manifest_overlap(docs, tmp_path / "nope.jsonl") == {
        "a.txt": False,
        "b.txt": False,
    }


def test_nested_sampling():
    """Verify a smaller per-class draw is a prefix of the larger seeded draw."""
    classes = ["contract", "correspondence"]
    docs: list[BlindDoc] = []
    gts: dict[str, GroundTruth] = {}
    for cls in classes:
        for i in range(60):
            name = f"{cls}_{i:02d}.txt"
            body = f"text for {name}"
            docs.append(BlindDoc(name, body, sha256_text(body)))
            gts[name] = GroundTruth(filename=name, expected=cls)

    n20 = sample(docs, gts, per_class=20, seed=42)
    n50 = sample(docs, gts, per_class=50, seed=42)

    assert len(n20) == 40
    assert len(n50) == 100
    assert {d.filename for d in n20} <= {d.filename for d in n50}
    for cls in classes:
        p20 = [d.filename for d in n20 if gts[d.filename].expected == cls]
        p50 = [d.filename for d in n50 if gts[d.filename].expected == cls]
        assert p20 == p50[:20]  # the n=20 draw is a prefix of the n=50 draw


# --------------------------------------------------------------------------- runner


def test_concurrency_bounded(env, monkeypatch):
    """Verify evaluation overlaps document work without exceeding its semaphore."""
    active = 0
    peak = 0

    async def fake_kickoff(self, inputs=None, input_files=None, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return MailroomState(status="archived")

    monkeypatch.setattr(flow_mod.MailroomFlow, "kickoff_async", fake_kickoff)

    run_eval(
        EvalConfig(
            local_dir=FIXTURES,
            per_class=2,
            seed=42,
            concurrency=4,
            judge_sample_rate=0.0,
        )
    )

    assert peak <= 4
    assert peak >= 2  # the semaphore really overlaps work


def test_judge_sample_rate(env, monkeypatch):
    """Verify deterministic grading selection is reflected in persisted rows."""
    docs, gts = _load_mini()
    sampled = sample(docs, gts, per_class=2, seed=42)

    graded = select_graded(sampled, 0.5, 42)
    assert len(graded) == 5
    assert graded == select_graded(sampled, 0.5, 42)
    assert len(select_graded(sampled, 1.0, 42)) == 10
    assert select_graded(sampled, 0.0, 42) == set()

    async def fake_kickoff(self, inputs=None, input_files=None, **kwargs):
        return MailroomState(status="archived")

    monkeypatch.setattr(flow_mod.MailroomFlow, "kickoff_async", fake_kickoff)
    run_id = run_eval(
        EvalConfig(
            local_dir=FIXTURES,
            per_class=2,
            seed=42,
            concurrency=4,
            judge_sample_rate=0.5,
        )
    )
    rows = _rows(run_id)
    assert len(rows) == 10
    assert sum(int(r["graded"]) for r in rows) == 5
    assert {r["filename"] for r in rows if r["graded"]} == graded


def test_eval_run_records_rows(env, mock_provider, monkeypatch):
    """Verify every evaluated document stores outputs, grades, usage and gate features."""
    _patch_bert_unavailable(monkeypatch)
    _patch_coverage(monkeypatch)
    _patch_judge(monkeypatch)
    sampled, gts = _sampled()
    first = gts[sampled[0].filename]
    _route_by_role(mock_provider, _sorter_payload(first.expected, first.expected_subclass))

    run_id = run_eval(
        EvalConfig(
            local_dir=FIXTURES,
            per_class=2,
            seed=42,
            concurrency=4,
            judge_sample_rate=1.0,
        )
    )

    rows = _rows(run_id)
    assert len(rows) == 10
    assert {r["filename"] for r in rows} == {d.filename for d in sampled}
    assert all(r["status"] == "archived" for r in rows)
    assert all(r["schema_valid"] == 1 for r in rows)
    assert all(r["graded"] == 1 for r in rows)
    assert all(r["judge_overall"] == pytest.approx(0.9) for r in rows)
    assert all(r["calls"] >= 2 for r in rows)
    assert all(json.loads(r["gate_features"])["extract"]["schema_valid"] for r in rows)


def test_no_gt_leak_in_agent_requests(env, mock_provider, monkeypatch):
    """Verify labels appear only in authorized judge tool results."""
    _patch_bert_unavailable(monkeypatch)
    _patch_coverage(monkeypatch)
    sampled, gts = _sampled()

    # Full pipeline run: sorter + specialist requests, no grading.
    _script_pipeline(mock_provider, sampled, gts)
    run_eval(
        EvalConfig(
            local_dir=FIXTURES,
            per_class=2,
            seed=42,
            concurrency=1,
            judge_sample_rate=0.0,
        )
    )

    # The real judge (called outside the eval event loop) fetches ground truth
    # only through the get_ground_truth tool; that tool result is the sole place
    # the labels may appear.
    doc = sampled[0]
    gt = gts[doc.filename]
    doc_id = doc_id_for_sha(doc.content_sha256)
    mock_provider.tool_call("get_ground_truth", {}).reply(JUDGE_GRADE_JSON)
    ctx = ToolContext(
        doc_text=doc.doc_text,
        doc_id=doc_id,
        eval_mode=True,
        ground_truth=lambda _: dataclasses.asdict(gt),
    )
    grade = judge_grade(doc.doc_text, gt.expected, {}, ctx)
    assert grade.overall == pytest.approx(0.9)

    canaries = _gt_values(gts)
    assert canaries
    filtered = _strip_gt_tool_results(mock_provider.requests, canaries)

    # The judge's get_ground_truth tool result carried the labels and was excluded.
    assert len(filtered) < len(mock_provider.requests)
    assert filtered, "expected sorter/specialist requests"
    assert_no_gt(filtered, {"fields": [g.fields for g in gts.values()]})


def test_eval_run_is_recorded_in_the_archive_ledger(env, mock_provider, monkeypatch):
    """Verify an eval run writes run_opened, one doc_closed per document and a sealed run_closed."""
    from mailroom_reloaded.storage.ledger import get_ledger

    _patch_bert_unavailable(monkeypatch)
    _patch_coverage(monkeypatch)
    _patch_judge(monkeypatch)
    sampled, gts = _sampled()
    first = gts[sampled[0].filename]
    _route_by_role(mock_provider, _sorter_payload(first.expected, first.expected_subclass))

    run_id = run_eval(
        EvalConfig(local_dir=FIXTURES, per_class=2, seed=42, concurrency=4, judge_sample_rate=1.0)
    )

    ledger = get_ledger()
    assert ledger.flush()
    entries = ledger.entries(run_id=run_id, limit=100)
    kinds = [e.kind for e in entries]
    assert kinds[0] == "run_opened" and kinds[-1] == "run_closed"
    assert kinds.count("doc_closed") == 10
    assert entries[0].payload["kind"] == "eval" and entries[0].payload["posture_label"] == "pipeline"
    closed = entries[-1].payload
    assert closed["closed_by"] == "completed" and closed["expected"] == 10 and closed["docs"] == 10
    assert all(e.payload["outcome"] == "completed" for e in entries if e.kind == "doc_closed")
    assert all(e.payload["usage_by_role"]["grader"]["calls"] >= 0 for e in entries if e.kind == "doc_closed")
    verdict = ledger.verify(run_id)
    assert verdict.ok and verdict.merkle_ok is True


def test_eval_error_rows_are_recorded_as_aborted_documents(env, monkeypatch):
    """Verify a document that fails before the flow can record it still gets a doc_closed."""
    from mailroom_reloaded.storage.ledger import get_ledger

    async def exploding_kickoff(self, inputs=None, input_files=None, **kwargs):
        raise RuntimeError("could not even configure")

    monkeypatch.setattr(flow_mod.MailroomFlow, "kickoff_async", exploding_kickoff)
    run_id = run_eval(
        EvalConfig(local_dir=FIXTURES, per_class=1, seed=42, concurrency=2, judge_sample_rate=0.0)
    )
    ledger = get_ledger()
    assert ledger.flush()
    docs = ledger.entries(run_id=run_id, kind="doc_closed", limit=100)
    assert len(docs) == 5
    assert all(e.payload["outcome"] == "aborted" and e.payload["usage_complete"] is False for e in docs)
    closed = ledger.entries(run_id=run_id, kind="run_closed")[0].payload
    assert closed["expected"] == 5 and closed["docs"] == 5


def test_eval_that_dies_closes_the_run_as_interrupted(env, monkeypatch):
    """Verify an exception escaping the eval still seals the run."""
    from mailroom_reloaded.eval import runner
    from mailroom_reloaded.storage.ledger import get_ledger

    async def broken_run_all(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "_run_all", broken_run_all)
    with pytest.raises(KeyboardInterrupt):
        run_eval(EvalConfig(local_dir=FIXTURES, per_class=1, seed=42, judge_sample_rate=0.0))
    ledger = get_ledger()
    assert ledger.flush()
    closed = ledger.entries(kind="run_closed")
    assert [e.payload["closed_by"] for e in closed] == ["interrupted"]


def test_specialist_cell_documents_are_recorded(env, monkeypatch):
    """Verify cell-mode rows (which never touch the flow) are recorded with the specialist's spend."""
    from mailroom_reloaded.eval import runner
    from mailroom_reloaded.llm.usage import Usage
    from mailroom_reloaded.storage.ledger import get_ledger

    def fake_extract(text, doc_type, doc_subclass, **kw):
        return runner.ExtractResult(doc_type, {"a": 1}, True, None, 0.9, None, 1, Usage(7, 3, 0.0, 1))

    monkeypatch.setattr(runner, "_extract", fake_extract)
    run_id = run_eval(
        EvalConfig(local_dir=FIXTURES, per_class=1, seed=42, mode="specialist_cell", judge_sample_rate=0.0)
    )
    ledger = get_ledger()
    assert ledger.flush()
    docs = ledger.entries(run_id=run_id, kind="doc_closed", limit=100)
    assert len(docs) == 5 and all(e.payload["outcome"] == "completed" for e in docs)
    roles = {r for e in docs for r in e.payload["usage_by_role"]}
    assert roles and all(r.endswith("_specialist") for r in roles)
    assert ledger.verify(run_id).ok


# --------------------------------------------------------------------------- overlap check


def _get_run_metadata(run_id):
    """Fetch the overlap check metadata from the eval_runs table."""
    engine = db.get_engine()
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT overlap_check FROM eval_runs WHERE run_id = :run"),
            {"run": run_id},
        )
        row = result.fetchone()
        if row is None:
            return None
        metadata_json = row[0]
        if metadata_json is None:
            return None
        return json.loads(metadata_json)


def test_overlap_check_fires_with_overlapping_manifest(env, monkeypatch, tmp_path):
    """Verify the overlap check detects and records overlapping documents."""
    _patch_bert_unavailable(monkeypatch)
    _patch_coverage(monkeypatch)
    _patch_judge(monkeypatch)

    async def fake_kickoff(self, inputs=None, input_files=None, **kwargs):
        return MailroomState(status="archived")

    monkeypatch.setattr(flow_mod.MailroomFlow, "kickoff_async", fake_kickoff)
    docs, gts = _load_mini()
    sampled = sample(docs, gts, per_class=1, seed=42)

    # Create a manifest that includes the first document
    manifest_file = tmp_path / "bert_documents.jsonl"
    first_doc = sampled[0]
    manifest_file.write_text(
        json.dumps(
            {
                "filename": first_doc.filename,
                "content_sha256": first_doc.content_sha256,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    run_id = run_eval(
        EvalConfig(
            local_dir=FIXTURES,
            bert_manifest=manifest_file,
            per_class=1,
            seed=42,
            concurrency=2,
            judge_sample_rate=0.0,
        )
    )

    # Check that overlap was recorded
    metadata = _get_run_metadata(run_id)
    assert metadata is not None
    assert metadata["status"] == "completed"
    assert metadata["overlapping_count"] > 0
    assert len(metadata.get("overlapping_samples", [])) > 0
    assert first_doc.filename in {s["filename"] for s in metadata["overlapping_samples"]}


def test_overlap_check_no_overlap_with_clean_manifest(env, monkeypatch, tmp_path):
    """Verify the overlap check reports no overlaps when manifest is clean."""
    _patch_bert_unavailable(monkeypatch)
    _patch_coverage(monkeypatch)
    _patch_judge(monkeypatch)

    async def fake_kickoff(self, inputs=None, input_files=None, **kwargs):
        return MailroomState(status="archived")

    monkeypatch.setattr(flow_mod.MailroomFlow, "kickoff_async", fake_kickoff)

    # Create a manifest with documents not in our sample
    manifest_file = tmp_path / "bert_documents.jsonl"
    manifest_file.write_text(
        json.dumps(
            {
                "filename": "nonexistent_doc.txt",
                "content_sha256": sha256_text("completely different content"),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    run_id = run_eval(
        EvalConfig(
            local_dir=FIXTURES,
            bert_manifest=manifest_file,
            per_class=1,
            seed=42,
            concurrency=2,
            judge_sample_rate=0.0,
        )
    )

    # Check that no overlaps were recorded
    metadata = _get_run_metadata(run_id)
    assert metadata is not None
    assert metadata["status"] == "completed"
    assert metadata["overlapping_count"] == 0
    assert len(metadata.get("overlapping_samples", [])) == 0


def test_overlap_check_skipped_when_manifest_absent(env, monkeypatch):
    """Verify the overlap check is skipped when the manifest does not exist."""
    _patch_bert_unavailable(monkeypatch)
    _patch_coverage(monkeypatch)
    _patch_judge(monkeypatch)

    async def fake_kickoff(self, inputs=None, input_files=None, **kwargs):
        return MailroomState(status="archived")

    monkeypatch.setattr(flow_mod.MailroomFlow, "kickoff_async", fake_kickoff)

    # No explicit manifest and nothing at <base_dir>/models/bert_manifest.jsonl.
    run_id = run_eval(
        EvalConfig(
            local_dir=FIXTURES,
            per_class=1,
            seed=42,
            concurrency=2,
            judge_sample_rate=0.0,
        )
    )

    # Check that the check was skipped
    metadata = _get_run_metadata(run_id)
    assert metadata is not None
    assert metadata["status"] == "skipped"
    assert metadata["reason"] == "manifest_not_found"


def test_overlap_check_uses_base_dir_manifest_fallback(env, monkeypatch):
    """Without ``bert_manifest``, ``<base_dir>/models/bert_manifest.jsonl`` is used."""
    _patch_bert_unavailable(monkeypatch)
    _patch_coverage(monkeypatch)
    _patch_judge(monkeypatch)

    async def fake_kickoff(self, inputs=None, input_files=None, **kwargs):
        return MailroomState(status="archived")

    monkeypatch.setattr(flow_mod.MailroomFlow, "kickoff_async", fake_kickoff)
    docs, gts = _load_mini()
    sampled = sample(docs, gts, per_class=1, seed=42)
    manifest_file = env / "models" / "bert_manifest.jsonl"
    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    manifest_file.write_text(
        json.dumps({"content_sha256": sampled[0].content_sha256}) + "\n", encoding="utf-8"
    )

    run_id = run_eval(
        EvalConfig(local_dir=FIXTURES, per_class=1, seed=42, judge_sample_rate=0.0)
    )

    metadata = _get_run_metadata(run_id)
    assert metadata["status"] == "completed", metadata
    assert metadata["overlapping_count"] == 1


@pytest.mark.parametrize(
    ("content", "expected"),
    [("{not json\n", "skipped"), ("", "completed")],
    ids=["malformed_manifest_records_check_error", "empty_manifest_completes"],
)
def test_overlap_check_manifest_read_outcomes(env, monkeypatch, tmp_path, content, expected):
    """A malformed manifest is a check error; a readable empty one is a clean check."""
    _patch_bert_unavailable(monkeypatch)
    _patch_coverage(monkeypatch)
    _patch_judge(monkeypatch)

    async def fake_kickoff(self, inputs=None, input_files=None, **kwargs):
        return MailroomState(status="archived")

    monkeypatch.setattr(flow_mod.MailroomFlow, "kickoff_async", fake_kickoff)
    manifest_file = tmp_path / "bert_documents.jsonl"
    manifest_file.write_text(content, encoding="utf-8")

    run_id = run_eval(
        EvalConfig(
            local_dir=FIXTURES,
            bert_manifest=manifest_file,
            per_class=1,
            seed=42,
            judge_sample_rate=0.0,
        )
    )

    metadata = _get_run_metadata(run_id)
    assert metadata["status"] == expected
    if expected == "skipped":
        assert metadata["reason"] == "check_error"
    else:
        assert metadata["overlapping_count"] == 0


def test_load_manifest_sha256_strict_raises_but_default_skips(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n", encoding="utf-8")
    assert load_manifest_sha256(bad) == set()
    with pytest.raises(ValueError):
        load_manifest_sha256(bad, strict=True)
