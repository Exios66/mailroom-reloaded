"""Compatibility policy: schema MAJOR must match; code version inside the window."""

from __future__ import annotations

SUPPORTED_SCHEMA_MAJOR = 2


class CompatError(Exception):
    """Content is not compatible with this code."""


def code_version() -> str:
    """Return the installed package version, or '0.2.0' if metadata lookup fails."""
    try:
        from importlib.metadata import version

        return version("mailroom-reloaded")
    except Exception:  # noqa: BLE001 - not installed (source checkout)
        return "0.2.0"


def parse_version(v: str) -> tuple[int, ...]:
    """Parse dot-separated integers without padding missing version components.

    Raise CompatError if any component cannot be converted to an integer.
    """
    try:
        return tuple(int(p) for p in str(v).split("."))
    except ValueError as exc:
        raise CompatError(f"bad version string {v!r}") from exc


def schema_major(schema_version: str) -> int:
    """Return the first version component, raising CompatError for invalid components."""
    return parse_version(schema_version)[0]


def check_compat(
    meta: dict,
    *,
    code: str | None = None,
    supported_major: int = SUPPORTED_SCHEMA_MAJOR,
) -> None:
    """Raise CompatError unless ``meta`` (content.json) fits this code.

    The schema major must equal ``supported_major``. Truthy min/max_code_version
    bounds are inclusive and compared as integer tuples without zero-padding.
    An omitted or empty ``code`` uses code_version(). Missing schema_version and
    invalid version components also raise CompatError.
    """
    sv = meta.get("schema_version")
    if sv is None:
        raise CompatError("content metadata has no schema_version")
    if schema_major(sv) != supported_major:
        raise CompatError(
            f"schema major {schema_major(sv)} != supported {supported_major}"
        )
    cur = parse_version(code or code_version())
    lo, hi = meta.get("min_code_version"), meta.get("max_code_version")
    if lo and cur < parse_version(lo):
        raise CompatError(
            f"code {'.'.join(map(str, cur))} older than min_code_version {lo}"
        )
    if hi and cur > parse_version(hi):
        raise CompatError(
            f"code {'.'.join(map(str, cur))} newer than max_code_version {hi}"
        )
