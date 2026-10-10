"""Acceptance checks for RUNBOOK.md's copyable examples and local references.

Follow the static deployment tests: no Docker, network, provider, or running
server is needed. Shell blocks are parsed with bash -n, never executed.
"""

import os
import re
import shlex
import subprocess
import textwrap
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs" / "RUNBOOK.md"
TEXT = RUNBOOK.read_text(encoding="utf-8")
SHELL_BLOCKS = [
    textwrap.dedent(block)
    for block in re.findall(
        r"^[ \t]*```bash\n(.*?)^[ \t]*```", TEXT, re.MULTILINE | re.DOTALL
    )
]
COMMANDS = [
    shlex.split(line, comments=True)
    for block in SHELL_BLOCKS
    for line in block.splitlines()
    if line.strip() and not line.lstrip().startswith("#")
]


@pytest.mark.parametrize("block", SHELL_BLOCKS, ids=lambda block: block.splitlines()[0])
def test_runbook_shell_examples_have_valid_syntax(block):
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-n"],
        input=block, capture_output=True, text=True, timeout=5, check=False,
        env={**os.environ, "BASH_ENV": os.devnull},
    )
    assert result.returncode == 0, result.stderr


def test_runbook_local_links_resolve_relative_to_document():
    links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", TEXT)
    local_links = [urlsplit(link) for link in links if not urlsplit(link).scheme]
    assert local_links, "No local documentation links found"
    for link in local_links:
        target = RUNBOOK.parent / link.path if link.path else RUNBOOK
        assert target.is_file(), f"Broken runbook link: {link.geturl()}"
        if link.fragment:
            # GitHub drops punctuation and replaces spaces in heading anchors.
            headings = re.findall(r"^#{1,6} (.+)$", target.read_text(), re.MULTILINE)
            anchors = {
                re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
                for heading in headings
            }
            assert link.fragment in anchors, f"Missing heading: {link.geturl()}"


def test_runbook_script_references_can_be_invoked_directly():
    scripts = set(re.findall(r"\bscripts/[\w/-]+\.sh\b", TEXT))
    assert scripts, "No script references found"
    for script in scripts:
        path = ROOT / script
        assert path.is_file(), f"Missing script: {script}"
        assert os.access(path, os.X_OK), f"Documented command is not executable: {script}"


def test_runbook_copy_sources_exist_in_fresh_checkout():
    # Include the inline inbox smoke check as well as fenced setup commands.
    copies = [command for command in COMMANDS if command[0] == "cp"]
    copies += [shlex.split(command) for command in re.findall(r"`(cp [^`]+)`", TEXT)]
    assert copies, "No setup or smoke-check copy commands found"
    for command in copies:
        source = ROOT / command[1]
        assert source.is_file(), f"Missing copy source: {command[1]}"
        assert source.stat().st_size > 0, f"Empty copy source: {command[1]}"


def test_runbook_compose_profiles_exist_in_documented_config():
    commands = [command for command in COMMANDS if command[:2] == ["docker", "compose"]]
    assert commands, "No Compose startup command found"
    profiles = set(re.findall(r"`--profile ([\w-]+)`", TEXT))
    assert profiles, "No optional Compose profiles documented"
    for command in commands:
        config = ROOT / command[command.index("-f") + 1]
        services = yaml.safe_load(config.read_text())["services"]
        available = {profile for svc in services.values() for profile in svc.get("profiles", [])}
        assert profiles <= available, f"Unknown profiles in {config.name}: {profiles - available}"


def test_runbook_install_uses_declared_dev_extra():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    commands = [command for command in COMMANDS if command[:2] == ["uv", "sync"]]
    assert commands, "No dependency installation command found"
    for command in commands:
        # Regression: --all-extras pulls optional ML dependencies into the demo.
        assert "--all-extras" not in command
        extras = [command[i + 1] for i, arg in enumerate(command) if arg == "--extra"]
        assert "dev" in extras, "The verification commands need pytest and ruff"
        assert set(extras) <= project["optional-dependencies"].keys()


def test_runbook_node_test_glob_matches_existing_tests():
    commands = [command for command in COMMANDS if command[:2] == ["node", "--test"]]
    assert commands, "No TUI test command found"
    for command in commands:
        for pattern in command[2:]:
            matches = list(ROOT.glob(pattern))
            assert matches, f"TUI test glob matches no files: {pattern}"
            assert all(path.is_file() for path in matches)


def test_runbook_protected_curl_examples_keep_bearer_header_together():
    commands = [command for command in COMMANDS if command[0] == "curl"]
    assert commands, "No API check examples found"
    protected = [command for command in commands if urlsplit(command[-1]).path.startswith("/v1/")]
    assert protected, "No authenticated API examples found"
    for command in protected:
        # shlex preserves a correctly quoted header as one argument; dropping
        # quotes would send only 'Authorization:' and turn Bearer into a URL.
        headers = [command[i + 1] for i, arg in enumerate(command) if arg in {"-H", "--header"}]
        assert "Authorization: Bearer $MAILROOM_API_TOKEN" in headers
