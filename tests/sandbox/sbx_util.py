"""Helpers shared by the sandbox server tests."""

from __future__ import annotations

API = "/api/sandbox/v1"


def messages_of(client, scenario: str) -> list[dict]:
    """Fetch public message views for one scenario through the sandbox API."""
    return client.get(f"{API}/messages", params={"scenario": scenario}).json()[
        "messages"
    ]
