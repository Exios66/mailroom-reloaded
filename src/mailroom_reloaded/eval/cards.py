"""SAND-37-parity score & cost cards (Task 21, spec section 8).

``build_card`` emits a ``mailroom.card/v1`` JSON dict with the same blocks as
the sandbox card: ``conditions``, ``cost``, ``documents``,
``engine_telemetry``, ``latency``, ``quality``, ``throughput``, ``time``,
``tokens`` and ``concurrency``. ``render_card_md`` renders it as Markdown, and
``build_master`` aggregates cards into the posture / serving-efficiency /
quality-and-cost tables of ``SAND-37-MASTER-SCORE-COST-CARD.md``.

Formulas only: no plotting and no CLI. A metric the rows did not capture
renders as ``None`` (JSON) or ``not captured`` (Markdown), never a fabricated
zero.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from mailroom_reloaded.eval.cost import cell_cost, token_split
from mailroom_reloaded.eval.metrics import specialist_kpis
from mailroom_reloaded.eval.vllm_telemetry import ReplicaTelemetry

__all__ = ["CARD_SCHEMA", "build_card", "build_master", "render_card_md"]

CARD_SCHEMA = "mailroom.card/v1"
NOT_CAPTURED = "not captured"

_BLOCK_KEYS = (
    "conditions",
    "cost",
    "documents",
    "engine_telemetry",
    "latency",
    "quality",
    "throughput",
    "time",
    "tokens",
    "concurrency",
)

_LABELS = {
    "correspondence": "Correspondence",
    "insurance_claim": "Insurance Claims",
    "corporate_record": "Corporate Records",
    "contract": "Contracts",
    "merger_agreement": "Merger Agreements",
}
_ORDER = (
    "insurance_claim",
    "contract",
    "corporate_record",
    "correspondence",
    "merger_agreement",
)


# --------------------------------------------------------------------------- helpers


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _p95(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]


def _sum(rows: Sequence[Mapping[str, Any]], key: str) -> int:
    total = 0
    for row in rows:
        try:
            total += int(row.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _load_rows(run_id: str, doc_type: str | None) -> list[dict[str, Any]]:
    """Read ``eval_docs`` rows for a run, optionally filtered by class."""
    from sqlalchemy import text

    from mailroom_reloaded.eval import runner

    engine = runner._engine()
    runner._ensure_table(engine)
    query = "SELECT * FROM eval_docs WHERE run_id = :run_id"
    params: dict[str, Any] = {"run_id": run_id}
    if doc_type is not None:
        query += " AND doc_type = :doc_type"
        params["doc_type"] = doc_type
    query += " ORDER BY filename"
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(text(query), params).mappings()]


def _error_kind(value: Any) -> str:
    text = str(value or "").strip()
    return text.split(":", 1)[0].strip() or "unknown"


def _normalise_telemetry(telemetry: Any, gpus: int) -> dict[str, Any]:
    """Return replica dictionaries, capture status, and the expected GPU replica count.

    Accept one mapping/telemetry record or a list/tuple; skip unsupported entries.
    """
    if telemetry is None:
        return {"captured": False, "expected_replicas": gpus, "replicas": []}
    entries = telemetry if isinstance(telemetry, (list, tuple)) else [telemetry]
    replicas: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if isinstance(entry, ReplicaTelemetry):
            data = entry._asdict()
        elif isinstance(entry, Mapping):
            data = dict(entry)
        else:
            continue
        data.setdefault("replica", str(index))
        replicas.append(data)
    return {
        "captured": bool(replicas),
        "expected_replicas": gpus,
        "replicas": replicas,
    }


# --------------------------------------------------------------------------- card


def build_card(
    run_id: str,
    doc_type: str | None,
    *,
    rows: Sequence[Mapping[str, Any]] | None = None,
    telemetry: Any = None,
    conditions: Mapping[str, Any] | None = None,
    wall_s: float | None = None,
    gpus: int = 1,
    concurrency: int = 1,
    usd_per_hour: float = 0.80,
) -> dict[str, Any]:
    """Build one ``mailroom.card/v1`` dict for a run and specialist cell.

    ``rows`` defaults to the ``eval_docs`` rows for ``run_id``; tests inject
    rows directly. ``telemetry`` is a :class:`ReplicaTelemetry`, a list of them,
    or a mapping.
    """
    if rows is None:
        rows = _load_rows(run_id, doc_type)
    rows = [dict(row) for row in rows]

    cond: dict[str, Any] = {
        "model": None,
        "gpu": "L4",
        "gpu_usd_per_hour": usd_per_hour,
        "replicas": gpus,
        "concurrency": concurrency,
        "dataset": {},
        "engine": {},
        "prompt": None,
        "temperature": None,
        "caps": {},
        "run_id": run_id,
    }
    if conditions:
        cond.update(dict(conditions))

    latencies = [
        v for v in (_num(row.get("latency_s")) for row in rows) if v is not None
    ]
    ok = sum(
        1
        for row in rows
        if str(row.get("status") or "").lower() in {"ok", "archived"}
        or bool(row.get("schema_valid"))
    )
    total = len(rows)
    wall = _num(wall_s)
    if wall is None:
        wall = round(sum(latencies), 6) if latencies else None

    prompt_tokens = _sum(rows, "prompt_tokens")
    completion_tokens = _sum(rows, "completion_tokens")
    tokens_total = prompt_tokens + completion_tokens
    completions = [
        v for v in (_num(row.get("completion_tokens")) for row in rows) if v is not None
    ]

    cost = cell_cost(
        wall,
        gpus=gpus,
        usd_per_hour=usd_per_hour,
        ok=ok,
        total=total,
        tokens=tokens_total,
    )

    spec_kpi = specialist_kpis(rows, doc_type)
    overall = [
        float(row["overall_score"])
        for row in rows
        if row.get("overall_score") is not None
    ]
    error_kinds = Counter(
        _error_kind(row.get("error_kind")) for row in rows if row.get("error_kind")
    )
    schema_values = [
        bool(row.get("schema_valid")) for row in rows if "schema_valid" in row
    ]

    parallelism = (sum(latencies) / wall) if wall and latencies else None

    card: dict[str, Any] = {
        "schema": CARD_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "run_id": run_id,
        "doc_type": doc_type,
        "specialist": _LABELS.get(str(doc_type), str(doc_type)),
        "n": total,
        "conditions": cond,
        "time": {
            "wall_seconds": wall,
            "gpu_seconds": round(wall * max(1, gpus), 6) if wall is not None else None,
            "cold_boot_seconds": cond.get("cold_boot_seconds"),
        },
        "cost": cost,
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": tokens_total,
            "completion_share": (
                round(completion_tokens / tokens_total, 4) if tokens_total else None
            ),
            "per_document": round(tokens_total / total, 4) if total else None,
            "completion_p95": _p95(completions),
            "completion_max": max(completions) if completions else None,
            "split": token_split(rows),
        },
        "latency": {
            "mean": round(statistics.mean(latencies), 6) if latencies else None,
            "p50": round(statistics.median(latencies), 6) if latencies else None,
            "p95": _p95(latencies),
            "max": max(latencies) if latencies else None,
        },
        "throughput": {
            "documents_per_minute": (
                round(total / wall * 60.0, 4) if wall and total else None
            ),
            "tokens_per_second": (
                round(tokens_total / wall, 4) if wall and tokens_total else None
            ),
            "tokens_per_second_per_gpu": (
                round(tokens_total / wall / max(1, gpus), 4)
                if wall and tokens_total
                else None
            ),
        },
        "concurrency": {
            "parallelism": round(parallelism, 4) if parallelism else None,
            "occupancy": (
                round(parallelism / max(1, concurrency), 4) if parallelism else None
            ),
            "slot_utilization": (
                round(parallelism / wall, 4) if parallelism and wall else None
            ),
        },
        "quality": {
            "documents": total,
            "ok": ok,
            "errors": total - ok,
            "error_kinds": dict(error_kinds),
            "parse_errors": spec_kpi["parse_errors"],
            "schema_valid_rate": (
                round(sum(schema_values) / len(schema_values), 4)
                if schema_values
                else None
            ),
            "overall_mean": (
                round(statistics.mean(overall), 4)
                if overall
                else spec_kpi["suite_mean"]
            ),
            "overall_sd": (
                round(statistics.pstdev(overall), 4)
                if len(overall) > 1
                else spec_kpi["sd"]
            ),
            "overall_min": min(overall) if overall else spec_kpi["min"],
            "overall_max": max(overall) if overall else spec_kpi["max"],
            "clause": spec_kpi["clause"],
            "maud": spec_kpi["maud"],
            "precision": spec_kpi["precision"],
            "recall": spec_kpi["recall"],
            "f1": spec_kpi["f1"],
            "f2": spec_kpi["f2"],
            "micro_f1": spec_kpi["micro_f1"],
            "mean_doc_f1": spec_kpi["mean_doc_f1"],
            "judge_scorer_agreement": spec_kpi["judge_scorer_agreement"],
        },
        "engine_telemetry": _normalise_telemetry(telemetry, gpus),
        "documents": [
            {
                "filename": row.get("filename"),
                "doc_id": row.get("doc_id"),
                "ok": str(row.get("status") or "").lower() in {"ok", "archived"}
                or bool(row.get("schema_valid")),
                "score": row.get("overall_score"),
                "latency_seconds": row.get("latency_s"),
                "prompt_tokens": row.get("prompt_tokens"),
                "completion_tokens": row.get("completion_tokens"),
                "calls": row.get("calls"),
                "schema_valid": bool(row.get("schema_valid")),
                "error_kind": row.get("error_kind"),
            }
            for row in rows
        ],
    }
    return card


# --------------------------------------------------------------------------- markdown


def _fmt(value: Any, digits: int = 4, suffix: str = "") -> str:
    if value is None:
        return NOT_CAPTURED
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}{suffix}"
    if isinstance(value, float):
        return f"{value:,.{digits}f}{suffix}"
    return f"{value}{suffix}"


def _money(value: Any, digits: int = 6) -> str:
    return NOT_CAPTURED if value is None else f"${float(value):,.{digits}f}"


def _pct(value: Any, digits: int = 1) -> str:
    return NOT_CAPTURED if value is None else f"{float(value) * 100:.{digits}f}%"


def render_card_md(card: Mapping[str, Any]) -> str:
    """Render a card JSON as a SAND-37-style Markdown table."""
    cond = card.get("conditions") or {}
    ds = cond.get("dataset") or {}
    telemetry = card.get("engine_telemetry") or {}
    reps = telemetry.get("replicas") or []
    quality = card.get("quality") or {}
    clause = quality.get("clause") or {}
    maud = quality.get("maud") or {}
    split = (card.get("tokens") or {}).get("split") or {}

    lines = [
        (
            f"# {card.get('specialist')} — {cond.get('replicas', 1)}× {cond.get('gpu', 'L4')} "
            f"· {card.get('n', 0)} documents"
        ),
        "",
        (
            f"**Run:** `{card.get('run_id')}` · **Model:** {cond.get('model') or NOT_CAPTURED} · "
            f"{cond.get('gpu', 'L4')} @ ${cond.get('gpu_usd_per_hour', 0.80):.2f}/GPU-hr"
        ),
        "",
        "## Conditions",
        "",
        "| Condition | Value |",
        "| --- | --- |",
        f"| Doc type | `{card.get('doc_type')}` |",
        f"| Prompt | {cond.get('prompt') or NOT_CAPTURED} |",
        f"| Temperature | {cond.get('temperature')} |",
        f"| Input cap (chars) | {_fmt((cond.get('caps') or {}).get('input_chars'))} |",
        f"| Output cap (tokens) | {_fmt((cond.get('caps') or {}).get('output_tokens'))} |",
        f"| GPUs / replicas | {cond.get('replicas')} |",
        f"| Concurrency | {cond.get('concurrency')} |",
        (
            f"| Dataset | {ds.get('repo') or NOT_CAPTURED} {ds.get('config') or ''} "
            f"@ {ds.get('revision') or NOT_CAPTURED}, split={ds.get('split') or NOT_CAPTURED}, "
            f"seed {ds.get('seed')} |"
        ),
        "",
        "## Score & cost card",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| **Time** wall (busy) | {_fmt((card['time'] or {}).get('wall_seconds'), 2, ' s')} |",
        f"| **Cost** busy-window GPU $ | {_money((card['cost'] or {}).get('busy_gpu_usd'))} |",
        f"| Cost per document | {_money((card['cost'] or {}).get('usd_per_document'))} |",
        f"| Cost per ok document | {_money((card['cost'] or {}).get('usd_per_ok_document'))} |",
        f"| Cost per 1M tokens | {_money((card['cost'] or {}).get('usd_per_million_tokens'))} |",
        f"| **Tokens** prompt | {_fmt((card['tokens'] or {}).get('prompt'))} |",
        f"| Completion tokens | {_fmt((card['tokens'] or {}).get('completion'))} |",
        (
            f"| Completion p95 / max | {_fmt((card['tokens'] or {}).get('completion_p95'))} / "
            f"{_fmt((card['tokens'] or {}).get('completion_max'))} |"
        ),
        f"| Instruction tokens per call | {_fmt(split.get('instruction_per_call'))} |",
        f"| Characters per token | {_fmt(split.get('chars_per_token'), 2)} |",
        f"| **Throughput** docs / minute | {_fmt((card['throughput'] or {}).get('documents_per_minute'), 2)} |",
        f"| Tokens / second / GPU | {_fmt((card['throughput'] or {}).get('tokens_per_second_per_gpu'), 1)} |",
        f"| **Latency** mean | {_fmt((card['latency'] or {}).get('mean'), 2, ' s')} |",
        (
            f"| p50 / p95 / max | {_fmt((card['latency'] or {}).get('p50'), 2)} / "
            f"{_fmt((card['latency'] or {}).get('p95'), 2)} / "
            f"{_fmt((card['latency'] or {}).get('max'), 2)} |"
        ),
        f"| **Concurrency** occupancy | {_pct((card['concurrency'] or {}).get('occupancy'))} |",
        f"| Slot utilization | {_pct((card['concurrency'] or {}).get('slot_utilization'))} |",
        f"| **Engine** requests | {_fmt(sum(r.get('requests') or 0 for r in reps))} |",
        f"| Length-capped finishes | {_fmt(sum(r.get('length_finishes') or 0 for r in reps))} |",
        f"| Preemptions | {_fmt(sum(r.get('preemptions') or 0 for r in reps))} |",
        "| Prefix-cache hit rate | "
        + (
            " / ".join(_pct(r.get("prefix_cache_hit_rate")) for r in reps)
            if reps
            else NOT_CAPTURED
        )
        + " |",
        "| Mean TTFT | "
        + (
            " / ".join(_fmt(r.get("ttft_mean_seconds"), 3, " s") for r in reps)
            if reps
            else NOT_CAPTURED
        )
        + " |",
        f"| **Quality** ok / total | {quality.get('ok')} / {quality.get('documents')} |",
        f"| Schema-valid rate | {_fmt(quality.get('schema_valid_rate'), 2)} |",
        (
            f"| Overall score mean (sd) | {_fmt(quality.get('overall_mean'))} "
            f"({_fmt(quality.get('overall_sd'), 3)}) |"
        ),
        (
            f"| Micro precision / recall / F1 / F2 | {_fmt(quality.get('precision'), 3)} / "
            f"{_fmt(quality.get('recall'), 3)} / {_fmt(quality.get('f1'), 3)} / "
            f"{_fmt(quality.get('f2'), 3)} |"
        ),
        f"| Mean per-document F1 | {_fmt(quality.get('mean_doc_f1'))} |",
        f"| Parse errors | {quality.get('parse_errors')} |",
        f"| Judge scorer agreement | {_pct(quality.get('judge_scorer_agreement'))} |",
    ]
    if clause.get("kind") == "cuad" and clause.get("docs_labeled"):
        lines += [
            (
                f"| CUAD clause precision / recall / F1 | {_fmt(clause.get('precision'), 3)} / "
                f"{_fmt(clause.get('recall'), 3)} / {_fmt(clause.get('f1'), 3)} |"
            ),
            f"| CUAD value checks | {clause.get('value_correct')} / {clause.get('value_checked')} |",
        ]
    if maud.get("questions"):
        lines += [
            (
                f"| MAUD accuracy (coverage) | {_pct(maud.get('accuracy'))} "
                f"({_pct(maud.get('coverage'))}) |"
            ),
            f"| MAUD precision on answered | {_pct(maud.get('precision_answered'))} |",
        ]

    lines += [
        "",
        "## Per-document results",
        "",
        "| # | Document | OK | Score | Latency (s) | Prompt tok | Completion tok | Error |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for index, doc in enumerate(card.get("documents") or [], 1):
        lines.append(
            f"| {index} | `{doc.get('filename')}` | {'yes' if doc.get('ok') else 'no'} | "
            f"{_fmt(doc.get('score')) if doc.get('score') is not None else '—'} | "
            f"{_fmt(doc.get('latency_seconds'), 1) if doc.get('latency_seconds') is not None else '—'} | "
            f"{doc.get('prompt_tokens') if doc.get('prompt_tokens') is not None else '—'} | "
            f"{doc.get('completion_tokens') if doc.get('completion_tokens') is not None else '—'} | "
            f"{doc.get('error_kind') or '—'} |"
        )
    lines += ["", f"_Generated {card.get('generated_at')} by mailroom card._", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- master


def _pooled(cards: Sequence[Mapping[str, Any]], gpus: int) -> dict[str, Any]:
    """Pool card counts, costs in USD, and throughput from summed wall seconds.

    Missing values contribute zero; rates without nonzero inputs return ``None``.
    Per-GPU throughput divides by at least one GPU.
    """
    docs = sum(int(c.get("n") or 0) for c in cards)
    ok = sum(int((c.get("quality") or {}).get("ok") or 0) for c in cards)
    errors = sum(int((c.get("quality") or {}).get("errors") or 0) for c in cards)
    wall = sum(float((c.get("time") or {}).get("wall_seconds") or 0.0) for c in cards)
    busy = sum(float((c.get("cost") or {}).get("busy_gpu_usd") or 0.0) for c in cards)
    tokens = sum(int((c.get("tokens") or {}).get("total") or 0) for c in cards)
    return {
        "cells": len(cards),
        "documents": docs,
        "ok": ok,
        "errors": errors,
        "error_rate": round(errors / docs, 4) if docs else None,
        "wall_seconds": wall or None,
        "busy_gpu_usd": busy or None,
        "tokens": tokens,
        "usd_per_document": round(busy / docs, 8) if busy and docs else None,
        "usd_per_million_tokens": round(busy / tokens * 1e6, 8)
        if busy and tokens
        else None,
        "tokens_per_second": round(tokens / wall, 4) if wall and tokens else None,
        "tokens_per_second_per_gpu": (
            round(tokens / wall / max(1, gpus), 4) if wall and tokens else None
        ),
        "documents_per_minute": round(docs / wall * 60.0, 4) if wall and docs else None,
    }


def _card_score(card: Mapping[str, Any]) -> float | None:
    quality = card.get("quality") or {}
    maud = quality.get("maud") or {}
    if maud.get("accuracy") is not None:
        return maud.get("accuracy")
    return quality.get("overall_mean")


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return lines


def build_master(
    run_ids: Sequence[str],
    *,
    cards: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], str]:
    """Aggregate cells into the SAND-37 master card.

    Returns ``(data, markdown)``. ``cards`` defaults to building one card per
    distinct doc type found in each run's ``eval_docs`` rows.
    """
    if cards is None:
        built: list[dict[str, Any]] = []
        for run_id in run_ids:
            rows = _load_rows(run_id, None)
            doc_types = sorted(
                {row.get("doc_type") for row in rows if row.get("doc_type")}
            )
            for doc_type in doc_types or [None]:
                class_rows = (
                    rows
                    if doc_type is None
                    else [row for row in rows if row.get("doc_type") == doc_type]
                )
                built.append(build_card(run_id, doc_type, rows=class_rows))
        cards = built
    cards = [dict(card) for card in cards]

    gpus = max(
        (int((card.get("conditions") or {}).get("replicas") or 1) for card in cards),
        default=1,
    )
    pooled = _pooled(cards, gpus)

    by_specialist: dict[str, list[Mapping[str, Any]]] = {}
    for card in cards:
        by_specialist.setdefault(str(card.get("doc_type")), []).append(card)

    data = {
        "schema": CARD_SCHEMA,
        "kind": "master",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "run_ids": list(run_ids),
        "gpus": gpus,
        "cells": len(cards),
        "pooled": pooled,
        "by_specialist": {
            key: {
                "label": _LABELS.get(key, key),
                "cells": len(value),
                "score": _card_score(value[-1]),
                "ok": sum(int((c.get("quality") or {}).get("ok") or 0) for c in value),
                "n": sum(int(c.get("n") or 0) for c in value),
                "p50_latency_seconds": (value[-1].get("latency") or {}).get("p50"),
                "usd_per_ok_document": (value[-1].get("cost") or {}).get(
                    "usd_per_ok_document"
                ),
            }
            for key, value in by_specialist.items()
        },
    }
    return data, render_master_md(data)


def render_master_md(data: Mapping[str, Any]) -> str:
    """Render the master card: postures, serving efficiency, specialist table, cost."""
    cards = data.get("by_specialist") or {}
    pooled = data.get("pooled") or {}
    lines = [
        "# L4 Specialist Grid: Results and Cost Summary",
        "",
        "## Postures",
        "",
        "| Cell | GPUs | Documents | Status |",
        "| --- | ---: | ---: | --- |",
    ]
    for key in _ORDER:
        if key not in cards:
            continue
        row = cards[key]
        lines.append(
            f"| {row.get('label')} | {data.get('gpus')} | {row.get('n')} / {row.get('ok')} | "
            f"{row.get('cells')} cell(s) |"
        )
    lines += [
        "",
        "## Serving efficiency (pooled across specialists)",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Error rate | {_pct(pooled.get('error_rate'))} |",
        f"| Documents per minute | {_fmt(pooled.get('documents_per_minute'), 2)} |",
        f"| Tokens per second per GPU | {_fmt(pooled.get('tokens_per_second_per_gpu'), 1)} |",
        f"| GPU cost per document | {_money(pooled.get('usd_per_document'))} |",
        "",
        "## Quality and cost by specialist",
        "",
        "| Specialist | Score | ok / n | p50 latency (s) | $ per ok document |",
        "| --- | :---: | :---: | :---: | :---: |",
    ]
    for key in _ORDER:
        if key not in cards:
            continue
        row = cards[key]
        lines.append(
            f"| {row.get('label')} | {_fmt(row.get('score'), 3)} | {row.get('ok')} / {row.get('n')} | "
            f"{_fmt(row.get('p50_latency_seconds'), 1)} | {_money(row.get('usd_per_ok_document'))} |"
        )
    lines += [
        "",
        "## Cost",
        "",
        "| Session | Documents | Busy-window GPU | Cost per document | Cost per 1M tokens |",
        "| --- | ---: | ---: | ---: | ---: |",
        (
            f"| All cells | {pooled.get('documents')} | {_money(pooled.get('busy_gpu_usd'), 4)} | "
            f"{_money(pooled.get('usd_per_document'))} | "
            f"{_money(pooled.get('usd_per_million_tokens'))} |"
        ),
        "",
        "- Busy-window GPU $ is each cell's busy wall × GPUs × the GPU hourly rate.",
        "",
    ]
    return "\n".join(lines)
