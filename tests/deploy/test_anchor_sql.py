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
    assert "drop trigger if exists mailroom_anchor_monotonic" in sql
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


def test_writer_grants_are_column_level_insert_and_table_select(sql: str) -> None:
    grants = [g for g in _statements(sql, "grant") if f"to {WRITER}" in g]
    table_grants = [g for g in grants if " on table " in g]
    assert len(table_grants) == 2
    assert f"grant select on table public.mailroom_anchor to {WRITER}" in table_grants
    assert (
        "grant insert (seq, entry_hash) on table public.mailroom_anchor to " + WRITER
        in table_grants
    )
    # no table-level INSERT (anchored_at must not be writable)
    assert not any(re.match(r"grant [^(]*\binsert\b[^(]* on table", g) for g in grants)
    assert "anchored_at" not in "".join(table_grants)
    for g in grants:
        assert not re.search(
            r"\b(update|delete|truncate|references|trigger|all)\b", g
        ), g
    assert "grant usage on schema public to " + WRITER in sql
    # re-run resets: table-level revoke (also clears column grants) precedes the grants
    assert sql.index(
        f"revoke all on table public.mailroom_anchor from {WRITER}"
    ) < sql.index("grant insert (seq, entry_hash)")
    # nothing is granted to anyone else
    for g in _statements(sql, "grant"):
        assert (
            f"to {WRITER}" in g
            or "to authenticator" in g
            or g.endswith(WRITER + " to authenticator")
        )


def test_constraints(sql: str) -> None:
    assert "check (seq > 0 and seq < 9007199254740992)" in sql
    assert "check (entry_hash ~ '^[0-9a-f]{64}$')" in sql
    assert sql.count("drop constraint if exists") == 2  # idempotent re-add


def test_trigger_function_hardening(sql: str) -> None:
    fn = re.search(
        r"create or replace function public\.mailroom_anchor_enforce_monotonic\(\).*?\$\$;",
        sql,
    )
    assert fn
    body = fn.group(0)
    assert "security definer" in body
    assert "set search_path = pg_catalog, pg_temp" in body
    assert "pg_catalog.pg_advisory_xact_lock" in body
    assert "from public.mailroom_anchor" in body
    assert (
        "current_max is not null and new.seq - current_max > 1000000" in body
    )  # max step, not on the first anchor
    assert "raise exception" in body
    # execute revoked from PUBLIC and the Supabase roles, guarded by role-exists
    assert (
        "revoke all on function public.mailroom_anchor_enforce_monotonic() from public"
        in sql
    )
    assert "revoke execute on function %s from %i" in sql
    assert "if exists (select 1 from pg_roles where rolname = r)" in sql
    assert "'public.mailroom_anchor_enforce_monotonic()'" in sql
    assert "'public.mailroom_anchor_forbid_change()'" in sql
    roles = re.search(
        r"foreach r in array array\[('anon'.*?)\] loop if exists \(select 1 from pg_roles "
        r"where rolname = r\) then foreach f",
        sql,
    )
    assert roles
    assert {"anon", "authenticated", "service_role"} <= set(
        re.findall(r"'(\w+)'", roles.group(1))
    )


def test_update_delete_truncate_guard_triggers(sql: str) -> None:
    guard = re.search(
        r"create or replace function public\.mailroom_anchor_forbid_change\(\).*?\$\$;",
        sql,
    )
    assert guard
    assert "raise exception" in guard.group(0)
    assert "set search_path = pg_catalog, pg_temp" in guard.group(0)
    assert re.search(
        r"create trigger \w+ before update or delete on public\.mailroom_anchor "
        r"for each row execute function public\.mailroom_anchor_forbid_change\(\)",
        sql,
    )
    assert re.search(
        r"create trigger \w+ before truncate on public\.mailroom_anchor "
        r"for each statement execute function public\.mailroom_anchor_forbid_change\(\)",
        sql,
    )
    assert sql.count("drop trigger if exists") == 3


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
