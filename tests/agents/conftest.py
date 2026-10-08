import pytest
from fakes.openai_server import fake_openai  # noqa: F401  (re-exported fixture)


@pytest.fixture(autouse=True)
def _fast_llm(monkeypatch):
    """No real sleeping in retry; forget per-endpoint tool-support memory."""
    from mailroom_reloaded.llm import retry, tooling

    sleeps: list[float] = []
    monkeypatch.setattr(retry, "_sleep", sleeps.append)
    tooling.reset_tool_support_cache()
    yield sleeps
    tooling.reset_tool_support_cache()


@pytest.fixture
def mock_provider(monkeypatch, fake_openai):  # noqa: F811
    monkeypatch.setenv("DEFAULT_PROVIDER", "mock")
    monkeypatch.setenv("MOCK_BASE_URL", fake_openai.base_url)
    return fake_openai
