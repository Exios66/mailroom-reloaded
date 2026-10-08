import pytest
from sqlalchemy import text

from mailroom_reloaded.schemas.audit import AuditLogEntry
from mailroom_reloaded.storage import audit_log, catalog
from mailroom_reloaded.storage.db import init_db


@pytest.fixture
def engine(tmp_path):
    eng = init_db(tmp_path / "mailroom.db")
    yield eng
    eng.dispose()


def _fill(engine, n=5, doc="d1"):
    for i in range(n):
        audit_log.append(doc, f"node{i}", "done", {"i": i}, engine=engine)


def test_wal_enabled(engine):
    with engine.connect() as c:
        assert c.execute(text("PRAGMA journal_mode")).scalar() == "wal"


def test_chain_verifies(engine):
    _fill(engine)
    es = audit_log.entries("d1", engine=engine)
    assert [e.seq for e in es] == [1, 2, 3, 4, 5]
    assert es[0].prev_hash == ""
    assert es[1].prev_hash == es[0].entry_hash
    assert isinstance(es[0], AuditLogEntry)
    res = audit_log.verify_chain(es)
    assert res.ok and res.broken_at is None


def test_tamper_detected(engine):
    _fill(engine)
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE audit_log SET payload='{\"i\": 99}' WHERE doc_id='d1' AND seq=3"
            )
        )
    res = audit_log.verify_chain(audit_log.entries("d1", engine=engine))
    assert not res.ok
    assert res.broken_at == 3


def test_deleted_row_detected(engine):
    _fill(engine)
    with engine.begin() as c:
        c.execute(text("DELETE FROM audit_log WHERE doc_id='d1' AND seq=2"))
    res = audit_log.verify_chain(audit_log.entries("d1", engine=engine))
    assert not res.ok and res.broken_at == 3


def test_append_idempotent_per_node(engine):
    a = audit_log.append("d1", "sorter", "classified", {"c": "contract"}, engine=engine)
    b = audit_log.append("d1", "sorter", "classified", {"c": "contract"}, engine=engine)
    assert a == b
    assert len(audit_log.entries("d1", engine=engine)) == 1
    audit_log.append("d1", "sorter", "classified", {"c": "other"}, engine=engine)
    assert len(audit_log.entries("d1", engine=engine)) == 2


def test_docs_are_independent(engine):
    audit_log.append("a", "n", "e", {}, engine=engine)
    audit_log.append("b", "n", "e", {}, engine=engine)
    assert audit_log.entries("b", engine=engine)[0].seq == 1


def test_default_engine_uses_base_dir(tmp_path, monkeypatch):
    from mailroom_reloaded.storage import db

    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(db, "_default_engine", None)
    e = audit_log.append("x", "n", "e", {"k": 1})
    assert e.seq == 1
    assert (tmp_path / "mailroom.db").exists()
    db.get_engine().dispose()
    monkeypatch.setattr(db, "_default_engine", None)


def test_catalog_upsert_get_list(engine):
    def rec(i, status):
        return catalog.CatalogRecord(
            doc_id=f"d{i}",
            filename=f"f{i}.pdf",
            doc_type="contract",
            status=status,
            file_sha256="ab" * 32,
        )

    for i in range(3):
        catalog.upsert(rec(i, "done"), engine=engine)
    catalog.upsert(rec(1, "failed"), engine=engine)
    assert catalog.get("d1", engine=engine).status == "failed"
    assert catalog.get("nope", engine=engine) is None
    assert len(catalog.list(10, 0, engine=engine)) == 3
    assert [r.doc_id for r in catalog.list(10, 0, status="failed", engine=engine)] == [
        "d1"
    ]
    assert len(catalog.list(2, 2, engine=engine)) == 1


def _worker(path, k, n):
    eng = init_db(path)
    for i in range(n):
        audit_log.append("shared", f"w{k}", f"e{i}", {"k": k, "i": i}, engine=eng)
    eng.dispose()


def test_concurrent_appends_across_processes(tmp_path):
    import multiprocessing as mp

    path = tmp_path / "c.db"
    eng = init_db(path)
    procs, n = 4, 30
    ctx = mp.get_context("spawn")
    ps = [ctx.Process(target=_worker, args=(path, k, n)) for k in range(procs)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(60)
        assert p.exitcode == 0
    es = audit_log.entries("shared", engine=eng)
    assert len(es) == procs * n
    assert audit_log.verify_chain(es).ok
    eng.dispose()
