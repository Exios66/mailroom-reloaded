"""OAuth token files must be owner-only."""

from __future__ import annotations

import os
import stat

from mailroom_reloaded.intake.gmail import _write_private


def test_write_private_creates_0600_and_tightens_existing(tmp_path):
    p = tmp_path / "sub" / "gmail_token.json"
    _write_private(p, "{}")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    os.chmod(p, 0o644)
    _write_private(p, '{"a":1}')
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert p.read_text() == '{"a":1}'
