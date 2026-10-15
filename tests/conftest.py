import os

import pytest

from mailroom_reloaded import settings as settings_mod

# CLI help assertions read plain text; a FORCE_COLOR in the caller's shell makes rich
# split option names with ANSI codes, so tests never inherit it.
os.environ.pop("FORCE_COLOR", None)
# Pager provider health stays off in tests unless a test opts in (tests/llm/test_health.py).
os.environ["MAILROOM_PROVIDER_HEALTH"] = "0"


@pytest.fixture(autouse=True)
def _clear_caches():
    settings_mod.get_settings.cache_clear()
    settings_mod.load_taxonomy.cache_clear()
    yield
    settings_mod.get_settings.cache_clear()
    settings_mod.load_taxonomy.cache_clear()
