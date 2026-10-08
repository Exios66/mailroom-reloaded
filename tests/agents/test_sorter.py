import json
import math

import pytest

from mailroom_reloaded.agents.sorter import sort
from mailroom_reloaded.ingest.bert import Handoff, SortMode
from mailroom_reloaded.prompts.loader import load_prompt
from mailroom_reloaded.scoring import subclass_vocab
from mailroom_reloaded.settings import load_taxonomy


def _handoff(
    mode=SortMode.SUBCLASS_ONLY,
    doc_type="correspondence",
    prior="",
    reason="fast_path",
):
    locked = doc_type if mode is SortMode.SUBCLASS_ONLY else None
    return Handoff(mode, locked, prior, reason)


def _script(provider, payload):
    """Queue the tool-round draft reply then the final structured reply.

    The sorter offers tools, so ``call_structured`` makes a tool round first
    (its reply is discarded when it carries no tool_calls) and then the final
    structured turn.
    """
    provider.reply("thinking").reply(json.dumps(payload))


def test_subclass_only_prompt_scoped(mock_provider):
    _script(
        mock_provider,
        {
            "doc_subclass": "email",
            "confidence": 0.8,
            "doc_type_disagree": False,
            "doc_type_disagree_reason": None,
        },
    )
    sort("a memo about the deal", _handoff())

    req = mock_provider.requests[-1]
    system = req["messages"][0]["content"]
    base = load_prompt("sorter_v14")
    assert system.startswith(base)
    for key in subclass_vocab("correspondence"):
        assert key in system
    enum = req["response_format"]["json_schema"]["schema"]["properties"]["doc_subclass"]["enum"]
    assert enum == subclass_vocab("correspondence")


def test_disagree_flag_propagates(mock_provider):
    _script(
        mock_provider,
        {
            "doc_subclass": "email",
            "confidence": 0.7,
            "doc_type_disagree": True,
            "doc_type_disagree_reason": "looks like a contract",
        },
    )
    result = sort("doc", _handoff())
    assert result.doc_type_disagree is True
    assert "contract" in result.disagree_reason
    assert result.doc_type == "correspondence"


def test_confidence_from_logprobs(mock_provider):
    final_json = (
        '{"doc_subclass": "email", "confidence": 0.99, '
        '"doc_type_disagree": false, "doc_type_disagree_reason": null}'
    )
    tokens = [
        ("LABEL", -0.9),
        (":", -0.8),
        (" correspondence", math.log(0.9)),
        ("/email", math.log(0.5)),
        ("\n", -0.7),
        (final_json, -0.5),
    ]
    content = "".join(token for token, _ in tokens)
    mock_provider.reply("thinking").reply(content).with_logprobs(tokens)

    result = sort("doc", _handoff())
    assert result.confidence_source == "logprob"
    assert result.raw_confidence == pytest.approx(0.9 * 0.5)
    assert result.doc_subclass == "email"


def test_calibration_applied(tmp_path, monkeypatch, mock_provider):
    payload = {
        "doc_subclass": "email",
        "confidence": 0.9,
        "doc_type_disagree": False,
        "doc_type_disagree_reason": None,
    }

    _script(mock_provider, payload)
    unc = sort("doc", _handoff())
    assert unc.calibrated is False
    assert unc.raw_confidence == pytest.approx(0.9)
    assert unc.confidence == pytest.approx(unc.raw_confidence)

    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    from mailroom_reloaded import settings

    settings.get_settings.cache_clear()
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "calibration.json").write_text(
        json.dumps({"mock": {"qwen/qwen3.7-flash": {"correspondence": 2.0}}})
    )
    _script(mock_provider, payload)
    cal = sort("doc", _handoff())
    expected = 1.0 / (1.0 + math.exp(-(math.log(0.9 / 0.1) / 2.0)))
    assert cal.calibrated is True
    assert cal.raw_confidence == pytest.approx(0.9)
    assert cal.confidence == pytest.approx(expected)


def test_self_report_fallback(mock_provider):
    _script(
        mock_provider,
        {
            "doc_subclass": "memo",
            "confidence": 0.77,
            "doc_type_disagree": False,
            "doc_type_disagree_reason": None,
        },
    )
    result = sort("doc", _handoff())
    assert result.confidence_source == "self_report"
    assert result.raw_confidence == pytest.approx(0.77)
    assert result.confidence == pytest.approx(0.77)


def test_sorter_uses_list_subclasses_tool(mock_provider):
    mock_provider.tool_call("list_subclasses", {"doc_type": "correspondence"})
    mock_provider.reply("working")
    mock_provider.reply(
        json.dumps(
            {
                "doc_subclass": "email",
                "confidence": 0.8,
                "doc_type_disagree": False,
                "doc_type_disagree_reason": None,
            }
        )
    )
    result = sort("doc", _handoff())
    assert result.doc_subclass == "email"
    final = mock_provider.requests[-1]["messages"]
    assert any(message.get("role") == "tool" for message in final)


def test_input_truncated_to_sorter_cap(mock_provider):
    cap = load_taxonomy().agent("sorter").max_input_chars
    text = "A" * cap + "B" * 50
    _script(
        mock_provider,
        {
            "doc_type": "contract",
            "doc_subclass": "license",
            "confidence": 0.9,
            "doc_type_disagree": False,
            "doc_type_disagree_reason": None,
        },
    )
    result = sort(
        text,
        _handoff(
            SortMode.FULL,
            doc_type=None,
            prior="BERT predicts class contract",
            reason="defer_class",
        ),
    )
    assert result.doc_type == "contract"
    user = mock_provider.requests[-1]["messages"][1]["content"]
    assert "A" * cap in user
    assert "A" * (cap + 1) not in user
    assert "B" * 50 not in user
