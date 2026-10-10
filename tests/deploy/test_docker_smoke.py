"""Docker build guards (issue #84).

The static checks run everywhere and catch the two defects PR #80 found by
reading code (a Dockerfile that installs the project without ``COPY schemas``,
and an editable install that does not survive the multi-stage copy). The
``docker`` test runs ``scripts/docker_smoke.sh`` and is deselected by default.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
SCRIPT = ROOT / "scripts" / "docker_smoke.sh"

#: Dockerfiles that install the project itself (pyproject force-includes schemas/).
PROJECT_DOCKERFILES = ["Dockerfile", "Dockerfile.dev", "Dockerfile.sandbox"]


def copies_schemas(text: str) -> bool:
    return re.search(r"^COPY\s+schemas\s", text, re.MULTILINE) is not None


def final_sync_is_non_editable(text: str) -> bool:
    """The last project-installing ``uv sync`` carries ``--no-editable``."""
    syncs = [
        line
        for line in text.splitlines()
        if "uv sync" in line and "--no-install-project" not in line
    ]
    return bool(syncs) and "--no-editable" in syncs[-1]


@pytest.mark.parametrize("name", PROJECT_DOCKERFILES)
def test_project_dockerfiles_copy_schemas(name):
    assert copies_schemas((DEPLOY / name).read_text()), f"{name} lacks COPY schemas"


def test_runtime_dockerfile_installs_non_editable():
    assert final_sync_is_non_editable((DEPLOY / "Dockerfile").read_text())


def test_checkers_reject_the_pr80_defects():
    good = (
        "COPY pyproject.toml uv.lock ./\n"
        "RUN uv sync --frozen --no-dev --no-install-project\n"
        "COPY src ./src\n"
        "COPY schemas ./schemas\n"
        "RUN uv sync --frozen --no-dev --no-editable\n"
    )
    assert copies_schemas(good) and final_sync_is_non_editable(good)
    assert not copies_schemas(good.replace("COPY schemas ./schemas\n", ""))
    assert not final_sync_is_non_editable(good.replace(" --no-editable", ""))


def _daemon_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


@pytest.mark.docker
def test_docker_smoke_script():
    if not _daemon_available():
        pytest.skip("Docker daemon unreachable")
    res = subprocess.run(
        ["bash", str(SCRIPT)], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if res.returncode == 2:
        pytest.skip(f"docker_smoke.sh prerequisite missing: {res.stderr.strip()[-300:]}")
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-3000:]
    assert "DOCKER SMOKE OK" in res.stdout
