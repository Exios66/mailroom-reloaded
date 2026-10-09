"""Shared fixtures for the sandbox server tests (offline, no network)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sbx_util import API

from mailroom_reloaded.sandbox.server.app import create_sandbox_app
from mailroom_reloaded.sandbox.server.content import load_sandbox_content
from mailroom_reloaded.sandbox.server.guard import NetworkGuard
from mailroom_reloaded.sandbox.server.service import SandboxService


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """One started sandbox (guard on, own data dir, mock LLM) with the smoke set injected."""
    guard = NetworkGuard().install()
    data = tmp_path_factory.mktemp("sbx") / "state"
    svc = SandboxService(load_sandbox_content(), data, guard=guard)
    app = create_sandbox_app(svc)
    with TestClient(app) as client:
        r = client.post(f"{API}/inject", json={"scenario_ids": "all", "wait": True})
        assert r.status_code == 200, r.text
        yield client, svc, r.json()
    guard.uninstall()


@pytest.fixture
def idle_service(tmp_path):
    """A service that is not started (no pipeline activation): ingress-only tests."""
    return SandboxService(load_sandbox_content(), tmp_path / "state")
