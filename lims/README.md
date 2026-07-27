# LIMS cloud tier (Supabase + React)

This folder is the cloud side of the print pipeline (ARCHITECTURE.md §11):

- `supabase/schema.sql` — database schema, RLS, realtime, storage bucket.
  Run it in the Supabase SQL editor (idempotent: safe to re-run).
- `web/` — the tester-facing React app (Vite). Testers create registrations
  (reg no, device type, tests) and watch forwarded documents appear live.

The **hub** (on the central desktop) is the only component that writes
documents to Supabase, using the **service role key**. The web app uses the
**anon key** + RLS only.

## Setup, step by step

### 1. Create the Supabase project

1. Go to <https://supabase.com>, sign in, **New project**.
2. Pick any name/region, set a strong database password (you won't need it
   day-to-day), and wait for the project to provision.

### 2. Run the schema

1. In the project, open **SQL Editor** → **New query**.
2. Paste the entire contents of `lims/supabase/schema.sql` and **Run**.
3. It should finish with "Success". Re-running it later (e.g. after a schema
   update) is safe — everything is `IF NOT EXISTS` / guarded.

This creates the tables (`device_types`, `registrations`, `registration_tests`,
`documents`), RLS policies, realtime publication entries, and the private
`lims-docs` storage bucket.

### 3. Enable email auth and create a tester user

1. **Authentication → Sign In / Providers**: make sure **Email** is enabled.
   (Optional, convenient for a closed lab group: turn **Confirm email** off so
   users you create can sign in immediately.)
2. **Authentication → Users → Add user → Create new user**: enter the tester's
   email and a password. Repeat per tester. There is no self-signup in the app.

### 4. Get the keys

**Project Settings → API**:

- **Project URL** — e.g. `https://abcd1234.supabase.co`
- **anon / public key** — for the web app (safe in the browser; RLS applies)
- **service_role key** — for the hub ONLY. This key bypasses RLS.
  **Never put it in `lims/web`, never commit it, never send it to client PCs.**
  It belongs on one machine: the central desktop running the hub.

### 5. Configure and run the web app

```bash
cd lims/web
cp .env.example .env      # then edit .env:
#   VITE_SUPABASE_URL=https://abcd1234.supabase.co
#   VITE_SUPABASE_ANON_KEY=<anon key>
npm install
npm run dev               # http://localhost:5173
```

Sign in with the tester user from step 3. Create a registration (device type,
tests as chips), and it becomes available to the hub's catalog within ~2 s.

For a production build: `npm run build` (output in `dist/`, host it on any
static host — the app only talks to Supabase).

### 6. Point the hub at Supabase

On the **central desktop**, open the hub dashboard → **Settings** and enter:

- **Supabase URL** — the Project URL from step 4
- **Service role key** — the `service_role` key from step 4

The hub stores the key DPAPI-encrypted on that machine. Once set, the hub
starts polling the registration catalog (via `catalog_version()`) and
forwarding every filed PDF to the `lims-docs` bucket + a `documents` row —
which then appears live on the web app's Documents page.

> Reminder: the service key never leaves the central desktop. The web app and
> the client PCs only ever see the anon key (web) or the hub URL (clients).

## Day-to-day

- **Registrations page** — create registrations, add/remove tests, close a
  registration when work is done (closed registrations disappear from the
  printers' dropdowns), reopen if needed. Updates propagate to the hub and
  from there to the client fallback catalog files.
- **Documents page** — every print filed by the hub appears here in real time:
  registration, test, device, document name, size, who printed it, and an
  `encrypted` badge for AES-256-protected PDFs. **Download** opens a short-lived
  (5 min) signed URL; encrypted PDFs still need their password to open.
