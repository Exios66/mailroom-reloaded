"""External anchor: backends, push, external verification, key handling and the audit CLI."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import httpx
import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from mailroom_reloaded.cli import app
from mailroom_reloaded.settings import get_settings
from mailroom_reloaded.storage import anchor, db
from mailroom_reloaded.storage.anchor import (
    AnchorConfig,
    AnchorConflict,
    AnchorNotConfigured,
    AnchorUnreachable,
    Head,
    SqlBackend,
    SupabaseBackend,
)
from mailroom_reloaded.storage.db import init_db
from mailroom_reloaded.storage.ledger import Ledger, get_ledger, reset_ledger

SECRET = "sb-writer-SECRET-key-0123456789"
URL = "https://proj.example.test"
H1, H2 = "11" * 32, "22" * 32


# --------------------------------------------------------------------------- helpers
class FakeStore:
    """An in-memory PostgREST ``mailroom_anchor`` table behind ``httpx.MockTransport``."""

    def __init__(self) -> None:
        self.rows: dict[int, str] = {}
        self.requests: list[httpx.Request] = []
        self.fail: Exception | None = None
        self.post_status: int | None = None  # force a POST failure status
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        if request.method == "GET":
            q = request.url.params
            if "seq" in q:
                seq = int(q["seq"].removeprefix("eq."))
                body = [{"entry_hash": self.rows[seq]}] if seq in self.rows else []
            elif self.rows:
                top = max(self.rows)
                body = [{"seq": top, "entry_hash": self.rows[top]}]
            else:
                body = []
            return httpx.Response(200, json=body)
        if request.method == "POST":
            if self.post_status is not None:
                return httpx.Response(self.post_status, json={"message": "nope"})
            data = json.loads(request.content)
            if data["seq"] in self.rows:
                return httpx.Response(409, json={"message": "duplicate"})
            self.rows[data["seq"]] = data["entry_hash"]
            return httpx.Response(201)
        return httpx.Response(405)

    def backend(self) -> SupabaseBackend:
        return SupabaseBackend(URL, SECRET, transport=self.transport)


class MemBackend:
    """A trivial in-memory Backend."""

    def __init__(self) -> None:
        self.rows: dict[int, str] = {}
        self.pushes: list[tuple[int, str]] = []
        self.head_error: Exception | None = None
        self.push_error: Exception | None = None

    def head(self) -> Head | None:
        if self.head_error:
            raise self.head_error
        if not self.rows:
            return None
        top = max(self.rows)
        return Head(top, self.rows[top])

    def get(self, seq: int) -> str | None:
        return self.rows.get(seq)

    def push(self, seq: int, entry_hash: str) -> None:
        self.pushes.append((seq, entry_hash))
        if self.push_error:
            raise self.push_error
        self.rows[seq] = entry_hash


@pytest.fixture
def engine(tmp_path):
    eng = init_db(tmp_path / "mailroom.db")
    yield eng
    eng.dispose()


@pytest.fixture
def ledger(engine):
    lg = Ledger(engine)
    yield lg
    lg.close()


def _fill(lg: Ledger, n: int = 3) -> None:
    for i in range(n):
        assert lg.append(
            "run_opened", f"r{i}", payload={"kind": "eval", "mode": "pipeline"}
        )
    assert lg.flush()


def _hash(lg: Ledger, seq: int) -> str:
    return lg.entries(since_seq=seq - 1, limit=1)[0].entry_hash


def _set_env(monkeypatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(f"MAILROOM_{key.upper()}", value)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """No stray .env / host anchor settings."""
    monkeypatch.chdir(tmp_path)
    for var in list(os.environ):
        if var.startswith("MAILROOM_ANCHOR"):
            monkeypatch.delenv(var)
    get_settings.cache_clear()


# --------------------------------------------------------------------------- configuration
def test_default_is_not_configured() -> None:
    with pytest.raises(AnchorNotConfigured):
        anchor.get_config()


def test_blank_anchor_is_none(monkeypatch) -> None:
    _set_env(monkeypatch, anchor="  ")
    assert get_settings().anchor == "none"
    with pytest.raises(AnchorNotConfigured):
        anchor.get_config()


def test_unknown_backend_is_not_configured(monkeypatch) -> None:
    _set_env(monkeypatch, anchor="s3-bucket")
    with pytest.raises(AnchorNotConfigured, match="unknown"):
        anchor.get_config()


def test_export_needs_no_url_or_key(monkeypatch) -> None:
    _set_env(monkeypatch, anchor="export")
    cfg = anchor.get_config()
    assert cfg.backend == "export"
    with pytest.raises(AnchorNotConfigured):
        anchor.make_backend(cfg)


def test_supabase_requires_url_and_key(monkeypatch) -> None:
    _set_env(monkeypatch, anchor="supabase", anchor_key=SECRET)
    with pytest.raises(AnchorNotConfigured, match="URL"):
        anchor.get_config()
    _set_env(monkeypatch, anchor_url=URL)
    monkeypatch.delenv("MAILROOM_ANCHOR_KEY")
    get_settings.cache_clear()
    with pytest.raises(AnchorNotConfigured, match="KEY"):
        anchor.get_config()


def test_supabase_https_enforced_loopback_exempt(monkeypatch) -> None:
    _set_env(
        monkeypatch,
        anchor="supabase",
        anchor_key=SECRET,
        anchor_url="http://proj.example.test",
    )
    with pytest.raises(AnchorNotConfigured, match="https"):
        anchor.get_config()
    for ok in (
        "https://proj.example.test",
        "http://localhost:54321",
        "http://127.0.0.1:54321",
        "http://[::1]:54321",
    ):
        _set_env(monkeypatch, anchor_url=ok)
        assert anchor.get_config().url == ok
    # a look-alike host is not loopback
    _set_env(monkeypatch, anchor_url="http://localhost.evil.test")
    with pytest.raises(AnchorNotConfigured, match="https"):
        anchor.get_config()


def test_env_key_wins_over_file(monkeypatch, tmp_path) -> None:
    keyfile = tmp_path / "key"
    keyfile.write_text("file-key\n")
    keyfile.chmod(0o600)
    _set_env(
        monkeypatch,
        anchor="supabase",
        anchor_url=URL,
        anchor_key="env-key",
        anchor_key_file=str(keyfile),
    )
    assert anchor.get_config().key == "env-key"


def test_env_key_wins_even_over_unreadable_file(monkeypatch, tmp_path) -> None:
    _set_env(monkeypatch, anchor="supabase", anchor_url=URL, anchor_key="env-key",
             anchor_key_file=str(tmp_path / "missing"))  # fmt: skip
    assert anchor.get_config().key == "env-key"


def test_key_file_is_read_and_stripped(monkeypatch, tmp_path) -> None:
    keyfile = tmp_path / "key"
    keyfile.write_text(f"  {SECRET}\n\n")
    keyfile.chmod(0o600)
    _set_env(
        monkeypatch, anchor="supabase", anchor_url=URL, anchor_key_file=str(keyfile)
    )
    cfg = anchor.get_config()
    assert cfg.key == SECRET and cfg.key_file_world_readable is False


def test_key_file_world_readable_flagged(monkeypatch, tmp_path) -> None:
    keyfile = tmp_path / "key"
    keyfile.write_text(SECRET)
    keyfile.chmod(0o644)
    _set_env(
        monkeypatch, anchor="supabase", anchor_url=URL, anchor_key_file=str(keyfile)
    )
    assert anchor.get_config().key_file_world_readable is True


def test_key_file_oversized_fails_closed(monkeypatch, tmp_path) -> None:
    keyfile = tmp_path / "key"
    keyfile.write_text("k" * (anchor.KEY_FILE_MAX_BYTES + 1))
    _set_env(
        monkeypatch, anchor="supabase", anchor_url=URL, anchor_key_file=str(keyfile)
    )
    with pytest.raises(AnchorNotConfigured, match="too large"):
        anchor.get_config()


@pytest.mark.parametrize("kind", ["missing", "directory", "empty", "binary"])
def test_key_file_unusable_fails_closed(monkeypatch, tmp_path, kind) -> None:
    path = tmp_path / "key"
    if kind == "directory":
        path.mkdir()
    elif kind == "empty":
        path.write_text("  \n")
    elif kind == "binary":
        path.write_bytes(b"\xff\xfe\x00\x80")
    _set_env(monkeypatch, anchor="supabase", anchor_url=URL, anchor_key_file=str(path))
    with pytest.raises(AnchorNotConfigured):
        anchor.get_config()


def test_unreadable_key_file_permission_denied(monkeypatch, tmp_path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    keyfile = tmp_path / "key"
    keyfile.write_text(SECRET)
    keyfile.chmod(0)
    _set_env(
        monkeypatch, anchor="supabase", anchor_url=URL, anchor_key_file=str(keyfile)
    )
    with pytest.raises(AnchorNotConfigured, match="unreadable") as err:
        anchor.get_config()
    assert SECRET not in str(err.value)


def test_config_repr_and_redact_hide_key() -> None:
    cfg = AnchorConfig("supabase", URL, SECRET)
    assert SECRET not in repr(cfg) and SECRET not in str(cfg)
    assert "***" in repr(cfg)
    assert cfg.redact(f"boom {SECRET} boom") == "boom *** boom"
    assert "key=None" in repr(AnchorConfig("export"))


def test_key_env_present(monkeypatch) -> None:
    assert anchor.key_env_present() is False
    _set_env(monkeypatch, anchor_key=SECRET)
    assert anchor.key_env_present() is True


# --------------------------------------------------------------------------- SupabaseBackend
def test_supabase_head_empty_and_populated() -> None:
    store = FakeStore()
    be = store.backend()
    assert be.head() is None
    store.rows.update({1: H1, 7: H2})
    assert be.head() == Head(7, H2)
    req = store.requests[-1]
    assert req.method == "GET" and req.url.path == "/rest/v1/mailroom_anchor"
    assert req.url.params["order"] == "seq.desc" and req.url.params["limit"] == "1"
    be.close()


def test_supabase_get() -> None:
    store = FakeStore()
    store.rows[4] = H1
    be = store.backend()
    assert be.get(4) == H1
    assert be.get(5) is None
    assert store.requests[0].url.params["seq"] == "eq.4"


def test_supabase_sends_auth_headers_and_https() -> None:
    store = FakeStore()
    store.backend().head()
    req = store.requests[0]
    assert req.headers["apikey"] == SECRET
    assert req.headers["Authorization"] == f"Bearer {SECRET}"
    assert req.url.scheme == "https"


def test_supabase_push_is_post_return_minimal() -> None:
    store = FakeStore()
    store.backend().push(3, H1)
    assert store.rows == {3: H1}
    (req,) = store.requests
    assert req.method == "POST"
    assert req.headers["Prefer"] == "return=minimal"
    assert json.loads(req.content) == {"seq": 3, "entry_hash": H1}


def test_supabase_never_patches_or_deletes() -> None:
    store = FakeStore()
    be = store.backend()
    be.head()
    be.get(1)
    be.push(1, H1)
    be.push(1, H1)  # duplicate
    with pytest.raises(AnchorConflict):
        be.push(1, H2)
    store.post_status = 500
    with pytest.raises(AnchorUnreachable):
        be.push(2, H2)
    assert {r.method for r in store.requests} <= {"GET", "POST"}


def test_supabase_duplicate_same_hash_ok_via_readback() -> None:
    store = FakeStore()
    store.rows[5] = H1
    store.backend().push(5, H1)
    assert [r.method for r in store.requests] == ["POST", "GET"]


def test_supabase_duplicate_different_hash_conflicts() -> None:
    store = FakeStore()
    store.rows[5] = H1
    with pytest.raises(AnchorConflict):
        store.backend().push(5, H2)
    assert store.rows == {5: H1}


def test_supabase_rejected_insert_without_row_is_unreachable() -> None:
    store = FakeStore()
    store.post_status = 403
    with pytest.raises(AnchorUnreachable, match="403"):
        store.backend().push(5, H1)


def test_supabase_lost_response_with_row_stored_is_ok() -> None:
    store = FakeStore()
    store.rows[5] = H1
    store.post_status = 502
    store.backend().push(5, H1)


@pytest.mark.parametrize("exc", [httpx.ConnectError("x"), httpx.ReadTimeout("x")])
def test_supabase_transport_errors_are_unreachable(exc) -> None:
    store = FakeStore()
    store.fail = exc
    be = store.backend()
    for call in (be.head, lambda: be.get(1), lambda: be.push(1, H1)):
        with pytest.raises(AnchorUnreachable):
            call()


def test_supabase_bad_responses_are_unreachable() -> None:
    def respond(status: int, content: bytes):
        transport = httpx.MockTransport(
            lambda r: httpx.Response(status, content=content)
        )
        return SupabaseBackend(URL, SECRET, transport=transport)

    with pytest.raises(AnchorUnreachable, match="500"):
        respond(500, b"[]").head()
    with pytest.raises(AnchorUnreachable, match="invalid JSON"):
        respond(200, b"<html>").head()
    with pytest.raises(AnchorUnreachable, match="unexpected"):
        respond(200, b'{"a": 1}').head()


def test_supabase_error_text_has_no_key() -> None:
    store = FakeStore()
    store.fail = httpx.ConnectError(f"cannot reach {SECRET}")
    with pytest.raises(AnchorUnreachable) as err:
        store.backend().head()
    assert SECRET not in str(err.value) and SECRET not in repr(err.value)


# --------------------------------------------------------------------------- SqlBackend
@pytest.fixture
def sql_backend(tmp_path):
    be = SqlBackend(f"sqlite:///{tmp_path / 'anchor.db'}")
    yield be
    be.close()


def test_sql_backend_roundtrip(sql_backend) -> None:
    assert sql_backend.head() is None and sql_backend.get(1) is None
    sql_backend.push(1, H1)
    sql_backend.push(4, H2)
    assert sql_backend.head() == Head(4, H2)
    assert sql_backend.get(1) == H1 and sql_backend.get(2) is None


def test_sql_backend_duplicate_same_hash_ok_different_conflicts(sql_backend) -> None:
    sql_backend.push(2, H1)
    sql_backend.push(2, H1)
    with pytest.raises(AnchorConflict):
        sql_backend.push(2, H2)
    assert sql_backend.get(2) == H1


def test_sql_backend_rejects_lower_seq(sql_backend) -> None:
    sql_backend.push(5, H1)
    with pytest.raises(AnchorConflict, match="not greater"):
        sql_backend.push(3, H2)
    assert sql_backend.head() == Head(5, H1)


def test_sql_backend_lost_response_same_hash_is_success(
    sql_backend, monkeypatch
) -> None:
    """The insert commits but the client sees an error: a read-back of the same hash succeeds."""
    sql_backend.push(1, H1)
    real_head = sql_backend.head
    calls = {"n": 0}

    def head_then_stale():
        calls["n"] += 1
        return (
            None if calls["n"] == 1 else real_head()
        )  # looks empty, so an insert is attempted

    monkeypatch.setattr(sql_backend, "head", head_then_stale)
    sql_backend.push(
        1, H1
    )  # IntegrityError -> AnchorUnreachable -> read-back equal -> ok


def test_sql_backend_db_error_is_unreachable(sql_backend) -> None:
    with sql_backend._engine.begin() as conn:
        conn.execute(text("DROP TABLE mailroom_anchor"))
    with pytest.raises(AnchorUnreachable) as err:
        sql_backend.head()
    assert "OperationalError" in str(err.value)


def _psycopg_missing() -> bool:
    try:
        import psycopg  # noqa: F401
    except ModuleNotFoundError:
        return True
    return False


@pytest.mark.skipif(not _psycopg_missing(), reason="psycopg installed")
def test_postgres_without_psycopg_is_not_configured() -> None:
    with pytest.raises(AnchorNotConfigured, match="extra"):
        SqlBackend("postgresql://writer@127.0.0.1:1/db", "pw-SECRET-123")


@pytest.mark.skipif(_psycopg_missing(), reason="needs psycopg")
def test_dsn_password_is_redacted_in_repr_and_text() -> None:
    cfg = AnchorConfig(
        backend="postgres", url="postgresql://writer:dsn-SECRET-9@host/db"
    )
    assert "dsn-SECRET-9" not in repr(cfg)
    assert "dsn-SECRET-9" not in cfg.redact(
        "boom postgresql://writer:dsn-SECRET-9@host/db"
    )


@pytest.mark.skipif(_psycopg_missing(), reason="needs psycopg")
def test_sql_backend_error_text_has_no_password() -> None:
    be = SqlBackend("postgresql://writer@127.0.0.1:1/db", "pw-SECRET-123")
    try:
        with pytest.raises(AnchorUnreachable) as err:
            be.head()
        assert "pw-SECRET-123" not in str(err.value)
    finally:
        be.close()


# --------------------------------------------------------------------------- push_head
def test_push_empty_ledger(ledger) -> None:
    be = MemBackend()
    assert anchor.push_head(ledger, be).status == "empty"
    assert be.pushes == []


def test_push_ok_then_already(ledger) -> None:
    _fill(ledger)
    be = MemBackend()
    res = anchor.push_head(ledger, be)
    assert (res.status, res.seq, res.entry_hash) == ("pushed", 3, _hash(ledger, 3))
    assert anchor.push_head(ledger, be).status == "already"
    assert len(be.pushes) == 1


def test_push_advances_after_new_entries(ledger) -> None:
    _fill(ledger, 2)
    be = MemBackend()
    anchor.push_head(ledger, be)
    ledger.append("run_opened", "more", payload={"kind": "eval", "mode": "pipeline"})
    res = anchor.push_head(ledger, be)
    assert res.status == "pushed" and res.seq == 3
    assert sorted(be.rows) == [2, 3]


def test_push_duplicate_same_hash_ok_via_readback(ledger) -> None:
    _fill(ledger)
    store = FakeStore()
    head = ledger.head()

    # the store holds the head already but the head query is stale (empty): the POST 409s
    real = store.backend()
    store.rows[head.seq] = head.entry_hash

    class StaleHead:
        def head(self):
            return None

        get = staticmethod(real.get)
        push = staticmethod(real.push)

    res = anchor.push_head(ledger, StaleHead())
    assert res.status == "pushed"
    assert [r.method for r in store.requests] == ["POST", "GET"]


class _RacedBackend(MemBackend):
    """``head()`` is empty on the first call, then shows what a concurrent writer anchored."""

    def __init__(self, raced: dict[int, str]) -> None:
        super().__init__()
        self._raced = raced
        self._calls = 0

    def head(self) -> Head | None:
        self._calls += 1
        if self._calls > 1:
            self.rows.update(self._raced)
        return super().head()

    def push(self, seq: int, entry_hash: str) -> None:
        raise AnchorConflict("seq is already anchored")


def test_push_losing_a_race_to_the_same_head_is_already(ledger) -> None:
    _fill(ledger, 3)
    head = ledger.head()
    res = anchor.push_head(ledger, _RacedBackend({head.seq: head.entry_hash}))
    assert (res.status, res.seq, res.entry_hash) == (
        "already",
        head.seq,
        head.entry_hash,
    )


def test_push_losing_a_race_to_a_longer_prefix_head_is_already(ledger) -> None:
    _fill(ledger, 3)
    head = ledger.head()
    ledger.append("run_opened", "later", payload={"kind": "eval", "mode": "pipeline"})
    ledger.flush()
    newer = ledger.head()
    res = anchor.push_head(ledger, _RacedBackend({newer.seq: newer.entry_hash}))
    assert res.status == "already" and res.seq == newer.seq
    assert newer.seq > head.seq


def test_push_losing_a_race_to_a_different_hash_still_conflicts(ledger) -> None:
    _fill(ledger, 3)
    head = ledger.head()
    with pytest.raises(AnchorConflict):
        anchor.push_head(ledger, _RacedBackend({head.seq: H1}))


def test_push_duplicate_different_hash_conflicts(ledger) -> None:
    _fill(ledger)
    store = FakeStore()
    store.rows[3] = H1

    class StaleHead:
        def head(self):
            return None

        def get(self, seq):
            return store.backend().get(seq)

        def push(self, seq, h):
            store.backend().push(seq, h)

    with pytest.raises(AnchorConflict):
        anchor.push_head(ledger, StaleHead())
    assert store.rows == {3: H1}


def test_push_refuses_truncated_ledger(ledger) -> None:
    _fill(ledger, 2)
    be = MemBackend()
    be.rows[5] = H1
    with pytest.raises(AnchorConflict, match="TRUNCATED"):
        anchor.push_head(ledger, be)
    assert be.pushes == []


def test_push_refuses_rewritten_ledger(ledger) -> None:
    _fill(ledger, 3)
    be = MemBackend()
    be.rows[2] = H1  # contradicts the local entry 2
    with pytest.raises(AnchorConflict, match="REWRITTEN"):
        anchor.push_head(ledger, be)
    assert be.pushes == []


def test_push_refuses_same_seq_different_hash(ledger) -> None:
    _fill(ledger, 3)
    be = MemBackend()
    be.rows[3] = H1
    with pytest.raises(AnchorConflict, match="REWRITTEN"):
        anchor.push_head(ledger, be)
    assert be.rows == {3: H1}


# --------------------------------------------------------------------------- verify_external
def _cfg(**kw) -> AnchorConfig:
    return AnchorConfig("supabase", URL, SECRET, **kw)


def test_verify_empty_everything_ok(ledger) -> None:
    res = anchor.verify_external(ledger, _cfg(), MemBackend())
    assert (res.status, res.exit_code) == ("ok", 0)


def test_verify_ok_when_head_anchored(ledger) -> None:
    _fill(ledger)
    be = MemBackend()
    anchor.push_head(ledger, be)
    res = anchor.verify_external(ledger, _cfg(), be)
    assert (
        res.status,
        res.exit_code,
        res.anchored_seq,
        res.local_seq,
        res.unanchored,
    ) == ("ok", 0, 3, 3, 0)


def test_verify_unanchored_tail_is_ok_when_recent(ledger) -> None:
    _fill(ledger, 3)
    be = MemBackend()
    be.rows[1] = _hash(ledger, 1)
    res = anchor.verify_external(ledger, _cfg(), be)
    assert (res.status, res.exit_code, res.unanchored, res.anchored_seq) == (
        "ok",
        0,
        2,
        1,
    )


def test_verify_never_anchored_is_unanchored(ledger) -> None:
    _fill(ledger, 2)
    res = anchor.verify_external(ledger, _cfg(), MemBackend())
    assert (res.status, res.exit_code, res.unanchored, res.anchored_seq) == (
        "unanchored",
        0,
        2,
        None,
    )


def test_verify_truncated(ledger) -> None:
    _fill(ledger, 2)
    be = MemBackend()
    be.rows[9] = H1
    res = anchor.verify_external(ledger, _cfg(), be)
    assert (res.status, res.exit_code, res.anchored_seq, res.local_seq) == (
        "truncated",
        1,
        9,
        2,
    )


def test_verify_truncated_to_empty(ledger) -> None:
    be = MemBackend()
    be.rows[1] = H1
    res = anchor.verify_external(ledger, _cfg(), be)
    assert (res.status, res.exit_code, res.local_seq) == ("truncated", 1, 0)


def test_verify_rewritten(ledger) -> None:
    _fill(ledger, 3)
    be = MemBackend()
    be.rows[2] = H1
    res = anchor.verify_external(ledger, _cfg(), be)
    assert (res.status, res.exit_code) == ("rewritten", 1)


def test_verify_rewritten_at_head(ledger) -> None:
    _fill(ledger, 3)
    be = MemBackend()
    be.rows[3] = H1
    assert anchor.verify_external(ledger, _cfg(), be).status == "rewritten"


def test_verify_stale_after_24h(ledger) -> None:
    _fill(ledger, 3)
    be = MemBackend()
    be.rows[1] = _hash(ledger, 1)
    later = datetime.now(UTC) + timedelta(hours=25)
    res = anchor.verify_external(ledger, _cfg(), be, now=later)
    assert (res.status, res.exit_code, res.unanchored) == ("stale", 5, 2)
    assert "25 h" in res.detail


def test_verify_stale_when_never_anchored(ledger) -> None:
    _fill(ledger, 2)
    later = datetime.now(UTC) + timedelta(hours=25)
    res = anchor.verify_external(ledger, _cfg(), MemBackend(), now=later)
    assert (res.status, res.exit_code, res.anchored_seq) == ("stale", 5, None)


def test_verify_not_stale_just_under_24h(ledger) -> None:
    _fill(ledger, 3)
    be = MemBackend()
    be.rows[1] = _hash(ledger, 1)
    soon = datetime.now(UTC) + timedelta(hours=23)
    assert anchor.verify_external(ledger, _cfg(), be, now=soon).exit_code == 0


def test_verify_fully_anchored_never_stale(ledger) -> None:
    _fill(ledger, 3)
    be = MemBackend()
    anchor.push_head(ledger, be)
    later = datetime.now(UTC) + timedelta(days=30)
    assert anchor.verify_external(ledger, _cfg(), be, now=later).status == "ok"


def test_verify_unreachable(ledger) -> None:
    _fill(ledger)
    be = MemBackend()
    be.head_error = AnchorUnreachable("down")
    res = anchor.verify_external(ledger, _cfg(), be)
    assert (res.status, res.exit_code, res.detail) == ("unreachable", 3, "down")


def test_verify_key_file_warning_propagates(ledger) -> None:
    res = anchor.verify_external(
        ledger, _cfg(key_file_world_readable=True), MemBackend()
    )
    assert res.key_file_warning is True


def test_verify_against_supabase_mock(ledger) -> None:
    _fill(ledger, 3)
    store = FakeStore()
    anchor.push_head(ledger, store.backend())
    assert anchor.verify_external(ledger, _cfg(), store.backend()).status == "ok"
    store.fail = httpx.ConnectError("down")
    assert anchor.verify_external(ledger, _cfg(), store.backend()).exit_code == 3


def test_export_head(ledger) -> None:
    assert anchor.export_head(ledger) is None
    _fill(ledger, 2)
    rec = anchor.export_head(ledger)
    assert rec["seq"] == 2 and rec["entry_hash"] == _hash(ledger, 2)
    assert {"ts", "exported_at"} <= set(rec)


# --------------------------------------------------------------------------- background push
def test_push_with_retries_retries_then_gives_up(ledger, monkeypatch) -> None:
    _fill(ledger)
    be = MemBackend()
    be.push_error = AnchorUnreachable("down")
    sleeps: list[float] = []
    monkeypatch.setattr(anchor.time, "sleep", sleeps.append)
    monkeypatch.setattr(anchor, "make_backend", lambda cfg: be)
    monkeypatch.setattr(anchor, "get_config", lambda: _cfg())
    assert anchor._push_with_retries(ledger, delays=(0.0, 0.0)) is False
    assert len(be.pushes) == 2  # two real attempts
    assert sleeps == [0.0]  # sleeps only between attempts


def test_push_with_retries_succeeds_on_second_attempt(ledger, monkeypatch) -> None:
    _fill(ledger)
    be = MemBackend()
    outcomes = [AnchorUnreachable("blip"), None]

    def flaky_push(seq, h):
        err = outcomes.pop(0)
        if err:
            raise err
        be.rows[seq] = h

    be.push = flaky_push  # type: ignore[method-assign]
    monkeypatch.setattr(anchor.time, "sleep", lambda s: None)
    monkeypatch.setattr(anchor, "make_backend", lambda cfg: be)
    monkeypatch.setattr(anchor, "get_config", lambda: _cfg())
    assert anchor._push_with_retries(ledger, delays=(0.0, 0.0)) is True
    assert be.rows == {3: _hash(ledger, 3)}


def test_push_with_retries_conflict_not_retried(ledger, monkeypatch) -> None:
    _fill(ledger)
    be = MemBackend()
    be.push_error = AnchorConflict("tamper")
    sleeps: list[float] = []
    monkeypatch.setattr(anchor.time, "sleep", sleeps.append)
    monkeypatch.setattr(anchor, "make_backend", lambda cfg: be)
    monkeypatch.setattr(anchor, "get_config", lambda: _cfg())
    assert anchor._push_with_retries(ledger, delays=(0.0, 0.0, 0.0)) is False
    assert len(be.pushes) == 1 and sleeps == []


def test_push_with_retries_not_configured_never_raises(ledger, monkeypatch) -> None:
    monkeypatch.setattr(anchor.time, "sleep", lambda s: None)
    assert anchor._push_with_retries(ledger, delays=(0.0, 0.0)) is False


def test_push_failure_log_is_redacted(ledger, monkeypatch) -> None:
    _fill(ledger)
    logged: list[dict] = []
    monkeypatch.setattr(anchor.logger, "warning", lambda event, **kw: logged.append(kw))
    monkeypatch.setattr(anchor.time, "sleep", lambda s: None)
    monkeypatch.setattr(anchor, "get_config", lambda: _cfg())

    def boom(cfg):
        raise RuntimeError(f"auth failed for {SECRET}")

    monkeypatch.setattr(anchor, "make_backend", boom)
    assert anchor._push_with_retries(ledger, delays=(0.0, 0.0)) is False
    assert logged and all(SECRET not in str(kw) for kw in logged)


def test_schedule_push_noop_when_not_remote(ledger, monkeypatch) -> None:
    started: list[object] = []
    monkeypatch.setattr(anchor.threading, "Thread", lambda **kw: started.append(kw))
    anchor.schedule_push(ledger)  # anchor=none
    _set_env(monkeypatch, anchor="export")
    anchor.schedule_push(ledger)
    assert started == []


def test_schedule_push_never_raises(ledger, monkeypatch) -> None:
    def explode():
        raise RuntimeError("settings broke")

    monkeypatch.setattr(anchor, "_configured_backend", explode)
    anchor.schedule_push(ledger)


def test_schedule_push_starts_one_daemon_thread(ledger, monkeypatch) -> None:
    class FakeThread:
        instances: ClassVar[list] = []

        def __init__(self, **kw) -> None:
            self.kw = kw
            self.started = False
            FakeThread.instances.append(self)

        def start(self) -> None:
            self.started = True

        def is_alive(self) -> bool:
            return self.started

    monkeypatch.setattr(anchor.threading, "Thread", FakeThread)
    monkeypatch.setattr(anchor, "_worker", None)
    monkeypatch.setattr(anchor, "_ledger_ref", None)
    _set_env(monkeypatch, anchor="supabase")
    anchor.schedule_push(ledger)
    anchor.schedule_push(ledger)
    assert len(FakeThread.instances) == 1
    assert FakeThread.instances[0].kw["daemon"] is True
    assert anchor._ledger_ref is ledger
    anchor._wanted.clear()


def test_install_triggers_only_on_trigger_kinds(ledger, monkeypatch) -> None:
    calls: list[Ledger] = []
    monkeypatch.setattr(anchor, "schedule_push", calls.append)
    anchor.install(ledger)
    assert len(calls) == 1  # startup catch-up
    ledger.append("run_opened", "r", payload={"kind": "eval", "mode": "pipeline"})
    ledger.flush()
    assert len(calls) == 1
    ledger.append("run_closed", "r", payload={"closed_by": "completed", "expected": 0})
    ledger.flush()
    assert len(calls) == 2


def test_failing_push_never_blocks_or_raises_into_ledger(ledger, monkeypatch) -> None:
    """A scheduler that raises (or a backend that always fails) cannot break append/commit."""

    def explode(lg):
        raise RuntimeError(f"push blew up {SECRET}")

    monkeypatch.setattr(anchor, "schedule_push", explode)
    ledger.add_commit_hook(
        lambda kinds: anchor.schedule_push(ledger) if "run_closed" in kinds else None
    )
    assert (
        ledger.append(
            "run_closed", "r1", payload={"closed_by": "completed", "expected": 0}
        )
        is True
    )
    assert ledger.flush()
    assert (
        ledger.append("run_opened", "r2", payload={"kind": "eval", "mode": "pipeline"})
        is True
    )
    assert ledger.flush()
    assert [e.seq for e in ledger.entries()] == [1, 2]
    assert ledger.verify().ok


def test_failing_backend_via_real_hook_chain_does_not_block(
    ledger, monkeypatch
) -> None:
    be = MemBackend()
    be.push_error = AnchorUnreachable("down")
    monkeypatch.setattr(anchor.time, "sleep", lambda s: None)
    monkeypatch.setattr(anchor, "make_backend", lambda cfg: be)
    monkeypatch.setattr(anchor, "get_config", lambda: _cfg())
    # run the push inline in place of the daemon thread
    monkeypatch.setattr(
        anchor,
        "schedule_push",
        lambda lg: anchor._push_with_retries(lg, delays=(0.0, 0.0)),
    )
    anchor.install(ledger)
    assert ledger.append(
        "run_closed", "r1", payload={"closed_by": "completed", "expected": 0}
    )
    assert ledger.flush()
    assert ledger.verify().ok and ledger.head().seq == 1


# --------------------------------------------------------------------------- CLI
runner = CliRunner()


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """Default-engine ledger rooted at tmp_path, anchor push thread disabled, fake Supabase store."""
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    monkeypatch.setattr(db, "_default_engine", None)
    reset_ledger()
    monkeypatch.setattr(anchor, "schedule_push", lambda lg: None)
    store = FakeStore()
    real = anchor.SupabaseBackend
    monkeypatch.setattr(
        anchor,
        "SupabaseBackend",
        lambda url, key: real(url, key, transport=store.transport),
    )
    yield store
    reset_ledger()
    if db._default_engine is not None:
        db._default_engine.dispose()


def _supabase_env(monkeypatch, **extra: str) -> None:
    _set_env(monkeypatch, anchor="supabase", anchor_url=URL, anchor_key=SECRET, **extra)


def _seed(n: int = 3) -> Ledger:
    lg = get_ledger()
    _fill(lg, n)
    return lg


def _invoke(*args: str):
    return runner.invoke(app, ["audit", *args])


def test_cli_verify_chain_ok_without_anchor(cli) -> None:
    _seed()
    res = _invoke("verify")
    assert res.exit_code == 0 and "chain: ok (3 entries" in res.output


def test_cli_verify_unknown_run_is_a_usage_error_not_tamper(cli) -> None:
    _seed()
    res = _invoke("verify", "--run", "no-such-run")
    assert res.exit_code == 2 and "unknown run 'no-such-run'" in res.output


def test_cli_verify_empty_ledger_ok(cli) -> None:
    assert _invoke("verify").exit_code == 0


def test_cli_verify_ignores_anchor_without_external(cli, monkeypatch) -> None:
    _seed()
    _supabase_env(monkeypatch)
    cli.fail = httpx.ConnectError("down")
    assert _invoke("verify").exit_code == 0
    assert cli.requests == []


def test_cli_verify_chain_broken_exits_1(cli) -> None:
    lg = _seed()
    with lg.engine.begin() as conn:
        conn.execute(text("UPDATE ledger SET digest = 'bad' WHERE seq = 2"))
    res = _invoke("verify")
    assert res.exit_code == 1 and "chain: broken at 2" in res.output


def test_cli_verify_external_not_configured(cli) -> None:
    _seed()
    res = _invoke("verify", "--external")
    assert res.exit_code == 4 and "not configured" in res.output


def test_cli_verify_external_unknown_backend(cli, monkeypatch) -> None:
    _seed()
    _set_env(monkeypatch, anchor="carrier-pigeon")
    assert _invoke("verify", "--external").exit_code == 4
    assert _invoke("anchor").exit_code == 4


def test_cli_verify_external_export_mode_exits_4(cli, monkeypatch) -> None:
    _seed()
    _set_env(monkeypatch, anchor="export")
    res = _invoke("verify", "--external")
    assert res.exit_code == 4 and "export only" in res.output


def test_cli_anchor_export_mode_exits_4(cli, monkeypatch) -> None:
    _seed()
    _set_env(monkeypatch, anchor="export")
    assert _invoke("anchor").exit_code == 4


def test_cli_anchor_not_configured(cli) -> None:
    _seed()
    assert _invoke("anchor").exit_code == 4


def test_cli_anchor_then_verify_external_ok(cli, monkeypatch) -> None:
    lg = _seed()
    _supabase_env(monkeypatch)
    res = _invoke("anchor")
    assert res.exit_code == 0 and "anchor: pushed 3" in res.output
    assert cli.rows == {3: _hash(lg, 3)}
    # the push writes nothing to the chain: an idle ledger is fully anchored, never stale
    assert lg.head().seq == 3
    res = _invoke("verify", "--external")
    assert res.exit_code == 0 and "anchored 3, local 3, 0 unanchored" in res.output


def test_cli_anchor_already(cli, monkeypatch) -> None:
    lg = _seed()
    _supabase_env(monkeypatch)
    cli.rows[3] = _hash(lg, 3)
    res = _invoke("anchor")
    assert res.exit_code == 0 and "anchor: already 3" in res.output
    assert len(lg.entries()) == 3


def test_cli_anchor_empty_ledger(cli, monkeypatch) -> None:
    _supabase_env(monkeypatch)
    res = _invoke("anchor")
    assert res.exit_code == 0 and "ledger empty" in res.output
    assert cli.rows == {}


def test_cli_anchor_conflict_exits_1(cli, monkeypatch) -> None:
    _seed()
    _supabase_env(monkeypatch)
    cli.rows[2] = H1
    res = _invoke("anchor")
    assert res.exit_code == 1 and "REWRITTEN" in res.output
    assert cli.rows == {2: H1}


def test_cli_anchor_unreachable_exits_3(cli, monkeypatch) -> None:
    _seed()
    _supabase_env(monkeypatch)
    cli.fail = httpx.ConnectError(f"boom {SECRET}")
    res = _invoke("anchor")
    assert res.exit_code == 3 and "unreachable" in res.output


def test_cli_verify_external_truncated_exits_1(cli, monkeypatch) -> None:
    _seed(2)
    _supabase_env(monkeypatch)
    cli.rows[9] = H1
    res = _invoke("verify", "--external")
    assert res.exit_code == 1 and "TRUNCATED" in res.output


def test_cli_verify_external_rewritten_exits_1(cli, monkeypatch) -> None:
    _seed(3)
    _supabase_env(monkeypatch)
    cli.rows[2] = H1
    res = _invoke("verify", "--external")
    assert res.exit_code == 1 and "REWRITTEN" in res.output


def test_cli_verify_external_unreachable_exits_3(cli, monkeypatch) -> None:
    _seed()
    _supabase_env(monkeypatch)
    cli.fail = httpx.ReadTimeout("slow")
    res = _invoke("verify", "--external")
    assert res.exit_code == 3 and "UNREACHABLE" in res.output


def test_cli_verify_external_stale_exits_5(cli, monkeypatch) -> None:
    lg = _seed(3)
    _supabase_env(monkeypatch)
    cli.rows[1] = _hash(lg, 1)
    real = anchor.verify_external
    later = datetime.now(UTC) + timedelta(hours=25)
    monkeypatch.setattr(
        anchor, "verify_external", lambda l, c, b: real(l, c, b, now=later)
    )
    res = _invoke("verify", "--external")
    assert res.exit_code == 5 and "STALE" in res.output


def test_cli_verify_external_unanchored_exits_0(cli, monkeypatch) -> None:
    _seed()
    _supabase_env(monkeypatch)
    res = _invoke("verify", "--external")
    assert res.exit_code == 0 and "UNANCHORED" in res.output


def test_cli_verify_external_http_url_refused(cli, monkeypatch) -> None:
    _seed()
    _supabase_env(monkeypatch)
    _set_env(monkeypatch, anchor_url="http://proj.example.test")
    res = _invoke("verify", "--external")
    assert res.exit_code == 4 and "https" in res.output
    assert cli.requests == []


def test_cli_world_readable_key_file_warns(cli, monkeypatch, tmp_path) -> None:
    _seed()
    keyfile = tmp_path / "key"
    keyfile.write_text(SECRET)
    keyfile.chmod(0o644)
    _set_env(
        monkeypatch, anchor="supabase", anchor_url=URL, anchor_key_file=str(keyfile)
    )
    res = _invoke("verify", "--external")
    assert "world-readable" in res.output
    keyfile.chmod(0o600)
    assert "world-readable" not in _invoke("verify", "--external").output


def test_cli_bad_key_file_exits_4(cli, monkeypatch, tmp_path) -> None:
    _seed()
    keyfile = tmp_path / "key"
    keyfile.write_text("k" * (anchor.KEY_FILE_MAX_BYTES + 1))
    _set_env(
        monkeypatch, anchor="supabase", anchor_url=URL, anchor_key_file=str(keyfile)
    )
    assert _invoke("verify", "--external").exit_code == 4
    assert _invoke("anchor").exit_code == 4
    assert cli.requests == []


def test_cli_secret_never_in_output(cli, monkeypatch) -> None:
    lg = _seed()
    _supabase_env(monkeypatch)
    outputs = []
    cli.fail = httpx.ConnectError(f"cannot reach {SECRET}")
    outputs += [_invoke("anchor"), _invoke("verify", "--external")]
    cli.fail = None
    cli.rows[2] = H1
    outputs += [_invoke("anchor"), _invoke("verify", "--external")]
    cli.rows.clear()
    outputs += [
        _invoke("anchor"),
        _invoke("verify", "--external"),
        _invoke("export-head"),
    ]
    for res in outputs:
        assert SECRET not in res.output
        assert res.exception is None or isinstance(res.exception, SystemExit)
    assert {r.exit_code for r in outputs} >= {0, 1, 3}
    assert SECRET not in json.dumps([e.payload for e in lg.entries()], default=str)


def test_cli_export_head_stdout(cli) -> None:
    lg = _seed()
    res = _invoke("export-head")
    assert res.exit_code == 0
    rec = json.loads(res.output)
    assert rec["seq"] == 3 and rec["entry_hash"] == _hash(lg, 3)


def test_cli_export_head_out_file(cli, tmp_path) -> None:
    lg = _seed()
    for flag in ("--out", "-o"):
        target = tmp_path / f"head{flag.strip('-')}.json"
        res = _invoke("export-head", flag, str(target))
        assert res.exit_code == 0 and str(target) in res.output
        assert json.loads(target.read_text())["entry_hash"] == _hash(lg, 3)


def test_cli_export_head_empty(cli, tmp_path) -> None:
    target = tmp_path / "head.json"
    res = _invoke("export-head", "-o", str(target))
    assert res.exit_code == 0 and "ledger empty" in res.output
    assert not target.exists()


def test_cli_export_head_works_with_any_setting(cli, monkeypatch) -> None:
    _seed()
    _set_env(monkeypatch, anchor="export")
    assert _invoke("export-head").exit_code == 0
    _set_env(monkeypatch, anchor="bogus")
    assert _invoke("export-head").exit_code == 0


# --------------------------------------------------------------------------- review regressions
def _recorder(calls: list):
    return lambda *a, **kw: calls.append((a, kw))


def test_cli_idle_ledger_never_stale_after_anchor(cli, monkeypatch) -> None:
    lg = _seed()
    _supabase_env(monkeypatch)
    assert _invoke("anchor").exit_code == 0
    assert [e.kind for e in lg.entries()].count("anchor") == 0
    real = anchor.verify_external
    later = datetime.now(UTC) + timedelta(hours=25)
    monkeypatch.setattr(
        anchor, "verify_external", lambda l, c, b: real(l, c, b, now=later)
    )
    res = _invoke("verify", "--external")
    assert (
        res.exit_code == 0 and "anchor: OK" in res.output and "STALE" not in res.output
    )


def test_verify_external_idle_after_push_not_stale(ledger) -> None:
    _fill(ledger)
    be = MemBackend()
    anchor.push_head(ledger, be)
    later = datetime.now(UTC) + timedelta(hours=25)
    res = anchor.verify_external(ledger, _cfg(), be, now=later)
    assert (res.status, res.exit_code) == ("ok", 0)


@pytest.mark.parametrize(
    "dsn", ["postgresql://u:pw@h:bad/db", "notaurl SECRET"], ids=["badport", "nourl"]
)
def test_cli_bad_postgres_url_exits_4(cli, monkeypatch, dsn) -> None:
    _seed()
    _set_env(monkeypatch, anchor="postgres", anchor_url=dsn)
    for args in (("verify", "--external"), ("anchor",)):
        res = _invoke(*args)
        assert res.exit_code == 4, res.output
        assert "SECRET" not in res.output and ":pw@" not in res.output
        assert "Traceback" not in res.output


@pytest.mark.parametrize("bad", ["https://h:bad/", "https://ex\nample"])
def test_cli_malformed_supabase_url_exits_4(cli, monkeypatch, bad) -> None:
    _seed()
    _set_env(monkeypatch, anchor="supabase", anchor_key=SECRET, anchor_url=bad)
    for args in (("verify", "--external"), ("anchor",)):
        res = _invoke(*args)
        assert res.exit_code == 4, res.output
        assert SECRET not in res.output and "Traceback" not in res.output
    assert cli.requests == []


def test_cli_odd_but_parsable_supabase_url_is_never_tamper(cli, monkeypatch) -> None:
    """httpx accepts 'https://exa mple'; the failure surfaces at request time, not as exit 1."""
    _seed()
    _set_env(
        monkeypatch, anchor="supabase", anchor_key=SECRET, anchor_url="https://exa mple"
    )
    cli.fail = httpx.ConnectError("bad host")
    for args in (("verify", "--external"), ("anchor",)):
        res = _invoke(*args)
        assert res.exit_code in (3, 4), res.output
        assert SECRET not in res.output and "Traceback" not in res.output


def _rows_backend(body) -> SupabaseBackend:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    return SupabaseBackend(URL, SECRET, transport=transport)


@pytest.mark.parametrize(
    "body",
    [
        [{"entry_hash": H1}],
        [{"seq": 1}],
        [{"seq": "abc", "entry_hash": H1}],
        [{"seq": None, "entry_hash": H1}],
        [{"seq": [1], "entry_hash": H1}],
        [1],
        ["x"],
        [None],
    ],
)
def test_supabase_malformed_head_row_is_unreachable(body) -> None:
    with pytest.raises(AnchorUnreachable):
        _rows_backend(body).head()


@pytest.mark.parametrize("body", [[{"seq": 1}], [1], ["x"], [None]])
def test_supabase_malformed_get_row_is_unreachable(body) -> None:
    with pytest.raises(AnchorUnreachable):
        _rows_backend(body).get(1)


def test_cli_unexpected_verify_error_exits_3_without_leak(cli, monkeypatch) -> None:
    _seed()
    _supabase_env(monkeypatch)

    def boom(*a, **kw):
        raise RuntimeError(f"kaboom {SECRET}")

    monkeypatch.setattr(anchor, "verify_external", boom)
    res = _invoke("verify", "--external")
    assert res.exit_code == 3
    assert SECRET not in res.output and "Traceback" not in res.output


def test_cli_unexpected_push_error_exits_3_without_leak(cli, monkeypatch) -> None:
    _seed()
    _supabase_env(monkeypatch)

    def boom(*a, **kw):
        raise RuntimeError(f"kaboom {SECRET}")

    monkeypatch.setattr(anchor, "push_head", boom)
    res = _invoke("anchor")
    assert res.exit_code == 3
    assert SECRET not in res.output and "Traceback" not in res.output


def test_tail_naive_aware_mismatch_does_not_raise(ledger) -> None:
    _fill(ledger, 3)
    be = MemBackend()
    be.rows[1] = _hash(ledger, 1)
    naive = datetime.now() + timedelta(hours=1)  # noqa: DTZ005 - deliberately naive
    res = anchor.verify_external(ledger, _cfg(), be, now=naive)
    assert res.exit_code == 0 and res.unanchored == 2
    assert (
        anchor.verify_external(ledger, _cfg(), MemBackend(), now=naive).exit_code == 0
    )


def test_config_repr_and_redact_hide_percent_encoded_dsn_password() -> None:
    dsn = "postgresql://u:p%40ss@h/db"
    cfg = AnchorConfig("postgres", dsn)
    assert "p%40ss" not in repr(cfg) and "p@ss" not in repr(cfg)
    assert "p%40ss" not in cfg.redact(f"boom {dsn}")
    assert "p@ss" not in cfg.redact("boom postgresql://u:p@ss@h/db")


def test_redact_short_password_does_not_mangle_other_text() -> None:
    cfg = AnchorConfig("postgres", "postgresql://u:pw@h/db")
    out = cfg.redact("pwned by postgresql://u:pw@h/db")
    assert out.startswith("pwned by ") and ":pw@" not in out and "***ned" not in out
    assert "pw" not in repr(cfg).replace("postgres", "")


def test_settings_repr_hides_anchor_secrets(monkeypatch) -> None:
    _set_env(
        monkeypatch,
        anchor="postgres",
        anchor_key="KEY-SECRET-777",
        anchor_url="postgresql://u:dsn-pass-42@h/db",
    )
    shown = repr(get_settings()) + str(get_settings())
    assert "KEY-SECRET-777" not in shown and "dsn-pass-42" not in shown


def test_push_with_retries_config_error_log_is_redacted(ledger, monkeypatch) -> None:
    _fill(ledger)
    _set_env(
        monkeypatch,
        anchor="supabase",
        anchor_key=SECRET,
        anchor_url="postgresql://u:dsn-pass-42@h/db",
    )
    logged: list[dict] = []
    monkeypatch.setattr(anchor.logger, "warning", lambda event, **kw: logged.append(kw))
    monkeypatch.setattr(anchor.time, "sleep", lambda s: None)

    def boom():
        raise RuntimeError(f"cfg failed {SECRET} postgresql://u:dsn-pass-42@h/db")

    monkeypatch.setattr(anchor, "get_config", boom)
    assert anchor._push_with_retries(ledger, delays=(0.0, 0.0)) is False
    assert len(logged) == 2
    blob = str(logged)
    assert SECRET not in blob and "dsn-pass-42" not in blob and "***" in blob


def _config_in_thread() -> tuple[bool, object]:
    import threading

    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["cfg"] = anchor.get_config()
        except BaseException as exc:  # noqa: BLE001
            box["exc"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(5)
    return (not t.is_alive()), box.get("exc", box.get("cfg"))


def test_key_file_fifo_fails_closed_promptly(monkeypatch, tmp_path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("no mkfifo")
    fifo = tmp_path / "key"
    os.mkfifo(fifo)
    _set_env(monkeypatch, anchor="supabase", anchor_url=URL, anchor_key_file=str(fifo))
    finished, result = _config_in_thread()
    if not finished:  # unblock the leaked thread before failing
        fd = os.open(fifo, os.O_RDWR)
        os.close(fd)
    assert finished, "reading a FIFO key file hung"
    assert isinstance(result, AnchorNotConfigured)


def test_key_file_symlink_is_followed(monkeypatch, tmp_path) -> None:
    real = tmp_path / "real-key"
    real.write_text(SECRET + "\n")
    real.chmod(0o600)
    link = tmp_path / "key"
    link.symlink_to(real)
    _set_env(monkeypatch, anchor="supabase", anchor_url=URL, anchor_key_file=str(link))
    assert anchor.get_config().key == SECRET


def test_key_file_device_is_rejected(monkeypatch) -> None:
    if not os.path.exists("/dev/zero"):
        pytest.skip("no /dev/zero")
    _set_env(
        monkeypatch, anchor="supabase", anchor_url=URL, anchor_key_file="/dev/zero"
    )
    finished, result = _config_in_thread()
    assert finished
    assert isinstance(result, AnchorNotConfigured)
    assert "regular" in str(result)


@pytest.fixture
def captured_engine(monkeypatch):
    from unittest.mock import MagicMock

    import sqlalchemy

    seen: dict[str, object] = {}

    def fake_create_engine(url, **kw):
        seen["url"] = url
        seen["connect_args"] = kw.get("connect_args", {})
        return MagicMock()

    monkeypatch.setattr(sqlalchemy, "create_engine", fake_create_engine)
    return seen


def test_postgres_remote_host_verifies_the_certificate_by_default(
    captured_engine,
) -> None:
    SqlBackend("postgresql://u:pw@db.example.test/db")
    args = captured_engine["connect_args"]
    assert args["sslmode"] == "verify-full" and "connect_timeout" in args


def test_postgres_remote_host_without_password_also_requires_tls(
    captured_engine,
) -> None:
    SqlBackend("postgres://u@db.example.test:5432/db", "pw-from-key")
    assert captured_engine["connect_args"]["sslmode"] == "verify-full"


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u@db.example.test/db?sslmode=disable",
        "postgresql://u@db.example.test/db?sslmode=verify-full",
        "postgresql://u@127.0.0.1/db",
        "postgresql://u@localhost:5432/db",
        "postgresql://u@[::1]/db",
    ],
)
def test_postgres_tls_not_forced_when_chosen_or_loopback(captured_engine, dsn) -> None:
    SqlBackend(dsn)
    assert "sslmode" not in captured_engine["connect_args"]
    assert "connect_timeout" in captured_engine["connect_args"]


def test_sqlite_gets_no_postgres_connect_args(captured_engine, tmp_path) -> None:
    be = SqlBackend(f"sqlite:///{tmp_path / 'a.db'}")
    assert captured_engine["connect_args"] == {}
    del be


def test_supabase_trigger_rejection_with_higher_head_is_conflict() -> None:
    store = FakeStore()
    store.rows[7] = H1
    store.post_status = 403  # the trigger refuses a non-increasing seq
    be = store.backend()
    with pytest.raises(AnchorConflict, match="ahead"):
        be.push(5, H2)
    with pytest.raises(AnchorConflict):
        be.push(7, H2)  # equal seq, different hash: get(7) is H1
    assert store.rows == {7: H1}


def test_supabase_trigger_rejection_head_at_seq_without_row_is_conflict() -> None:
    """head >= seq but get(seq) is None (gap below the head)."""
    store = FakeStore()
    store.rows[9] = H1
    store.post_status = 400
    with pytest.raises(AnchorConflict):
        store.backend().push(4, H2)


def test_supabase_rejection_with_lower_head_stays_unreachable() -> None:
    store = FakeStore()
    store.rows[2] = H1
    store.post_status = 403
    with pytest.raises(AnchorUnreachable, match="403"):
        store.backend().push(5, H2)


def _readonly_probe(monkeypatch):
    installs: list = []
    schedules: list = []
    threads: list = []
    monkeypatch.setattr(anchor, "install", _recorder(installs))
    monkeypatch.setattr(anchor, "schedule_push", _recorder(schedules))
    monkeypatch.setattr(anchor.threading, "Thread", lambda **kw: threads.append(kw))
    return installs, schedules, threads


def test_cli_read_only_commands_never_install_anchor(cli, monkeypatch) -> None:
    import threading

    _seed()
    _supabase_env(monkeypatch)
    cli.rows[3] = _hash(get_ledger(anchor=False), 3)
    installs, schedules, threads = _readonly_probe(monkeypatch)
    for args in (
        ("verify",),
        ("export-head",),
        ("verify", "--external"),
        ("anchor",),
    ):
        reset_ledger()  # force a fresh ledger so an install would be visible
        res = _invoke(*args)
        assert res.exit_code == 0, (args, res.output)
    assert installs == [] and schedules == [] and threads == []
    assert not [t for t in threading.enumerate() if t.name == "ledger-anchor"]


def test_get_ledger_default_installs_hook_once(cli, monkeypatch) -> None:
    installs: list = []
    monkeypatch.setattr(anchor, "install", _recorder(installs))
    reset_ledger()
    lg = get_ledger()
    assert get_ledger() is lg
    assert len(installs) == 1 and installs[0][0] == (lg,)


def test_get_ledger_anchor_false_skips_hook(cli, monkeypatch) -> None:
    installs: list = []
    monkeypatch.setattr(anchor, "install", _recorder(installs))
    reset_ledger()
    get_ledger(anchor=False)
    assert installs == []
