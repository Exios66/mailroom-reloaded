import hashlib
import json
from importlib import resources

import pytest

from mailroom_reloaded.prompts import loader
from mailroom_reloaded.prompts.loader import (
    SPECIALISTS,
    PromptLockError,
    load_prompt,
    prompt_sha256,
)


@pytest.mark.parametrize("prompt_set", ["frozen_v1", "sand37"])
def test_every_frozen_prompt_matches_lineage(prompt_set):
    lineage = json.loads(
        (resources.files("mailroom_reloaded.prompts") / prompt_set / "lineage.json").read_bytes()
    )["specialists"]
    assert set(lineage) == set(SPECIALISTS) and len(SPECIALISTS) == 5
    for name in SPECIALISTS:
        text = load_prompt(name, prompt_set=prompt_set)
        assert hashlib.sha256(text.encode()).hexdigest() == lineage[name]["sha256"]
        assert prompt_sha256(name, prompt_set) == lineage[name]["sha256"]


def test_tampered_prompt_raises(monkeypatch):
    load_prompt.cache_clear()
    monkeypatch.setattr(loader, "_read_bytes", lambda n, s: b"tampered")
    with pytest.raises(PromptLockError):
        load_prompt("contracts_specialist")
    load_prompt.cache_clear()


def test_unknown_prompt_keyerror():
    with pytest.raises(KeyError):
        load_prompt("nope")


def test_unlocked_prompts_load_and_format():
    for n in ["sorter_v14", "sorter_subclass_scope", "judge_completeness", "judge_grade",
              "arbiter", "boss", "vision", "merger_agreement_specialist_maud_v1"]:
        assert load_prompt(n).strip()
    out = load_prompt("sorter_subclass_scope").format(doc_type="contract", subclasses="a, b")
    assert "a, b" in out
    out = load_prompt("judge_grade").format(doc_type="contract")
    shape = out.split("this shape:")[1].split("\n")[1]
    assert shape.startswith('{"fields"')
    assert "get_ground_truth" in out and "gt_suspect" in out
