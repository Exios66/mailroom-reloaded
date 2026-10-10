"""Offline dataset validation and sampling boundary cases."""

import json
import sys

import pytest

from mailroom_reloaded.eval.dataset import (
    BlindDoc,
    DatasetIntegrityError,
    GroundTruth,
    _ground_truth_from_row,
    load_split,
    sample,
    sha256_text,
)
from mailroom_reloaded.eval.runner import select_graded


@pytest.mark.parametrize(
    "row,reason",
    [
        (
            {"doc_text": "text", "content_sha256": sha256_text("text")},
            "missing a filename",
        ),
        ({"filename": "a.txt", "doc_text": "text"}, "missing content_sha256"),
    ],
)
def test_local_loader_rejects_missing_identity(tmp_path, row, reason):
    """Verify blind rows require both a filename and a declared content hash."""
    (tmp_path / "default.jsonl").write_text(json.dumps(row) + "\n")
    (tmp_path / "ground_truth.jsonl").write_text("")
    with pytest.raises(DatasetIntegrityError, match=reason):
        load_split(local_dir=tmp_path)


def test_loader_aliases_unicode_hashes_and_ground_truth_json(tmp_path):
    """Verify alias normalization and Unicode hashing keep labels out of blind data."""
    body = "caf\u00e9 \u6587\u66f8"
    blind = {
        "name": " letter.txt ",
        "body": body,
        "sha256": sha256_text(body),
        "expected": "must not enter blind document",
    }
    truth = {
        "file": "letter.txt",
        "label": "correspondence",
        "subclass": "email",
        "extraction": '{"sender": "Alice"}',
        "cuad_clauses": '["clause", 7]',
        "maud_clauses": "single label",
        "retry_expected": False,
    }
    for filename, row in [("default.jsonl", blind), ("ground_truth.jsonl", truth)]:
        (tmp_path / filename).write_text(
            "\n" + json.dumps(row) + "\n\n", encoding="utf-8"
        )
    docs, gts = load_split(local_dir=tmp_path)
    assert docs == [BlindDoc("letter.txt", body, sha256_text(body))]
    assert gts["letter.txt"] == GroundTruth(
        filename="letter.txt",
        expected="correspondence",
        expected_subclass="email",
        fields={"sender": "Alice"},
        cuad_clause_labels=["clause", "7"],
        maud_clause_labels=["single label"],
        retry_expected=False,
    )


def test_ground_truth_parses_hub_string_flags():
    """The Hub serves the escalation flags as ``"true"``/``"false"`` strings."""
    gt = _ground_truth_from_row(
        {
            "filename": "a.txt",
            "retry_expected": "true",
            "review_expected": "false",
            "expected_stage": "archived",
        }
    )
    assert gt.retry_expected is True
    assert gt.review_expected is False


def test_ground_truth_derives_flags_from_richer_columns():
    """Richer columns win over the degenerate all-false booleans (issue #14)."""
    review = _ground_truth_from_row(
        {
            "filename": "r.txt",
            "retry_expected": "false",
            "review_expected": "false",
            "expected_stage": "review",
            "review_reason": "low_confidence",
        }
    )
    assert review.review_expected is True

    retry = _ground_truth_from_row(
        {
            "filename": "t.txt",
            "retry_expected": "false",
            "review_expected": "false",
            "expected_stage": "archived",
            "expected_post_retry_state": "human_review",
        }
    )
    assert retry.retry_expected is True


@pytest.mark.parametrize("stage_fields", [
    {"expected_stage": "archived"}, {}, {"expected_stage": None}, {"expected_stage": ""},
])
@pytest.mark.parametrize("flag_fields, expected", [
    ({"review_expected": "false"}, False), ({}, None), ({"review_expected": "true"}, True),
])
def test_ground_truth_review_reason_alone_is_not_a_review(stage_fields, flag_fields, expected):
    """A reason alone preserves false, unknown, and true explicit review labels."""
    gt = _ground_truth_from_row(
        {
            "filename": "a.txt",
            **stage_fields,
            **flag_fields,
            "review_reason": "ambiguous",
        }
    )
    assert gt.review_expected is expected


def test_load_split_labeled_config_computes_missing_hash(monkeypatch):
    """A self-contained labeled config (fixtures) needs no declared sha256."""
    from types import SimpleNamespace

    from mailroom_reloaded.eval import dataset as ds

    rows = [
        {
            "filename": "fx.txt",
            "doc_text": "hello",
            "expected": "contract",
            "retry_expected": "true",
            "expected_stage": "archived",
            "expected_post_retry_state": "human_review",
        }
    ]
    fake = SimpleNamespace(load_dataset=lambda *a, **k: rows)
    monkeypatch.setitem(sys.modules, "datasets", fake)

    docs, gts = ds.load_split(
        revision="rev", split="train", repo="org/repo", config="fixtures"
    )
    assert docs[0].content_sha256 == sha256_text("hello")
    assert gts["fx.txt"].retry_expected is True


@pytest.mark.parametrize("config", ["fixtures", "bundles"])
@pytest.mark.parametrize("duplicate_name", ["fx.txt", " fx.txt "])
def test_labeled_loader_rejects_duplicate_filenames(monkeypatch, config, duplicate_name):
    """Reject repeated normalized filenames before validating labeled content."""
    from types import SimpleNamespace

    rows = [
        {"filename": "fx.txt", "doc_text": "hello", "expected": "contract"},
        {
            "filename": duplicate_name,
            "doc_text": "different",
            "expected": "correspondence",
            # Duplicate identity must be rejected before validating the document.
            "content_sha256": sha256_text("hello"),
        },
    ]
    monkeypatch.setitem(
        sys.modules, "datasets", SimpleNamespace(load_dataset=lambda *a, **k: rows)
    )

    with pytest.raises(DatasetIntegrityError, match=r"Duplicate ground truth filename: fx\.txt"):
        load_split(config=config)


@pytest.fixture
def documents():
    """Build known, custom and unlabelled documents for sampling boundary tests."""
    docs = [BlindDoc(f"{i}.txt", str(i), sha256_text(str(i))) for i in range(8)]
    gts = {
        doc.filename: GroundTruth(doc.filename, expected=label)
        for doc, label in zip(
            docs, ["contract"] * 3 + ["correspondence"] * 3 + ["custom"]
        )
    }
    return docs, gts


@pytest.mark.parametrize("limit", [0, -1, -100])
def test_nonpositive_sample_size_selects_nothing(documents, limit):
    """Verify zero and negative per-class limits yield no documents."""
    docs, gts = documents
    assert sample(docs, gts, per_class=limit) == []


def test_sampling_filters_classes_and_is_independent_of_input_order(documents):
    """Verify class filters and seeded ordering without mutating the input list."""
    docs, gts = documents
    original = list(docs)
    selected = sample(docs, gts, per_class=2, classes=["contract"], seed=17)
    assert len(selected) == 2
    assert all(gts[doc.filename].expected == "contract" for doc in selected)
    assert selected == sample(
        list(reversed(docs)), gts, per_class=2, classes=["contract"], seed=17
    )
    assert docs == original
    assert sample(docs, gts, per_class=10, classes=[]) == []
    assert sample(docs, gts, per_class=10, classes=["absent"]) == []


def test_sampling_keeps_unknown_and_unlabelled_documents_without_duplicates(documents):
    """Verify unrestricted sampling retains every document exactly once."""
    docs, gts = documents
    selected = sample(docs, gts, per_class=100)
    assert len(selected) == len(docs)
    assert {doc.filename for doc in selected} == {doc.filename for doc in docs}
    assert [doc.filename for doc in selected[-2:]] == ["7.txt", "6.txt"]


@pytest.mark.parametrize(
    "rate,count", [(-1, 0), (0, 0), (0.125, 1), (0.5, 4), (1, 8), (2, 8)]
)
def test_grading_selection_bounds_and_order_independence(documents, rate, count):
    """Verify grading rates clamp at the bounds and selection ignores input order."""
    docs, _ = documents
    selected = select_graded(docs, rate, 7)
    assert len(selected) == count
    assert selected <= {doc.filename for doc in docs}
    assert selected == select_graded(list(reversed(docs)), rate, 7)
    assert select_graded([], rate, 7) == set()


@pytest.mark.parametrize("as_dict", [False, True])
def test_hf_fallback_filters_actual_rows(as_dict):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from mailroom_reloaded.eval.dataset import _load_hf_config

    rows = [{"split": "train"}, {"split": "test", "filename": "a"}, {}]
    data = {"train": rows, "other": [{"split": "test", "filename": "b"}]} if as_dict else rows
    loader = Mock(side_effect=[ValueError("missing split"), data])
    result = _load_hf_config(SimpleNamespace(load_dataset=loader), "default", "rev", "test")
    assert result == [rows[1]] + ([data["other"][0]] if as_dict else [])


def test_hf_fallback_preserves_original_error_without_matches():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from mailroom_reloaded.eval.dataset import _load_hf_config

    original = ValueError("missing split")
    loader = Mock(side_effect=[original, {"train": [{"split": "train"}, {}]}])
    with pytest.raises(ValueError) as exc:
        _load_hf_config(SimpleNamespace(load_dataset=loader), "default", "rev", "test")
    assert exc.value is original


@pytest.mark.parametrize("blind_names,truth_names", [
    (["a.txt"], []),
    ([], ["a.txt"]),
    (["a.txt"], ["b.txt"]),
])
def test_join_rejects_unmatched_filenames(blind_names, truth_names):
    from mailroom_reloaded.eval.dataset import _join

    blind = [
        {"filename": name, "doc_text": "text", "content_sha256": sha256_text("text")}
        for name in blind_names
    ]
    truth = [{"filename": name, "expected": "contract"} for name in truth_names]
    with pytest.raises(DatasetIntegrityError, match="Filename mismatch"):
        _join(blind, truth)


@pytest.mark.parametrize('metadata_json', [False, True])
@pytest.mark.parametrize('valid', [False, True])
def test_labeled_metadata_hash_is_verified(monkeypatch, metadata_json, valid):
    """Verify labeled configs enforce hashes from mapping or JSON metadata."""
    from types import SimpleNamespace

    metadata = {'content_sha256': sha256_text('hello' if valid else 'different')}
    row = {'filename': 'fx.txt', 'doc_text': 'hello',
           'metadata': json.dumps(metadata) if metadata_json else metadata}
    monkeypatch.setitem(sys.modules, 'datasets', SimpleNamespace(load_dataset=lambda *a, **k: [row]))
    if valid:
        docs, _ = load_split(config='fixtures')
        assert docs[0].content_sha256 == sha256_text('hello')
    else:
        with pytest.raises(DatasetIntegrityError, match='content_sha256 mismatch'):
            load_split(config='fixtures')


@pytest.mark.parametrize('declared', [sha256_text('hello'), sha256_text('wrong')])
def test_labeled_top_level_hash_takes_precedence(monkeypatch, declared):
    """Prefer a top-level declared hash over conflicting metadata."""
    from types import SimpleNamespace

    row = {'filename': 'fx.txt', 'doc_text': 'hello', 'content_sha256': declared,
           'metadata': {'content_sha256': sha256_text('different')}}
    monkeypatch.setitem(sys.modules, 'datasets', SimpleNamespace(load_dataset=lambda *a, **k: [row]))
    if declared == sha256_text('hello'):
        assert load_split(config='fixtures')[0][0].content_sha256 == declared
    else:
        with pytest.raises(DatasetIntegrityError, match='content_sha256 mismatch'):
            load_split(config='fixtures')
