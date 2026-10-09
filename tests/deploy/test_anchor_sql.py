"""Static checks of deploy/anchor/mailroom_anchor.sql (no database is involved)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mailroom_reloaded.storage.anchor import SupabaseBackend

ROOT = Path(__file__).resolve().parents[2]
SQL_PATH = ROOT / "deploy" / "anchor" / "mailroom_anchor.sql"
ANCHOR_PY = ROOT / "src" / "mailroom_reloaded" / "storage" / "anchor.py"
WRITER = "mailroom_anchor_writer"


def _strip_comments(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


@pytest.fixture(scope="module")
def raw() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def sql(raw: str) -> str:
    """Lower-cased, comment-free, whitespace-collapsed statements."""
    return re.sub(r"\s+", " ", _strip_comments(raw)).lower()


def _statements(sql: str, keyword: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip().startswith(keyword)]


def test_names_match_client() -> None:
    table = SupabaseBackend.TABLE.rsplit("/", 1)[-1]
    text = ANCHOR_PY.read_text(encoding="utf-8")
    assert table == "mailroom_anchor"
    assert f'"{table}"' in text  # SqlBackend Table name
    assert 'Column("seq", BigInteger, primary_key=True, autoincrement=False)' in text
    assert 'Column("entry_hash", String, nullable=False)' in text
    assert (
        "select=seq,entry_hash" not in text
    )  # params are a dict; check the keys instead
    assert '"select": "seq,entry_hash"' in text
    assert '"order": "seq.desc"' in text


def test_table_definition(sql: str) -> None:
    m = re.search(r"create table if not exists public\.mailroom_anchor \((.*?)\);", sql)
    assert m, "table must be created idempotently"
    body = m.group(1)
    assert re.search(r"\bseq bigint primary key\b", body)
    assert re.search(r"\bentry_hash text not null\b", body)
    assert "anchored_at timestamptz not null default now()" in body
    assert "generated" not in body
    assert "serial" not in body


def test_monotonic_trigger_exists(sql: str) -> None:
    assert (
        "create or replace function public.mailroom_anchor_enforce_monotonic()" in sql
    )
    assert "language plpgsql" in sql
    assert re.search(
        r"create trigger \w+ before insert on public\.mailroom_anchor", sql
    )
    assert (
        "for each row execute function public.mailroom_anchor_enforce_monotonic()"
        in sql
    )
    assert "new.seq <= current_max" in sql
    assert "raise exception" in sql
    assert "drop trigger if exists" in sql  # idempotent


def test_rls_enabled_with_explicit_policies(sql: str) -> None:
    assert "alter table public.mailroom_anchor enable row level security" in sql
    policies = _statements(sql, "create policy")
    kinds = {re.search(r"\bfor (\w+)", p).group(1) for p in policies}  # type: ignore[union-attr]
    assert kinds == {"insert", "select"}
    for p in policies:
        assert f"to {WRITER}" in p
    assert sql.count("drop policy if exists") == len(policies)


def test_writer_grants_are_insert_and_select_only(sql: str) -> None:
    grants = [g for g in _statements(sql, "grant") if f"to {WRITER}" in g]
    table_grants = [g for g in grants if " on table " in g]
    assert len(table_grants) == 1
    privs = table_grants[0].split(" on table ")[0].removeprefix("grant ")
    assert {p.strip() for p in privs.split(",")} == {"select", "insert"}
    for g in grants:
        assert not re.search(
            r"\b(update|delete|truncate|references|trigger|all)\b", g
        ), g
    assert "grant usage on schema public to " + WRITER in sql
    # nothing is granted to anyone else
    for g in _statements(sql, "grant"):
        assert (
            f"to {WRITER}" in g
            or "to authenticator" in g
            or g.endswith(WRITER + " to authenticator")
        )


def test_no_grants_in_dollar_blocks_to_other_roles(sql: str) -> None:
    for role in ("anon", "authenticated", "service_role", "public"):
        assert not re.search(rf"grant [^;]* to {role}\b", sql)


def test_revokes_cover_public_and_supabase_roles(sql: str) -> None:
    assert "revoke all on table public.mailroom_anchor from public" in sql
    block = re.search(r"foreach r in array array\[(.*?)\]", sql)
    assert block, "revoke role list missing"
    roles = {x.strip().strip("'") for x in block.group(1).split(",")}
    assert {"anon", "authenticated", "service_role"} <= roles
    # each revoke is guarded by a role-exists check so plain Postgres works
    assert "if exists (select 1 from pg_roles where rolname = r)" in sql
    assert "revoke all on table public.mailroom_anchor from %i" in sql


def test_writer_role_is_nologin_and_secret_free(raw: str, sql: str) -> None:
    assert f"create role {WRITER} nologin" in sql
    assert f"rolname = '{WRITER}'" in sql
    # a password may only appear as a psql variable or in a comment
    assert not re.search(r"password\s+'", _strip_comments(raw), re.IGNORECASE)
    assert not re.search(r"eyJ[A-Za-z0-9_-]{10,}", raw)


def test_header_is_honest_and_warns_about_service_role(raw: str) -> None:
    head = raw[:1500].lower()
    assert "unverified" in head
    assert "staging" in raw.lower()
    assert "never be the service_role key" in raw.lower()
    assert "read the row back" in raw.lower() or "reading the row back" in raw.lower()
