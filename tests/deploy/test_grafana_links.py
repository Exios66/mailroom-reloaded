"""Grafana dashboards: replay / phoenix links and queries against metrics that are really emitted."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from mailroom_reloaded.obs.metrics import _SPECS

DASHBOARDS = Path(__file__).resolve().parents[2] / "deploy" / "grafana" / "dashboards"
OURS = ("pipeline", "quality")


def _load(name: str) -> dict:
    return json.loads((DASHBOARDS / f"{name}.json").read_text())


def _prometheus_names() -> set[str]:
    """Series names the OTel collector's Prometheus exporter produces for ``obs/metrics._SPECS``."""
    names: set[str] = set()
    for otel, kind, unit in _SPECS.values():
        base = re.sub(r"[^a-zA-Z0-9]", "_", otel)
        if unit == "s":
            base += "_seconds"
        if kind == "counter":
            names.add(base + "_total")
        elif kind == "histogram":
            names.update({base + "_bucket", base + "_sum", base + "_count"})
        else:
            names.add(base)
    return names


def _queries(dash: dict) -> list[str]:
    out = [
        v["query"]["query"]
        for v in dash["templating"]["list"]
        if v.get("type") == "query"
    ]
    for panel in dash["panels"]:
        out += [t["expr"] for t in panel.get("targets", [])]
    return out


def _constants(dash: dict) -> dict[str, str]:
    return {
        v["name"]: v["query"]
        for v in dash["templating"]["list"]
        if v.get("type") == "constant"
    }


@pytest.mark.parametrize("name", OURS)
def test_dashboards_link_to_replay_and_phoenix(name) -> None:
    dash = _load(name)
    links = {link["title"]: link for link in dash["links"]}
    assert set(links) == {"replay ↗", "phoenix ↗"}
    replay = links["replay ↗"]
    assert (
        replay["url"] == "${public_url}/tui#replay=run:${run_id}"
        and replay["targetBlank"] is True
    )
    assert links["phoenix ↗"]["url"] == "${phoenix_url}"
    consts = _constants(dash)
    assert consts == {
        "public_url": "http://localhost:8000",
        "phoenix_url": "http://localhost:6006",
    }


@pytest.mark.parametrize("name", OURS)
def test_run_id_variable_reads_a_label_that_is_emitted(name) -> None:
    dash = _load(name)
    run_var = next(v for v in dash["templating"]["list"] if v["name"] == "run_id")
    assert run_var["query"]["query"] == "label_values(mailroom_documents_total, run_id)"
    assert "mailroom_documents_total" in _prometheus_names() | {
        "mailroom_documents_total"
    }


@pytest.mark.parametrize("name", OURS)
def test_every_queried_metric_is_one_the_code_emits(name) -> None:
    emitted = _prometheus_names()
    unknown = set()
    for query in _queries(_load(name)):
        for metric in re.findall(r"\b((?:mailroom|gen_ai)_[a-z0-9_]+)", query):
            if metric not in emitted:
                unknown.add(metric)
    assert not unknown, f"queried but never emitted: {sorted(unknown)}"


def test_no_panel_still_queries_the_never_emitted_eval_gauges() -> None:
    for name in OURS:
        assert "mailroom_eval_" not in json.dumps(_load(name))


def test_the_per_run_table_links_each_row_to_its_replay() -> None:
    table = next(p for p in _load("quality")["panels"] if p["type"] == "table")
    links = table["fieldConfig"]["defaults"]["links"]
    assert links[0]["url"] == "${public_url}/tui#replay=run:${__data.fields.run_id}"
    assert "run_id" in table["targets"][0]["expr"]


def test_pipeline_has_a_decisions_row_mirroring_the_replay_ticker() -> None:
    panels = _load("pipeline")["panels"]
    assert any(p["type"] == "row" and p["title"] == "Decisions" for p in panels)
    exprs = " ".join(t["expr"] for p in panels for t in p.get("targets", []))
    for series in (
        "mailroom_retries_total",
        "mailroom_escalations_total",
        "mailroom_review_causes_total",
    ):
        assert series in exprs
    ids = [p["id"] for p in panels]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("name", OURS)
def test_every_panel_filters_on_the_run_id_variable(name) -> None:
    for panel in _load(name)["panels"]:
        for target in panel.get("targets", []):
            assert 'run_id=~"$run_id"' in target["expr"], panel["title"]
