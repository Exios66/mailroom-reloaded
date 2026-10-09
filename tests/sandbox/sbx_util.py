"""Helpers shared by the sandbox server tests."""

from __future__ import annotations

API = "/api/sandbox/v1"


def messages_of(client, scenario: str) -> list[dict]:
    return client.get(f"{API}/messages", params={"scenario": scenario}).json()[
        "messages"
    ]
