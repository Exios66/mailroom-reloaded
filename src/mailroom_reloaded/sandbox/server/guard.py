"""Network guard: prove the sandbox never leaves the machine.

Patches ``socket`` connect/resolve entry points and ``smtplib`` so any attempt to
reach a non-loopback host raises :class:`NetworkBlocked` and is recorded. The
only sockets the sandbox opens are loopback ones (its own listener and the
in-process mock LLM); those are recorded in ``connects`` so tests can assert it.
"""

from __future__ import annotations

import ipaddress
import smtplib
import socket
import threading
from typing import Any, Self

__all__ = ["NetworkBlocked", "NetworkGuard", "is_loopback_host"]


class NetworkBlocked(OSError):
    """Raised when sandbox code tries to reach a non-loopback host."""


def is_loopback_host(host: Any) -> bool:
    """True for loopback IP literals and ``localhost``; False for anything else."""
    if host is None or host == "":
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip("[]").lower()
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class NetworkGuard:
    """Install/uninstall socket and smtplib patches; usable as a context manager."""

    def __init__(self) -> None:
        self.blocked: list[tuple[str, str]] = []
        self.connects: list[tuple[str, int]] = []
        self.installed = False
        self._lock = threading.Lock()
        self._orig: dict[str, Any] = {}

    # -- recording
    def _check(self, kind: str, host: Any, port: Any = None) -> None:
        if is_loopback_host(host):
            if kind == "connect":
                with self._lock:
                    self.connects.append((str(host), int(port or 0)))
            return
        with self._lock:
            self.blocked.append((kind, f"{host}:{port}"))
        raise NetworkBlocked(f"sandbox network guard: refused {kind} to {host!r}")

    def _addr_host(self, address: Any) -> tuple[Any, Any]:
        if isinstance(address, tuple) and address:
            return address[0], address[1] if len(address) > 1 else None
        return address, None  # AF_UNIX path (str/bytes): local, allowed

    # -- install
    def install(self) -> Self:
        if self.installed:
            return self
        guard = self
        orig_connect = socket.socket.connect
        orig_connect_ex = socket.socket.connect_ex
        orig_getaddrinfo = socket.getaddrinfo
        orig_smtp_connect = smtplib.SMTP.connect
        self._orig = {
            "connect": orig_connect,
            "connect_ex": orig_connect_ex,
            "getaddrinfo": orig_getaddrinfo,
            "smtp_connect": orig_smtp_connect,
        }

        def connect(self_sock, address):  # type: ignore[no-untyped-def]
            if isinstance(address, tuple):
                guard._check("connect", *guard._addr_host(address))
            return orig_connect(self_sock, address)

        def connect_ex(self_sock, address):  # type: ignore[no-untyped-def]
            if isinstance(address, tuple):
                guard._check("connect", *guard._addr_host(address))
            return orig_connect_ex(self_sock, address)

        def getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
            guard._check("resolve", host)
            return orig_getaddrinfo(host, *args, **kwargs)

        def smtp_connect(self_smtp, host="localhost", port=0, source_address=None):  # type: ignore[no-untyped-def]
            with guard._lock:
                guard.blocked.append(("smtp", f"{host}:{port}"))
            raise NetworkBlocked(
                "sandbox network guard: SMTP is disabled; mail is captured, never sent"
            )

        socket.socket.connect = connect  # type: ignore[method-assign]
        socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
        socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
        smtplib.SMTP.connect = smtp_connect  # type: ignore[method-assign]
        self.installed = True
        return self

    def uninstall(self) -> None:
        if not self.installed:
            return
        socket.socket.connect = self._orig["connect"]  # type: ignore[method-assign]
        socket.socket.connect_ex = self._orig["connect_ex"]  # type: ignore[method-assign]
        socket.getaddrinfo = self._orig["getaddrinfo"]  # type: ignore[assignment]
        smtplib.SMTP.connect = self._orig["smtp_connect"]  # type: ignore[method-assign]
        self.installed = False

    def __enter__(self) -> Self:
        return self.install()

    def __exit__(self, *exc: object) -> None:
        self.uninstall()

    def summary(self) -> dict:
        with self._lock:
            return {
                "installed": self.installed,
                "blocked_attempts": len(self.blocked),
                "blocked": [{"kind": k, "target": t} for k, t in self.blocked[-20:]],
                "loopback_connects": len(self.connects),
            }
