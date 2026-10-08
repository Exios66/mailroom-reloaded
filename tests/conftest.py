import pytest

from mailroom_reloaded import settings as settings_mod


@pytest.fixture(autouse=True)
def _clear_caches():
    settings_mod.get_settings.cache_clear()
    settings_mod.load_taxonomy.cache_clear()
    yield
    settings_mod.get_settings.cache_clear()
    settings_mod.load_taxonomy.cache_clear()
