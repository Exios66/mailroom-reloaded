"""FastAPI ``/v1`` API and the localhost runs UI (spec sections 3 and 9)."""

from mailroom_reloaded.api.app import api, app, create_app

__all__ = ["api", "app", "create_app"]
