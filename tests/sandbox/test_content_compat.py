"""Compatibility metadata and inclusive code-version windows."""

from importlib import metadata

import pytest

from mailroom_reloaded.sandbox.content import compat


@pytest.mark.parametrize("schema", ["2", "2.99", "2.1.4"])
def test_schema_minor_and_patch_versions_are_additive(schema):
    """Accept schema versions that share the supported major version."""
    compat.check_compat({"schema_version": schema}, code="0.2.0")


@pytest.mark.parametrize("meta,code,message", [
    ({}, "0.2.0", "no schema_version"),
    ({"schema_version": None}, "0.2.0", "no schema_version"),
    ({"schema_version": "1.99"}, "0.2.0", "schema major 1"),
    ({"schema_version": "2.x"}, "0.2.0", "bad version string"),
    ({"schema_version": "2.0"}, "latest", "bad version string"),
    ({"schema_version": "2.0", "min_code_version": "invalid"}, "0.2.0", "bad version"),
    ({"schema_version": "2.0", "max_code_version": "invalid"}, "0.2.0", "bad version"),
    ({"schema_version": "2.0", "min_code_version": "0.10.0"}, "0.9.9", "older than"),
    ({"schema_version": "2.0", "max_code_version": "0.9.9"}, "0.10.0", "newer than"),
])
def test_incompatible_or_malformed_metadata_is_rejected(meta, code, message):
    """Reject missing, malformed, or incompatible schema and code version metadata."""
    with pytest.raises(compat.CompatError, match=message):
        compat.check_compat(meta, code=code)


@pytest.mark.parametrize("window,code", [
    ({"min_code_version": "0.2.0"}, "0.2.0"),
    ({"min_code_version": "0.2.0"}, "1.0.0"),
    ({"max_code_version": "0.3.0"}, "0.3.0"),
    ({"max_code_version": "0.3.0"}, "0.1.0"),
    ({"min_code_version": "0.2.0", "max_code_version": "0.2.0"}, "0.2.0"),
])
def test_one_sided_and_single_version_windows(window, code):
    """Accept inclusive bounds for open-ended and exact code version windows."""
    compat.check_compat({"schema_version": "2.0", **window}, code=code)


def test_caller_can_select_supported_major():
    """Honor a caller-supplied schema major version."""
    compat.check_compat({"schema_version": "3.1"}, code="0.2.0", supported_major=3)


def test_compat_uses_installed_code_version(monkeypatch):
    """Use the installed code version when no explicit version is supplied."""
    monkeypatch.setattr(compat, "code_version", lambda: "0.4.0")
    with pytest.raises(compat.CompatError, match="newer than"):
        compat.check_compat({"schema_version": "2.0", "max_code_version": "0.3.0"})


def test_code_version_reads_package_metadata(monkeypatch):
    """Read the installed distribution version from package metadata."""
    def version(name):
        """Return a synthetic version after checking the requested distribution name."""
        assert name == "mailroom-reloaded"
        return "0.7.0"

    monkeypatch.setattr(metadata, "version", version)
    assert compat.code_version() == "0.7.0"


def test_source_checkout_has_a_code_version(monkeypatch):
    """Fall back to the source version when distribution metadata is unavailable."""
    def missing(name):
        """Simulate missing distribution metadata for the requested package."""
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", missing)
    assert compat.code_version() == "0.2.0"
