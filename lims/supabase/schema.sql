-- LIMS cloud schema (run in the Supabase SQL editor).
-- Idempotent: safe to re-run on an existing project (IF NOT EXISTS / ON CONFLICT /
-- DROP POLICY IF EXISTS / guarded publication DO blocks).
-- Contract: ARCHITECTURE.md section 11.

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

create table if not exists public.device_types (
    id text primary key
);

insert into public.device_types (id) values ('gcms'), ('lcms'), ('icpms')
on conflict (id) do nothing;

create table if not exists public.registrations (
    id          uuid primary key default gen_random_uuid(),
    reg_no      text unique not null,
    device_type text not null references public.device_types (id),
    product     text default '',
    status      text not null default 'open' check (status in ('open', 'closed')),
    created_by  uuid default auth.uid(),
    created_at  timestamptz default now(),
    updated_at  timestamptz default now()
);

create table if not exists public.registration_tests (
    id              uuid primary key default gen_random_uuid(),
    registration_id uuid references public.registrations (id) on delete cascade,
    test_name       text not null,
    unique (registration_id, test_name)
);

create table if not exists public.documents (
    id              uuid primary key default gen_random_uuid(),
    registration_id uuid references public.registrations (id),
    reg_no          text not null,
    test_name       text not null,
    device_name     text not null,
    device_type     text not null,
    docname         text,
    storage_path    text not null,
    size            bigint,
    sha256          text,
    printed_by      text,
    job_id          text,
    encrypted       boolean default false,
    -- Passphrase for AES-256 encrypted PDFs, forwarded by the hub so testers can
    -- open the document. Readable by authenticated users only (RLS below).
    pdf_password    text,
    received_at     timestamptz default now()
);

-- Idempotent upgrade for projects created before pdf_password existed.
alter table public.documents add column if not exists pdf_password text;

-- storage_path is the hub's idempotency key: it is fixed at enqueue (with a uuid
-- prefix, so distinct prints never collide) and reused across forward retries.
-- The UNIQUE index lets the hub UPSERT (POST ...?on_conflict=storage_path with
-- Prefer: resolution=merge-duplicates), so an at-least-once retry after a
-- post-commit failure updates the same row instead of creating a duplicate.
create unique index if not exists documents_storage_path_key
    on public.documents (storage_path);

-- ---------------------------------------------------------------------------
-- updated_at maintenance
-- The hub polls a SINGLE watermark (max(registrations.updated_at)), so any
-- change to a registration's tests must bump the parent row too.
-- ---------------------------------------------------------------------------

create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists trg_registrations_touch on public.registrations;
create trigger trg_registrations_touch
    before update on public.registrations
    for each row execute function public.touch_updated_at();

create or replace function public.bump_parent_registration()
returns trigger
language plpgsql
as $$
begin
    -- On cascade-delete the parent is already gone; the UPDATE matches 0 rows,
    -- which is fine (the parent's own DELETE moved the watermark logic anyway).
    update public.registrations
       set updated_at = now()
     where id = coalesce(new.registration_id, old.registration_id);
    return coalesce(new, old);
end;
$$;

drop trigger if exists trg_registration_tests_bump on public.registration_tests;
create trigger trg_registration_tests_bump
    after insert or update or delete on public.registration_tests
    for each row execute function public.bump_parent_registration();

-- ---------------------------------------------------------------------------
-- catalog_version(): the hub polls this (~2 s) and refetches only on change.
-- ---------------------------------------------------------------------------

create or replace function public.catalog_version()
returns timestamptz
language sql
stable
security definer
set search_path = public
as $$
    select coalesce(max(updated_at), 'epoch'::timestamptz) from public.registrations;
$$;

revoke all on function public.catalog_version() from public;
grant execute on function public.catalog_version() to service_role, authenticated;

-- ---------------------------------------------------------------------------
-- Row Level Security
-- authenticated (testers via the web app): read everything; create/update
-- registrations and their tests; delete tests. documents has NO client
-- insert policy: only the hub writes rows, via the service role (bypasses RLS).
-- ---------------------------------------------------------------------------

alter table public.device_types       enable row level security;
alter table public.registrations      enable row level security;
alter table public.registration_tests enable row level security;
alter table public.documents          enable row level security;

grant usage on schema public to authenticated;
grant select on public.device_types to authenticated;
grant select, insert, update on public.registrations to authenticated;
grant select, insert, update, delete on public.registration_tests to authenticated;
grant select on public.documents to authenticated;

drop policy if exists "device_types read" on public.device_types;
create policy "device_types read" on public.device_types
    for select to authenticated using (true);

drop policy if exists "registrations read" on public.registrations;
create policy "registrations read" on public.registrations
    for select to authenticated using (true);

drop policy if exists "registrations insert" on public.registrations;
create policy "registrations insert" on public.registrations
    for insert to authenticated with check (true);

drop policy if exists "registrations update" on public.registrations;
create policy "registrations update" on public.registrations
    for update to authenticated using (true) with check (true);

drop policy if exists "registration_tests read" on public.registration_tests;
create policy "registration_tests read" on public.registration_tests
    for select to authenticated using (true);

drop policy if exists "registration_tests insert" on public.registration_tests;
create policy "registration_tests insert" on public.registration_tests
    for insert to authenticated with check (true);

drop policy if exists "registration_tests update" on public.registration_tests;
create policy "registration_tests update" on public.registration_tests
    for update to authenticated using (true) with check (true);

drop policy if exists "registration_tests delete" on public.registration_tests;
create policy "registration_tests delete" on public.registration_tests
    for delete to authenticated using (true);

drop policy if exists "documents read" on public.documents;
create policy "documents read" on public.documents
    for select to authenticated using (true);

-- ---------------------------------------------------------------------------
-- Realtime: the web app subscribes to postgres_changes on all four tables.
-- ALTER PUBLICATION ... ADD TABLE errors if the table is already a member,
-- so each add is guarded.
-- ---------------------------------------------------------------------------

do $$
begin
    begin
        alter publication supabase_realtime add table public.device_types;
    exception when duplicate_object then null;
    end;
    begin
        alter publication supabase_realtime add table public.registrations;
    exception when duplicate_object then null;
    end;
    begin
        alter publication supabase_realtime add table public.registration_tests;
    exception when duplicate_object then null;
    end;
    begin
        alter publication supabase_realtime add table public.documents;
    exception when duplicate_object then null;
    end;
end;
$$;

-- ---------------------------------------------------------------------------
-- Storage: private bucket for the forwarded PDFs. The hub uploads with the
-- service role (bypasses RLS); the web app only needs SELECT so that
-- createSignedUrl() works for authenticated testers.
-- ---------------------------------------------------------------------------

insert into storage.buckets (id, name, public)
values ('lims-docs', 'lims-docs', false)
on conflict (id) do nothing;

drop policy if exists "lims-docs authenticated read" on storage.objects;
create policy "lims-docs authenticated read" on storage.objects
    for select to authenticated using (bucket_id = 'lims-docs');
