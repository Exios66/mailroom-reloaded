"""stations.js must stay byte-for-byte equal to ``obs.attrs.STATIONS``."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from mailroom_reloaded.obs.attrs import STATIONS

JS = (
    Path(__file__).resolve().parents[2]
    / "src/mailroom_reloaded/api/tui/replay/stations.js"
)
FIELDS = ("id", "label", "phase", "kind", "color_token", "order")


def _python_stations() -> list[dict[str, object]]:
    return [
        {
            "id": s.id,
            "label": s.label,
            "phase": s.phase,
            "kind": s.kind,
            "color_token": s.color_token,
            "order": i,
        }
        for i, s in enumerate(STATIONS)
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_stations_equal_python() -> None:
    script = (
        f"import({json.dumps(JS.as_uri())}).then(m => "
        "console.log(JSON.stringify({s: m.STATIONS, ids: m.STATION_IDS})))"
    )
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True, timeout=30
    )
    data = json.loads(out.stdout)
    got = [{k: row[k] for k in FIELDS} for row in data["s"]]
    assert got == _python_stations()
    assert all(set(row) == set(FIELDS) for row in data["s"])
    assert data["ids"] == [s.id for s in STATIONS]
