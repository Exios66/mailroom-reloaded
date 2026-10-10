"""Isolated-scenario conformance harness for the Correspondent stand-in.

Each scenario is injected on its own (state reset in between) so ingress admission
control, driven by *other* scenarios' traffic, cannot hide results. Ingress policy is
untouched: a scenario that is still shed when it runs alone is reported separately
as shed by design (its own traffic trips admission control).

Tuned/held-out split: scenario ids sorted, every third one (index % 3 == 2) is held
out. Rules must be tuned against the ``tuned`` split only.

A pack may instead tag scenarios ``heldout`` (and optionally freeze them). Passing
``heldout=True`` to :func:`run_conformance` selects that tagged batch as the official
held-out measure, independent of the positional heuristic, so a frozen H batch can be
run and reported without leaking into the ``index % 3`` split.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from mailroom_reloaded.sandbox.server.service import SandboxService

__all__ = [
    "HELDOUT_TAG",
    "family_of",
    "format_lofo",
    "format_table",
    "heldout_ids",
    "lofo",
    "run_conformance",
    "split_of",
    "summarise",
]

HELDOUT_TAG = "heldout"


def split_of(ids: list[str]) -> dict[str, str]:
    """Label every third sorted ID ``heldout`` (starting with the third), and the rest ``tuned``."""
    return {
        n: ("heldout" if i % 3 == 2 else "tuned") for i, n in enumerate(sorted(ids))
    }


def heldout_ids(scenarios: dict[str, dict]) -> list[str]:
    """Return sorted IDs tagged ``heldout`` without checking their frozen status."""
    return sorted(
        name
        for name, scenario in scenarios.items()
        if HELDOUT_TAG in (scenario.get("tags") or [])
    )


def _row(svc: SandboxService, name: str) -> dict[str, Any]:
    """Reset the service, run one scenario, and return its check results.

    With a worker, wait at most 120 seconds before evaluating the current
    state; the idle-wait result is ignored. An unavailable evaluation becomes
    ``not_run``, or ``shed`` if any message was shed. Service errors propagate.
    """
    svc.reset()
    svc.inject([name], stagger_seconds=0)
    svc.wait_idle(120)
    msgs = svc.scenario_messages(name)
    shed = [m["id"] for m in msgs if m["state"] == "shed"]
    errors = [m["id"] for m in msgs if m["state"] == "error"]
    ev = svc.evaluation(name) or {
        "verdict": "not_run",
        "checks": [],
        "summary": {"passed": 0, "failed": 0, "unchecked": 0},
    }
    if shed and ev["verdict"] == "not_run":
        verdict = "shed"
    else:
        verdict = ev["verdict"]
    return {
        "scenario": name,
        "verdict": verdict,
        "shed_messages": len(shed),
        "error_messages": len(errors),
        "checks": [
            {"key": c["key"], "ok": c["ok"], "note": c.get("note", "")}
            for c in ev["checks"]
        ],
        "failed_checks": [c["key"] for c in ev["checks"] if c["ok"] is False],
    }


def run_conformance(
    svc: SandboxService, only: list[str] | None = None, *, heldout: bool = False
) -> dict[str, Any]:
    """Run selected scenarios separately and return counts plus per-scenario checks.

    Use a started service with disposable state: reset it before each scenario
    and after a successful run. ``only=None`` or an empty list selects all IDs,
    unless ``heldout`` is set, in which case selection defaults to the scenarios
    tagged ``heldout``. An explicit ``only`` list overrides selection; with
    ``heldout`` each row is labelled ``heldout`` when tagged, else ``tuned``.
    Without ``heldout``, split labels always come from the full pack. Unknown
    IDs raise ``KeyError`` before any scenario runs. Other service errors propagate without
    a final reset.
    """
    ids = svc.content.scenario_ids()
    tagged = set(heldout_ids(svc.content.cs.scenarios))
    if only:
        selected = sorted(only)
    elif heldout:
        selected = sorted(tagged)
    else:
        selected = sorted(ids)
    positional = split_of(ids)
    unknown = sorted(set(only or []) - set(ids))
    if unknown:
        raise KeyError(f"unknown scenario ids: {', '.join(unknown)}")
    rows = []
    for name in selected:
        r = _row(svc, name)
        if heldout:
            r["split"] = "heldout" if name in tagged else "tuned"
        else:
            r["split"] = positional.get(name, "tuned")
        rows.append(r)
    svc.reset()
    return summarise(rows)


def family_of(name: str) -> str:
    """Scenario family = the series letter of the id (A..H, S, T)."""
    return name[:1].upper()


def lofo(rows: list[dict]) -> dict[str, Any]:
    """Summarize supplied results by held-out scenario family.

    Each fold reports held-out and training pass rates, plus failed-check
    counts per held-out family. The macro mean weights families equally; the
    micro rate weights rows equally. All verdicts count in rate denominators.
    No rules are trained here: interpreting rates as held-out performance
    requires tuning without seeing the held-out family beforehand.

    A fold without training rows has a ``None`` training rate. Empty input
    returns ``None`` aggregate rates and no folds.
    """
    if not rows:
        return {
            "protocol": "leave-one-family-out over series letters",
            "macro_mean_held_out_rate": None,
            "micro_pass_rate": None,
            "folds": [],
        }
    fams = sorted({family_of(r["scenario"]) for r in rows})
    folds = []
    for fam in fams:
        held = [r for r in rows if family_of(r["scenario"]) == fam]
        train = [r for r in rows if family_of(r["scenario"]) != fam]
        hp = sum(r["verdict"] == "pass" for r in held)
        tp = sum(r["verdict"] == "pass" for r in train)
        checks: Counter[str] = Counter()
        for r in held:
            checks.update(set(r["failed_checks"]))
        folds.append(
            {
                "held_out_family": fam,
                "held_out_n": len(held),
                "held_out_pass": hp,
                "held_out_rate": round(hp / len(held), 3),
                "training_n": len(train),
                "training_pass": tp,
                "training_rate": round(tp / len(train), 3) if train else None,
                "held_out_failed_checks": dict(sorted(checks.items())),
            }
        )
    return {
        "protocol": "leave-one-family-out over series letters",
        "macro_mean_held_out_rate": round(
            sum(f["held_out_rate"] for f in folds) / len(folds), 3
        ),
        "micro_pass_rate": round(
            sum(r["verdict"] == "pass" for r in rows) / len(rows), 3
        ),
        "folds": folds,
    }


def format_lofo(rep: dict[str, Any]) -> str:
    """Format a ``lofo`` report as a text table with macro and micro pass rates."""
    macro_rate = (
        "n/a"
        if rep["macro_mean_held_out_rate"] is None
        else f"{rep['macro_mean_held_out_rate']:.3f}"
    )
    micro_rate = (
        "n/a"
        if rep["micro_pass_rate"] is None
        else f"{rep['micro_pass_rate']:.3f}"
    )
    lines = [
        f"{'fold (held-out family)':<24}{'n':>3}{'pass':>6}{'rate':>7}   training rate"
    ]
    for f in rep["folds"]:
        training_rate = (
            "n/a" if f["training_rate"] is None else f"{f['training_rate']:.2f}"
        )
        lines.append(
            f"{f['held_out_family']:<24}{f['held_out_n']:>3}{f['held_out_pass']:>6}{f['held_out_rate']:>7.2f}   {training_rate}"
        )
    lines.append(
        f"macro mean held-out rate {macro_rate}; micro pass rate {micro_rate}"
    )
    return "\n".join(lines)


def summarise(rows: list[dict]) -> dict[str, Any]:
    """Return verdict counts by split, failed-check occurrence counts, and the original rows."""

    def count(rs: list[dict]) -> dict[str, int]:
        """Count each reported verdict and include all rows in the total."""
        c = Counter(r["verdict"] for r in rs)
        return {
            "pass": c["pass"],
            "fail": c["fail"],
            "not_run": c["not_run"],
            "shed": c["shed"],
            "total": len(rs),
        }

    by_check: Counter[str] = Counter()
    for r in rows:
        by_check.update(r["failed_checks"])
    return {
        "all": count(rows),
        "tuned": count([r for r in rows if r["split"] == "tuned"]),
        "heldout": count([r for r in rows if r["split"] == "heldout"]),
        "failed_checks": dict(sorted(by_check.items())),
        "scenarios": rows,
    }


def format_table(result: dict[str, Any]) -> str:
    """Format scenario verdicts, failed checks, and split totals as a text table."""
    lines = [f"{'scenario':<44}{'split':<8}{'verdict':<9}failed checks"]
    for r in result["scenarios"]:
        lines.append(
            f"{r['scenario']:<44}{r['split']:<8}{r['verdict']:<9}{', '.join(r['failed_checks'])}"
        )
    lines.append("")
    for k in ("all", "tuned", "heldout"):
        c = result[k]
        lines.append(
            f"{k:<8} pass {c['pass']} fail {c['fail']} not_run {c['not_run']} shed {c['shed']} (of {c['total']})"
        )
    lines.append(
        "failed checks: "
        + ", ".join(f"{k}={v}" for k, v in result["failed_checks"].items())
    )
    return "\n".join(lines)


def dumps(result: dict[str, Any], *, slim: bool = False) -> str:
    """JSON text; ``slim`` drops per-check rows (the committed baseline)."""
    if slim:
        result = {
            **result,
            "scenarios": [
                {k: v for k, v in r.items() if k != "checks"}
                for r in result["scenarios"]
            ],
        }
    return json.dumps(result, indent=1, sort_keys=True) + "\n"
