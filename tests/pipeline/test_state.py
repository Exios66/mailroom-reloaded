"""State checkpoint round trips must preserve typed routing and usage data."""

from mailroom_reloaded.agents.arbiter import ArbiterDecision
from mailroom_reloaded.agents.boss import BossDecision
from mailroom_reloaded.agents.judge import JudgeVerdict
from mailroom_reloaded.agents.sorter import SortResult
from mailroom_reloaded.agents.specialists import ExtractResult
from mailroom_reloaded.ingest.bert import BertVerdict, Handoff, SortMode
from mailroom_reloaded.ingest.clerk import IngestResult
from mailroom_reloaded.llm.usage import Usage
from mailroom_reloaded.pipeline.state import MailroomState


def test_checkpoint_restores_typed_results_and_retry_counters():
    """Verify JSON checkpoints restore typed results without sharing mutable lists."""
    state = MailroomState(
        doc_id="doc-1",
        path="letter.txt",
        text="letter",
        status="processing",
        ingest=IngestResult("letter", "text", 1),
        bert=BertVerdict(available=False, reason="flag_off"),
        handoff=Handoff(SortMode.FULL, None, "prior", "retry"),
        sort=SortResult(
            "correspondence",
            "email",
            0.95,
            0.98,
            True,
            SortMode.FULL,
            "self_report",
            False,
            None,
            Usage(calls=1),
        ),
        extract=ExtractResult(
            "correspondence",
            {"sender": "Alice"},
            True,
            None,
            0.9,
            None,
            1,
            Usage(calls=1),
        ),
        verdict=JudgeVerdict(label="partial", score=0.8),
        arbiter=ArbiterDecision(action="re_extract", caveats=["missing date"]),
        boss=BossDecision(action="accept"),
        classify_attempts=2,
        extract_attempts=1,
        resorted=True,
        route_trail=["sort", "extract", "verify"],
        usage_total=Usage(prompt_tokens=40, completion_tokens=10, calls=2),
    )
    restored = MailroomState.model_validate_json(state.model_dump_json())
    assert restored == state
    assert isinstance(restored.sort, SortResult)
    assert isinstance(restored.extract, ExtractResult)
    assert restored.handoff.mode is SortMode.FULL
    assert restored.usage_total.total_tokens == 50
    restored.route_trail.append("extract")
    restored.arbiter.caveats.append("missing recipient")
    assert state.route_trail == ["sort", "extract", "verify"]
    assert state.arbiter.caveats == ["missing date"]


def test_documents_do_not_share_mutable_defaults():
    """Verify route and usage mutations remain isolated to one document state."""
    first, second = MailroomState(), MailroomState()
    first.route_trail.append("ingest")
    first.usage_total += Usage(prompt_tokens=5, calls=1)
    assert second.route_trail == []
    assert second.usage_total == Usage()
