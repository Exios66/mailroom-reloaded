"""Subtype / doc-subclass normalization and equivalence helpers.

Ported from ``agents/sorter_agent.py`` (llm-entity-extraction) so scoring no
longer depends on importing the agent class. All configuration comes from
:mod:`llm_dojo_scoring.config`.
"""

from __future__ import annotations

import re

from .config import (
    SUBTYPE_UNKNOWN,
    get_settings,
)

_ALIAS_KEY_RE = re.compile(r"[^a-z0-9]")


def normalize_subtype(value, settings=None) -> str:
    """Coerce a raw sorter subtype output (or a CUAD folder name) to a
    canonical subtype key; unknown/non-contract values become ``other``."""
    s = settings or get_settings()
    if value is None:
        return SUBTYPE_UNKNOWN
    key = _ALIAS_KEY_RE.sub("", str(value).strip().lower())
    if not key:
        return SUBTYPE_UNKNOWN
    keys = s.contract_subtype_keys
    if key in keys:
        return key
    aliases = {_ALIAS_KEY_RE.sub("", k): v for k, v in s.subtype_aliases.items()}
    if key in aliases:
        return aliases[key]
    # "License Agreement" -> "license"; "Non-Compete" -> non_compete_no_solicit.
    for subtype in s.contract_subtypes:
        norm_label = _ALIAS_KEY_RE.sub("", subtype["label"].lower())
        if key == norm_label or key.startswith(norm_label[:8]):
            return subtype["key"]
    return SUBTYPE_UNKNOWN


def equivalent_subtypes(a: str, b: str, settings=None) -> bool:
    """True when two subtype keys are the same family or members of the same
    interchangeable family class (see ``subtype_equivalences``)."""
    return (settings or get_settings()).equivalent_subtypes(a, b)


def equivalent_doc_subclasses(a: str | None, b: str | None,
                              allowed: set[str] | None = None, settings=None) -> bool:
    """True when two doc_subclass keys are the same family or members of the
    same equivalence class, scoped to the doc_type's own dimension when an
    ``allowed`` key set is provided."""
    return (settings or get_settings()).equivalent_doc_subclasses(a, b, allowed)


def normalize_doc_subclass(value, allowed: set[str] | None = None) -> str:
    """Coerce a raw sorter subclass output to a canonical doc_subclass key.

    ``allowed`` scopes the enum (consideration types for merger_agreement,
    record types for corporate_record); unknown values and subclasses from
    the wrong dimension become ``other``."""
    if value is None:
        return SUBTYPE_UNKNOWN
    raw = str(value).strip()
    key = _ALIAS_KEY_RE.sub("", raw.lower())
    if not key:
        return SUBTYPE_UNKNOWN
    if allowed is not None:
        if raw in allowed:
            return raw
        for candidate in allowed:
            if key == _ALIAS_KEY_RE.sub("", candidate.lower()):
                return candidate
        return SUBTYPE_UNKNOWN
    return raw
