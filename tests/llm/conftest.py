import pytest
from fakes.openai_server import fake_openai  # noqa: F401  (re-exported fixture)


def pytest_configure(config):
    config.addinivalue_line("markers", "live: hits a real configured provider (needs MAILROOM_LIVE=1)")


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


@pytest.fixture
def vllm_provider(monkeypatch, fake_openai):  # noqa: F811
    monkeypatch.setenv("DEFAULT_PROVIDER", "vllm")
    monkeypatch.setenv("VLLM_BASE_URL", fake_openai.base_url)
    return fake_openai
