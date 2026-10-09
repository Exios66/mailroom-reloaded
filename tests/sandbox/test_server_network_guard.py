"""The sandbox never opens a non-loopback socket and never speaks SMTP."""

from __future__ import annotations

import smtplib
import socket

import pytest
from sbx_util import API

from mailroom_reloaded.sandbox.server.guard import (
    NetworkBlocked,
    NetworkGuard,
    is_loopback_host,
)


def test_guard_blocks_remote_connect_resolve_and_smtp():
    """Verify remote sockets, DNS, and SMTP are blocked and patches are restored."""
    original = socket.socket.connect
    with NetworkGuard() as g:
        with pytest.raises(NetworkBlocked):
            socket.create_connection(("example.com", 80), timeout=1)
        with pytest.raises(NetworkBlocked):
            socket.socket().connect(("93.184.216.34", 80))
        with pytest.raises(NetworkBlocked):
            socket.getaddrinfo("mail.example.org", 25)
        with pytest.raises(NetworkBlocked):
            smtplib.SMTP("smtp.example.com", 25)
        kinds = {k for k, _ in g.blocked}
        assert {"connect", "resolve", "smtp"} <= kinds
    # uninstalled: the patches are gone
    assert socket.socket.connect is original


def test_guard_allows_loopback_only():
    """Verify a real loopback connection succeeds and its destination is recorded."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    with NetworkGuard() as g:
        c = socket.create_connection(("127.0.0.1", port), timeout=2)
        c.close()
        assert g.connects == [("127.0.0.1", port)] and g.blocked == []
    srv.close()
    assert (
        is_loopback_host("::1")
        and is_loopback_host("localhost")
        and not is_loopback_host("10.0.0.5")
    )


def test_full_flow_opened_only_loopback_sockets(live):
    """Acceptance proof: A, B, mock LLM and egress capture ran with the guard on."""
    client, svc, _ = live
    summary = client.get(f"{API}/status").json()["network_guard"]
    assert summary["installed"] is True and summary["blocked_attempts"] == 0
    assert svc.guard.connects, (
        "the in-process mock LLM should have been reached over loopback"
    )
    assert {h for h, _ in svc.guard.connects} <= {"127.0.0.1", "localhost", "::1"}
    mock_port = int(svc.pipeline.mock.base_url.rsplit(":", 1)[1].split("/")[0])
    assert {p for _, p in svc.guard.connects} == {mock_port}
    assert client.get(f"{API}/outbox").json()["summary"]["transmitted"] == 0


@pytest.mark.parametrize("resolver", ["gethostbyname", "gethostbyname_ex"])
def test_legacy_resolvers_check_before_delegating_and_restore(monkeypatch, resolver):
    """Verify legacy DNS wrappers reject remote hosts before calling the resolver."""
    calls = []
    result = (
        "127.0.0.1" if resolver == "gethostbyname" else ("localhost", [], ["127.0.0.1"])
    )

    def original(host):
        """Record calls to the stub resolver and return its configured loopback result."""
        calls.append(host)
        return result

    monkeypatch.setattr(socket, resolver, original)
    with NetworkGuard() as guard:
        resolve = getattr(socket, resolver)
        with pytest.raises(NetworkBlocked):
            resolve("mail.example.org")
        assert calls == []
        assert guard.blocked == [("resolve", "mail.example.org:None")]
        assert resolve("localhost") == result
        assert calls == ["localhost"]
    assert getattr(socket, resolver) is original
