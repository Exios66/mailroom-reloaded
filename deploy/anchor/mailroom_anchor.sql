-- mailroom_anchor.sql: external anchor store for the Mailroom archive ledger head.
--
-- STATUS: UNVERIFIED. This script has been reviewed and statically tested only
-- (tests/deploy/test_anchor_sql.py parses the text). It has NOT been run against a live
-- Postgres or Supabase project. Run it on a staging project first and exercise it
-- (insert, non-monotonic insert, update, delete, anon/service_role access, PostgREST
-- calls with the writer JWT) before trusting it as a tamper-evidence control.
--
-- What it creates (idempotent; safe to re-run, runs on plain Postgres and on Supabase):
--   * table  public.mailroom_anchor (seq bigint primary key, entry_hash text not null,
--     anchored_at timestamptz default now()). The client sends only {seq, entry_hash} and
--     never reads anchored_at.
--   * CHECK constraints: 0 < seq < 2^53 and entry_hash is 64 lower-case hex characters.
--   * a BEFORE INSERT trigger requiring seq to be strictly greater than the current max
--     and at most 1000000 above it (SECURITY DEFINER, search_path = pg_catalog, pg_temp).
--   * BEFORE UPDATE OR DELETE (row) and BEFORE TRUNCATE (statement) triggers that always
--     raise. Defence in depth: the table owner or a superuser can still drop/disable them.
--   * role   mailroom_anchor_writer with USAGE on the schema, SELECT on the table and
--     INSERT on the columns (seq, entry_hash) only (it cannot set anchored_at; no UPDATE,
--     DELETE, TRUNCATE, REFERENCES or TRIGGER). EXECUTE on the trigger functions is revoked
--     from PUBLIC, anon, authenticated and service_role.
--   * REVOKE ALL on the table from PUBLIC, anon, authenticated and service_role
--     (each guarded by a role-exists check).
--   * row level security enabled, with explicit insert and select policies for the writer.
--
-- Usage (as the database owner / postgres role):
--   psql "$ADMIN_DSN" -f deploy/anchor/mailroom_anchor.sql
--
-- Credentials. This file contains no secrets. The role is created NOLOGIN.
--   * Plain Postgres (MAILROOM_ANCHOR=postgres): give it a password out of band, e.g.
--       psql "$ADMIN_DSN" -v anchor_pw="$(read -rs p; echo "$p")" \
--            -c "ALTER ROLE mailroom_anchor_writer LOGIN PASSWORD :'anchor_pw'"
--     and put that password in MAILROOM_ANCHOR_KEY (SqlBackend uses it as the password).
--   * Supabase (MAILROOM_ANCHOR=supabase): the key given to MAILROOM_ANCHOR_KEY MUST be a
--     JWT whose "role" claim is mailroom_anchor_writer, signed with the project's JWT
--     secret. It must NEVER be the service_role key (which bypasses RLS and could rewrite
--     the table) and never the anon key. The block below grants the role to
--     "authenticator" so PostgREST can switch to it.
--
-- Duplicates. Re-pushing an already-anchored seq fails (primary key or trigger). That is
-- intended: the client treats a failed insert as success only after reading the row back
-- (select=entry_hash&seq=eq.N) and finding the same hash. The server does not do upsert.
--
-- Limits. The table owner and any superuser (on Supabase: the postgres role and the
-- dashboard) can still change or delete rows; guard those credentials separately. A holder
-- of the writer credential can append anchors, only monotonic ones, never edit old ones.

-- ---------------------------------------------------------------- table
create table if not exists public.mailroom_anchor (
    seq         bigint      primary key,
    entry_hash  text        not null,
    anchored_at timestamptz not null default now()
);

comment on table public.mailroom_anchor is
    'Insert-only external anchor of the Mailroom ledger head (seq, entry_hash).';

-- ---------------------------------------------------------------- writer role
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'mailroom_anchor_writer') then
        create role mailroom_anchor_writer nologin;
    end if;
    -- Supabase: let PostgREST switch to the writer role named in the JWT "role" claim.
    if exists (select 1 from pg_roles where rolname = 'authenticator') then
        grant mailroom_anchor_writer to authenticator;
    end if;
end
$$;

-- ---------------------------------------------------------------- constraints
-- Bounds keep a writer-key holder from bricking anchoring (e.g. seq = 2^63-1) and keep
-- seq exactly representable in JSON/JavaScript clients (< 2^53).
alter table public.mailroom_anchor drop constraint if exists mailroom_anchor_seq_range;
alter table public.mailroom_anchor
    add constraint mailroom_anchor_seq_range
    check (seq > 0 and seq < 9007199254740992);

alter table public.mailroom_anchor drop constraint if exists mailroom_anchor_hash_format;
alter table public.mailroom_anchor
    add constraint mailroom_anchor_hash_format
    check (entry_hash ~ '^[0-9a-f]{64}$');

-- ---------------------------------------------------------------- monotonic trigger
-- SECURITY DEFINER so the max(seq) check sees every row whatever the caller's RLS policy,
-- and so the advisory lock serialises concurrent inserters (otherwise two transactions
-- could each see the same max and both commit; the primary key stops equal seq only).
-- search_path is pinned to pg_catalog, pg_temp and every reference is schema-qualified.
-- Ledger seqs are small increasing integers; gaps are normal (only some heads are
-- anchored), but a single jump larger than 1000000 is refused.
create or replace function public.mailroom_anchor_enforce_monotonic()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, pg_temp
as $$
declare
    current_max bigint;
begin
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext('public.mailroom_anchor'));
    select pg_catalog.max(a.seq) into current_max from public.mailroom_anchor as a;
    if current_max is not null and new.seq <= current_max then
        raise exception 'mailroom_anchor: seq % is not greater than the anchored head %',
            new.seq, current_max
            using errcode = 'check_violation';
    end if;
    if new.seq - coalesce(current_max, 0) > 1000000 then
        raise exception 'mailroom_anchor: seq % jumps more than 1000000 past the anchored head %',
            new.seq, coalesce(current_max, 0)
            using errcode = 'check_violation';
    end if;
    return new;
end
$$;

-- Defence in depth: refuse UPDATE, DELETE and TRUNCATE even on paths that bypass
-- privileges/RLS (non-owner roles). The table owner or a superuser can still DROP these
-- triggers (or disable them); guard those credentials separately.
create or replace function public.mailroom_anchor_forbid_change()
returns trigger
language plpgsql
set search_path = pg_catalog, pg_temp
as $$
begin
    raise exception 'mailroom_anchor is insert-only: % is not allowed', tg_op
        using errcode = 'insufficient_privilege';
end
$$;

-- Trigger functions need no EXECUTE for the inserting role; do not expose them for direct calls.
revoke all on function public.mailroom_anchor_enforce_monotonic() from public;
revoke all on function public.mailroom_anchor_forbid_change() from public;

do $$
declare
    r text;
    f text;
begin
    foreach r in array array['anon', 'authenticated', 'service_role']
    loop
        if exists (select 1 from pg_roles where rolname = r) then
            foreach f in array array[
                'public.mailroom_anchor_enforce_monotonic()',
                'public.mailroom_anchor_forbid_change()'
            ]
            loop
                execute format('revoke execute on function %s from %I', f, r);
            end loop;
        end if;
    end loop;
end
$$;

drop trigger if exists mailroom_anchor_monotonic on public.mailroom_anchor;
create trigger mailroom_anchor_monotonic
    before insert on public.mailroom_anchor
    for each row execute function public.mailroom_anchor_enforce_monotonic();

drop trigger if exists mailroom_anchor_no_update_delete on public.mailroom_anchor;
create trigger mailroom_anchor_no_update_delete
    before update or delete on public.mailroom_anchor
    for each row execute function public.mailroom_anchor_forbid_change();

drop trigger if exists mailroom_anchor_no_truncate on public.mailroom_anchor;
create trigger mailroom_anchor_no_truncate
    before truncate on public.mailroom_anchor
    for each statement execute function public.mailroom_anchor_forbid_change();

-- ---------------------------------------------------------------- privileges
revoke all on table public.mailroom_anchor from public;

do $$
declare
    r text;
begin
    -- Supabase default privileges hand new public tables to these roles; plain Postgres
    -- has none of them, hence the existence checks.
    foreach r in array array['anon', 'authenticated', 'service_role']
    loop
        if exists (select 1 from pg_roles where rolname = r) then
            execute format('revoke all on table public.mailroom_anchor from %I', r);
        end if;
    end loop;
end
$$;

-- Reset the writer: revoking at table level also removes any column-level grants, so a
-- re-run drops anything granted by hand. Then SELECT on the table and INSERT on
-- (seq, entry_hash) only, so the writer can never set anchored_at.
revoke all on table public.mailroom_anchor from mailroom_anchor_writer;
grant usage on schema public to mailroom_anchor_writer;
grant select on table public.mailroom_anchor to mailroom_anchor_writer;
grant insert (seq, entry_hash) on table public.mailroom_anchor to mailroom_anchor_writer;

-- ---------------------------------------------------------------- row level security
alter table public.mailroom_anchor enable row level security;

drop policy if exists mailroom_anchor_writer_insert on public.mailroom_anchor;
create policy mailroom_anchor_writer_insert
    on public.mailroom_anchor
    for insert
    to mailroom_anchor_writer
    with check (true);

drop policy if exists mailroom_anchor_writer_select on public.mailroom_anchor;
create policy mailroom_anchor_writer_select
    on public.mailroom_anchor
    for select
    to mailroom_anchor_writer
    using (true);

-- No UPDATE or DELETE policy exists (and no privilege), so those are denied for everyone
-- but the table owner and superusers.
