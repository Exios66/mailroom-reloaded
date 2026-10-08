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
    server = FakeOpenAI()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def mock_provider(monkeypatch, fake_openai):
    monkeypatch.setenv("DEFAULT_PROVIDER", "mock")
    monkeypatch.setenv("MOCK_BASE_URL", fake_openai.base_url)
    return fake_openai


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    from mailroom_reloaded import settings

    settings.get_settings.cache_clear()
    monkeypatch.setattr(db, "_default_engine", None)
    try:
        yield tmp_path
    finally:
        if db._default_engine is not None:
            db._default_engine.dispose()
        db._default_engine = None
        settings.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fast_llm(monkeypatch):
    from mailroom_reloaded.llm import retry, tooling

    monkeypatch.setattr(retry, "_sleep", lambda *_: None)
    tooling.reset_tool_support_cache()
    yield
    tooling.reset_tool_support_cache()


# --------------------------------------------------------------------------- helpers


def _load_mini():
    return load_split(local_dir=FIXTURES)


def _sampled(per_class=2, seed=42):
    docs, gts = _load_mini()
    return sample(docs, gts, per_class=per_class, seed=seed), gts


def _sorter_payload(doc_type, subclass):
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
    verdict = BertVerdict(available=False, reason="flag_off")
    handoff = Handoff(SortMode.FULL, None, "", "bert_unavailable:flag_off")
    monkeypatch.setattr(flow_mod, "classify_primary", lambda text, cfg=None: verdict)
    monkeypatch.setattr(flow_mod, "decide_handoff", lambda v, cfg: handoff)


def _patch_coverage(monkeypatch):
    from mailroom_reloaded.agents import specialists

    monkeypatch.setattr(specialists, "_coverage", lambda doc_type, data: 1.0)


def _patch_judge(monkeypatch):
    def fake_grade(text_, doc_type, data, ctx):
        return JudgeGrade(
            doc_id=ctx.doc_id,
            doc_type=doc_type,
            fields=[FieldFinding(field="party_or_sender", verdict="correct", rationale="ok")],
            classification=ClassificationFinding(verdict="correct", rationale="match"),
            overall=0.9,
            usage=Usage(prompt_tokens=3, completion_tokens=2, calls=1),
        )

    monkeypatch.setattr(flow_mod, "judge_grade", fake_grade)


def _script_pipeline(provider, sampled, gts):
    for doc in sampled:
        gt = gts[doc.filename]
        for _ in range(2):
            provider.reply(_sorter_payload(gt.expected, gt.expected_subclass))
        for _ in range(2):
            provider.reply("{}")


def _rows(run_id):
    engine = db.get_engine()
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT * FROM eval_docs WHERE run_id = :run ORDER BY filename"),
            {"run": run_id},
        )
        return [dict(row._mapping) for row in result]


def _gt_values(gts):
    values: set[str] = set()
    for gt in gts.values():
        for value in gt.fields.values():
            if isinstance(value, str) and len(value) >= 4:
                values.add(value)
    return values


def _strip_gt_tool_results(requests, gt_values):
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
    _real_columns(tmp_path, sha="0" * 64)
    with pytest.raises(DatasetIntegrityError):
        load_split(local_dir=tmp_path)


def test_blind_metadata_hash_is_a_fallback(tmp_path):
    _, actual = _real_columns(tmp_path, with_gt_sha=False)
    with pytest.raises(DatasetIntegrityError):
        load_split(local_dir=tmp_path)
    _real_columns(tmp_path, blind_sha=actual, with_gt_sha=False)
    docs, _ = load_split(local_dir=tmp_path)
    assert docs[0].content_sha256 == actual


def test_bert_manifest_overlap(tmp_path):
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
    _patch_bert_unavailable(monkeypatch)
    _patch_coverage(monkeypatch)
    _patch_judge(monkeypatch)
    sampled, gts = _sampled()
    _script_pipeline(mock_provider, sampled, gts)

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
