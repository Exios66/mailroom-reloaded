#!/usr/bin/env python3
"""Pull one document from ``Lucius-Morningstar/mailroom-dataset`` and optionally upload it.

Downloads a parquet shard of the blind ``default`` config from the Hub
(``huggingface_hub``), extracts one row's ``doc_text``, writes it to a ``.txt``
file, and — with ``--upload`` — POSTs it to a running mailroom API
(``POST /v1/documents``) so the run can be inspected and replayed in ``/tui``.

    uv run python scripts/fetch_dataset_sample.py --out data/samples/sample.txt
    uv run python scripts/fetch_dataset_sample.py --upload http://localhost:8000

The blind ``default`` config carries only ``filename`` / ``doc_text`` / ``prompt``
/ ``metadata`` — no labels — so the uploaded document is exactly what an incoming
mailroom item looks like.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO_DEFAULT = "Lucius-Morningstar/mailroom-dataset"


def _slug(name: str) -> str:
    """A filesystem-safe stem from a dataset filename."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    return (stem[:80] or "sample")


def _download_row(repo: str, config: str, split: str, index: int, revision: str | None):
    """Return row ``index`` of ``config``/``split`` (downloading only that shard)."""
    try:
        import pyarrow.parquet as pq
        from huggingface_hub import HfApi, hf_hub_download
    except ImportError as exc:  # pragma: no cover - guidance for a missing extra
        raise SystemExit(
            "fetch_dataset_sample needs huggingface_hub and pyarrow: "
            "uv sync --extra eval"
        ) from exc

    api = HfApi(token=os.environ.get("HUGGING_FACE_HUB_TOKEN") or os.environ.get("HF_TOKEN"))
    prefix = f"parquet/{config}/{split}/"
    shards = sorted(
        f
        for f in api.list_repo_files(repo, repo_type="dataset", revision=revision)
        if f.startswith(prefix) and f.endswith(".parquet")
    )
    if not shards:
        raise SystemExit(f"no parquet shards for {config}/{split} in {repo}")

    remaining = index
    for shard in shards:
        local = hf_hub_download(
            repo, shard, repo_type="dataset", revision=revision,
            token=os.environ.get("HUGGING_FACE_HUB_TOKEN") or os.environ.get("HF_TOKEN"),
        )
        rows = pq.read_table(local).to_pylist()
        if remaining < len(rows):
            return rows[remaining]
        remaining -= len(rows)
    raise SystemExit(f"index {index} out of range for {config}/{split} ({repo})")


def _upload(base_url: str, path: Path, token: str | None) -> dict:
    """POST the document to ``POST /v1/documents`` and return the JSON response."""
    import httpx

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = base_url.rstrip("/") + "/v1/documents"
    with path.open("rb") as fh:
        resp = httpx.post(url, files={"file": (path.name, fh, "text/plain")},
                          headers=headers, timeout=60.0)
    resp.raise_for_status()
    return resp.json()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=REPO_DEFAULT)
    parser.add_argument("--config", default="default", help="dataset config (default: default)")
    parser.add_argument("--split", default="test", choices=("train", "test"))
    parser.add_argument("--index", type=int, default=0, help="row index within the config/split")
    parser.add_argument("--revision", default=None, help="dataset revision (tag/branch/commit)")
    parser.add_argument("--out", default="data/samples/sample.txt", help="output .txt path (or dir)")
    parser.add_argument("--upload", metavar="BASE_URL",
                        help="POST the document to a running API, e.g. http://localhost:8000")
    parser.add_argument("--api-token", default=os.environ.get("MAILROOM_API_TOKEN"))
    args = parser.parse_args(argv)

    row = _download_row(args.repo, args.config, args.split, args.index, args.revision)
    text = row.get("doc_text") or ""
    filename = str(row.get("filename") or f"sample-{args.index}.htm")

    out = Path(args.out)
    if out.suffix == "":
        out = out / f"{_slug(filename)}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    print(f"pulled {args.repo} [{args.config}/{args.split}#{args.index}]")
    print(f"  dataset filename : {filename}")
    print(f"  wrote            : {out} ({len(text)} chars)")

    if args.upload:
        result = _upload(args.upload, out, args.api_token)
        print(f"  uploaded         : {json.dumps(result)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
