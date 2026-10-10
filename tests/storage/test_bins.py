import hashlib
import json
import threading

import pytest

from mailroom_reloaded.schemas.manifest import Manifest, next_node
from mailroom_reloaded.storage.bins import (
    Bins,
    doc_id_for,
    load_manifest,
    save_manifest,
)

NODE_ORDER = ["ingest", "bert_primary", "sort", "gate_classify", "extract", "gate_extract", "report_catalog_archive"]


def test_claim_race_single_winner(tmp_path):
    bins = Bins(tmp_path)
    f = bins.inbox / "a.txt"
    f.write_text("hello")
    results = []
    barrier = threading.Barrier(8)

    def worker(i):
        barrier.wait()
        results.append(bins.claim(f, f"w{i}"))

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    winners = [r for r in results if r is not None]
    assert len(results) == 8 and len(winners) == 1
    assert winners[0].read_text() == "hello"
    assert winners[0].parent.parent.name == "processing"
    assert not f.exists()


def test_manifest_roundtrip_atomic(tmp_path):
    bins = Bins(tmp_path)
    p = tmp_path / "d.txt"
    p.write_text("content")
    did = doc_id_for(p)
    assert did == hashlib.sha256(b"content").hexdigest()[:16]
    m = Manifest(doc_id=did, filename="d.txt", content_sha256=hashlib.sha256(b"content").hexdigest(),
                 status="processing", completed_nodes=["ingest"], state={"k": [1, 2]})
    save_manifest(bins, m)
    assert load_manifest(bins, did) == m
    assert load_manifest(bins, "missing") is None
    assert not list(bins.manifests.glob("*.tmp"))
    assert json.loads((bins.manifests / f"{did}.json").read_text())["doc_id"] == did
    m2 = m.model_copy(update={"status": "archived"})
    save_manifest(bins, m2)
    assert load_manifest(bins, did).status == "archived"


def test_move_and_bins(tmp_path):
    bins = Bins(tmp_path)
    f = bins.inbox / "a.txt"
    f.write_text("x")
    claimed = bins.claim(f, "w1")
    assert claimed.parent == bins.processing("w1")
    assert claimed.name.endswith("_a.txt")
    dest = bins.move(claimed, "review")
    assert dest.parent == bins.review and dest.read_text() == "x"
    assert not claimed.exists()
    assert bins.claim(f, "w2") is None


@pytest.mark.parametrize("operation", ["claim", "move"])
@pytest.mark.parametrize("contents", [("first", "second"), ("same", "same")])
def test_same_filename_preserves_both_documents(tmp_path, operation, contents):
    bins = Bins(tmp_path)
    source = bins.inbox / "document.txt"
    destinations = []
    for content in contents:
        source.write_text(content)
        dest = (bins.claim(source, "worker") if operation == "claim"
                else bins.move(source, "review"))
        destinations.append(dest)
        assert not source.exists()

    assert destinations[0] != destinations[1]
    assert tuple(dest.read_text() for dest in destinations) == contents


def test_resume_point():
    m = Manifest(doc_id="x", filename="f", content_sha256="s", status="processing",
                 completed_nodes=["ingest", "bert_primary", "sort"])
    assert next_node(m, NODE_ORDER) == "gate_classify"
    m.completed_nodes = list(NODE_ORDER)
    assert next_node(m, NODE_ORDER) is None


def test_enqueue_publishes_complete_bytes_and_survives_immediate_claim(tmp_path, monkeypatch):
    import os

    bins = Bins(tmp_path)
    real_link = os.link
    claimed = []

    def publish(src, dest):
        assert not [p for p in bins.inbox.iterdir() if not p.name.startswith('.')]
        assert src.read_bytes() == b'complete document'
        real_link(src, dest)
        claimed.append(bins.claim(dest, 'watcher'))

    monkeypatch.setattr(os, 'link', publish)
    dest = bins.enqueue(b'complete document', 'letter.txt')
    assert not dest.exists()
    assert claimed[0].read_bytes() == b'complete document'
    assert not list(bins.inbox.iterdir())


def test_enqueue_collision_preserves_both_contents(tmp_path):
    bins = Bins(tmp_path)
    first = bins.enqueue(b'first', 'letter.txt')
    second = bins.enqueue(b'second', 'letter.txt')
    assert first != second
    assert first.read_bytes() == b'first'
    assert second.read_bytes() == b'second'


def test_enqueue_cleans_staging_on_publish_failure(tmp_path, monkeypatch):
    import os

    bins = Bins(tmp_path)

    def fail(*args):
        raise OSError('disk failure')

    monkeypatch.setattr(os, 'link', fail)
    with pytest.raises(OSError, match='disk failure'):
        bins.enqueue(b'document', 'letter.txt')
    assert not list(bins.inbox.iterdir())


def test_enqueue_publishes_world_readable_file(tmp_path):
    import stat

    bins = Bins(tmp_path)
    dest = bins.enqueue(b'letter', 'letter.txt')
    assert stat.S_IMODE(dest.stat().st_mode) == 0o644
