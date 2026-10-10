"""Docker smoke tests: image builds, container health, and compose configs."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"


@pytest.fixture
def docker_available():
    """Check if Docker daemon is available; skip test if not."""
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")
    res = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        pytest.skip("Docker daemon unreachable")


class TestDockerSmoke:
    """Docker smoke test runner with proper skip handling for exit code 2."""

    @pytest.mark.docker
    def test_docker_smoke_runs(self, docker_available):
        """Run scripts/docker_smoke.sh and treat exit 2 as skip."""
        script = ROOT / "scripts" / "docker_smoke.sh"
        assert script.exists(), f"Script not found: {script}"

        res = subprocess.run(
            [str(script)],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )

        # Exit code 2 means prerequisite missing (registry unreachable, etc.)
        if res.returncode == 2:
            pytest.skip(f"Docker smoke prerequisites not met: {res.stderr}")

        # Exit code 0 is success; 1 is failure
        assert res.returncode == 0, (
            f"Docker smoke test failed with exit {res.returncode}\n"
            f"stdout: {res.stdout}\n"
            f"stderr: {res.stderr}"
        )
        assert "DOCKER SMOKE OK" in res.stdout, (
            f"Expected 'DOCKER SMOKE OK' in output, got:\n{res.stdout}"
        )


class TestDockerfileStaticChecks:
    """Always-on static checks for Dockerfiles (no daemon needed)."""

    def _get_dockerfile_content(self, dockerfile_path: str | Path) -> str:
        """Read and return Dockerfile content."""
        return Path(dockerfile_path).read_text()

    def _check_copy_schemas_exists(self, content: str) -> bool:
        """Check if 'COPY schemas' line exists in Dockerfile."""
        return bool(re.search(r"COPY\s+schemas\s+", content))

    def _check_no_editable_in_final_uv_sync(self, content: str) -> bool:
        """Check that final uv sync has --no-editable flag."""
        # Find all uv sync lines with --no-install-project
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if "uv sync" in line and "--no-install-project" not in line:
                # This is a final install line; check for --no-editable
                if "--no-editable" in line:
                    return True
        # For single-stage or final stage, check the last uv sync
        for line in reversed(lines):
            if "uv sync" in line and "--no-install-project" not in line:
                if "--no-editable" in line:
                    return True
        return False

    def test_dockerfile_has_copy_schemas(self):
        """Test that deploy/Dockerfile includes COPY schemas line."""
        content = self._get_dockerfile_content(DEPLOY / "Dockerfile")
        assert self._check_copy_schemas_exists(
            content
        ), "Dockerfile missing 'COPY schemas' line"

    def test_dockerfile_final_uv_sync_no_editable(self):
        """Test that deploy/Dockerfile final uv sync has --no-editable."""
        content = self._get_dockerfile_content(DEPLOY / "Dockerfile")
        # Check the final uv sync call (line 33) has --no-editable
        assert "--no-editable" in content.split("RUN uv sync")[
            -1
        ], "Dockerfile final uv sync missing --no-editable flag"

    def test_dockerfile_sandbox_has_copy_schemas(self):
        """Test that deploy/Dockerfile.sandbox includes COPY schemas line."""
        content = self._get_dockerfile_content(DEPLOY / "Dockerfile.sandbox")
        assert self._check_copy_schemas_exists(
            content
        ), "Dockerfile.sandbox missing 'COPY schemas' line"

    def test_dockerfile_copy_schemas_checker(self):
        """Test the copy schemas checker itself by verifying it fails on bad input."""
        bad_dockerfile = "FROM python:3.11\nRUN uv sync\n"
        assert not self._check_copy_schemas_exists(
            bad_dockerfile
        ), "Checker should reject Dockerfile without COPY schemas"

        good_dockerfile = "FROM python:3.11\nCOPY schemas ./schemas\nRUN uv sync\n"
        assert self._check_copy_schemas_exists(
            good_dockerfile
        ), "Checker should accept Dockerfile with COPY schemas"

    def test_dockerfile_no_editable_checker(self):
        """Test the --no-editable checker itself."""
        # Dockerfile without --no-editable should fail
        bad_content = (
            "FROM python:3.11\n"
            "RUN uv sync --frozen --no-install-project\n"
            "RUN uv sync --frozen\n"
        )
        assert not self._check_no_editable_in_final_uv_sync(
            bad_content
        ), "Checker should reject final uv sync without --no-editable"

        # With --no-editable should pass
        good_content = (
            "FROM python:3.11\n"
            "RUN uv sync --frozen --no-install-project\n"
            "RUN uv sync --frozen --no-editable\n"
        )
        assert self._check_no_editable_in_final_uv_sync(
            good_content
        ), "Checker should accept final uv sync with --no-editable"


class TestComposeStaticChecks:
    """Always-on static checks for docker-compose files (YAML parsing)."""

    @pytest.mark.parametrize(
        "compose_file",
        [
            DEPLOY / "docker-compose.yml",
            DEPLOY / "docker-compose.dev.yml",
            DEPLOY / "docker-compose.sandbox.yml",
        ],
        ids=lambda f: f.name,
    )
    def test_compose_file_parses_yaml(self, compose_file):
        """Test that all docker-compose files are valid YAML."""
        content = compose_file.read_text()
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as e:
            pytest.fail(f"{compose_file.name} is not valid YAML: {e}")

    def test_compose_files_exist(self):
        """Test that all expected compose files exist."""
        for cf in [
            DEPLOY / "docker-compose.yml",
            DEPLOY / "docker-compose.dev.yml",
            DEPLOY / "docker-compose.sandbox.yml",
        ]:
            assert cf.exists(), f"Expected compose file not found: {cf}"
