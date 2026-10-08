"""Exercise the shell boundary with literal dotenv values and synthetic secrets."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def helper(tmp_path):
    (tmp_path / "scripts").mkdir()
    for name in ("dev.sh", "dev_env.py"):
        shutil.copy(ROOT / "scripts" / name, tmp_path / "scripts" / name)
    (tmp_path / "bin").mkdir()
    for name, body in {
        "docker": 'printf "%s\\n" "$@" >> "$TEST_ROOT/docker-args"\n',
        "id": 'echo 1234\n',
        "curl": '''python3 - "$@" <<'INNER'
import json, os, sys
from pathlib import Path
with (Path(os.environ['TEST_ROOT']) / 'curl-args').open('a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
print('200')
INNER
''',
    }.items():
        executable = tmp_path / "bin" / name
        executable.write_text("#!/usr/bin/env bash\n" + body)
        executable.chmod(0o755)
    env = {"PATH": f"{tmp_path}/bin:{os.environ['PATH']}", "TEST_ROOT": str(tmp_path)}

    def run(command, dotenv=None, extra_env=None):
        if dotenv is not None:
            (tmp_path / ".env").write_text(dotenv)
        return subprocess.run(
            ["bash", str(tmp_path / "scripts/dev.sh"), command],
            env={**env, **(extra_env or {})}, capture_output=True, text=True, check=False,
        )

    return tmp_path, run


def test_dotenv_is_data_and_preserves_spaces_dollars(helper):
    root, run = helper
    result = run("status", """MAILROOM_API_URL='http://example.test/a b/$literal'
PHOENIX_URL=http://example.test/has spaces/$literal # trailing comment
PROMETHEUS_URL="http://example.test/$(touch should-not-exist)"
GRAFANA_URL='http://example.test/`touch should-not-exist`'
PATH=/nonexistent
IGNORED=$(touch should-not-exist)
""")
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in (root / 'curl-args').read_text().splitlines()]
    assert [call[-1] for call in calls] == [
        'http://example.test/a b/$literal/health',
        'http://example.test/has spaces/$literal/',
        'http://example.test/$(touch should-not-exist)/-/healthy',
        'http://example.test/`touch should-not-exist`/api/health',
    ]
    assert not (root / 'should-not-exist').exists()


@pytest.mark.parametrize("source", ["dotenv", "environment"])
def test_grafana_password_redacted_and_env_file_retained(helper, source):
    root, run = helper
    secret = 'synthetic secret $literal'
    result = run(
        'up', f"GRAFANA_ADMIN_PASSWORD='{secret}'\n" if source == 'dotenv' else '',
        {'GRAFANA_ADMIN_PASSWORD': secret} if source == 'environment' else {},
    )
    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout + result.stderr
    assert 'GRAFANA_ADMIN_PASSWORD' not in result.stdout + result.stderr
    assert '(admin / <configured>)' in result.stdout
    assert ['--env-file', str(root / '.env')] == (root / 'docker-args').read_text().splitlines()[5:7]


def test_grafana_default_password_indication(helper):
    _, run = helper
    result = run('up')
    assert result.returncode == 0
    assert '(admin / admin (default))' in result.stdout


def test_dotenv_parser_tokens_multiline_and_environment_precedence(tmp_path):
    path = tmp_path / '.env'
    path.write_text("""export MAILROOM_API_TOKEN='a token $literal $(not-run)'
SMOKE_TIMEOUT=42
MAILROOM_API_URL='http://file.test'
PHOENIX_URL="a\\tb $literal" # comment
GRAFANA_URL='line one
line two'
""")
    result = subprocess.run(
        ['python3', str(ROOT / 'scripts/dev_env.py'), str(path)],
        env={**os.environ, 'MAILROOM_API_URL': 'http://environment.test'},
        capture_output=True, check=True,
    )
    fields = result.stdout.decode().split('\0')[:-1]
    settings = dict(zip(fields[::2], fields[1::2]))
    assert settings['MAILROOM_API_TOKEN'] == 'a token $literal $(not-run)'
    assert settings['SMOKE_TIMEOUT'] == '42'
    assert 'MAILROOM_API_URL' not in settings
    assert settings['PHOENIX_URL'] == 'a\tb $literal'
    assert settings['GRAFANA_URL'] == 'line one\nline two'
