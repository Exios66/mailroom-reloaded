"""Load packaged prompts; frozen sets are sha256-locked to their lineage.json."""

from __future__ import annotations

import hashlib
import json
from functools import cache
from importlib import resources
from typing import Literal

PromptSet = Literal["frozen_v1", "sand37"]

SPECIALISTS: tuple[str, ...] = (
    "contracts_specialist",
    "merger_agreement_specialist",
    "corporate_records_specialist",
    "correspondence_specialist",
    "insurance_claims_specialist",
)

_EXTS = (".txt", ".md")


class PromptLockError(Exception):
    """A locked prompt's bytes do not match the sha256 in its lineage file."""


def _root():
    return resources.files("mailroom_reloaded.prompts")


def _read_bytes(name: str, prompt_set: str) -> bytes:
    """Read raw prompt bytes: prompt_set dir first, then the shared prompts dir."""
    for base in (_root() / prompt_set, _root()):
        for ext in _EXTS:
            f = base / f"{name}{ext}"
            if f.is_file():
                return f.read_bytes()
    raise KeyError(name)


@cache
def _lineage(prompt_set: str) -> dict[str, str]:
    f = _root() / prompt_set / "lineage.json"
    if not f.is_file():
        return {}
    data = json.loads(f.read_bytes())
    return {k: v["sha256"] for k, v in data.get("specialists", {}).items()}


def prompt_sha256(name: str, prompt_set: PromptSet = "frozen_v1") -> str:
    return hashlib.sha256(_read_bytes(name, prompt_set)).hexdigest()


@cache
def load_prompt(name: str, *, prompt_set: PromptSet = "frozen_v1") -> str:
    raw = _read_bytes(name, prompt_set)
    expected = _lineage(prompt_set).get(name)
    if expected is not None:
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected:
            raise PromptLockError(
                f"{prompt_set}/{name}: sha256 {actual} != locked {expected}"
            )
    return raw.decode("utf-8")
