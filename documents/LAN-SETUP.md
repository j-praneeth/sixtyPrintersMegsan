# LAN → LIMS deployment guide

This is the runbook for the full pipeline:

```
60 client PCs (NO internet, LAN only)
   Print ▶ pick Registration ▶ pick Test ▶ PDF POSTed to the hub
        │
        ▼
Central desktop  192.168.1.172  (has internet, hosts the limsDocs share)
   hub\  (port 8000) — files PDFs into  limsDocs\<device>\<type>\<reg>\<test>\
                     — HOLDS jobs with no/invalid registration (dashboard assigns)
                     — syncs the registration catalog from Supabase (~2 s)
                     — forwards every filed PDF to the cloud LIMS instantly
        │
        ▼
Cloud LIMS (Supabase + React, lims\) — testers create registrations
   (reg no + device type + tests); printed documents appear live.
```

Registration numbers are **never typed by hand** at print time: testers create
them in the LIMS web app, the hub caches them, and the print dialog offers them
as dropdowns filtered by the printer's device type. A job whose registration or
test is missing/unknown is **held** at the hub — never lost, never forwarded —
until an operator assigns it in the hub dashboard.

---

## Order of operations

### 1. Cloud LIMS (once)

1. Create a project at <https://supabase.com> (any region).
2. SQL editor → paste and run **`lims/supabase/schema.sql`** (idempotent — safe
   to re-run). This creates `device_types` (seeded `gcms`/`lcms`/`icpms`),
   `registrations`, `registration_tests`, `documents`, RLS policies, realtime,
   and the private `lims-docs` storage bucket.
   - Need more device types? `insert into device_types (id) values ('xrf');`
3. Authentication → Providers → enable **Email**; Users → create your tester
   accounts (email + password).
4. Project Settings → API: note the **Project URL**, the **anon** key (web app)
   and the **service_role** key (hub ONLY — never put it in the web app).
5. Web app:
   ```bat
   cd lims\web
   copy .env.example .env     &REM fill VITE_SUPABASE_URL + VITE_SUPABASE_ANON_KEY
   npm install
   npm run dev                &REM or: npm run build → host dist\ anywhere
   ```
   Sign in as a tester → **Registrations** → create e.g. `R-2026-0001`, device
   type `gcms`, tests `rx-test`, `ry-test`. See [lims/README.md](lims/README.md).

### 2. Central hub (the 192.168.1.172 desktop, once)

1. Make sure the shared folder exists (e.g. `C:\limsDocs`, shared as `limsDocs`
   so clients see `\\192.168.1.172\limsDocs`) and every client account can READ it.
2. Start the hub:
   ```bat
   cd hub
   run.bat
   ```
   The console prints two secrets — record both:
   - **ADMIN TOKEN** — paste into the dashboard (`http://192.168.1.172:8000/`).
   - **ENROLL KEY** — needed once per client printer install.
3. Dashboard → **Settings**: set the limsDocs directory to its **local** path
   (`C:\limsDocs`), the **Supabase URL** and the **service_role key** (stored
   DPAPI-encrypted on disk), then Save. The Catalog tab should show your
   registrations within a couple of seconds ("Synced from Supabase every 2 s").
   - No Supabase yet? Leave it blank — the hub runs in **local mode** and you can
     manage the catalog in the dashboard by hand; forwarding starts the moment
     Supabase is configured (the backlog drains automatically).
4. Allow inbound TCP 8000 in Windows Firewall so the clients can reach it, and
   keep `run.bat` running (or register it as a scheduled task / service at boot).

### 3. Each client PC (60×)

Copy this repo to the PC and double-click **`install.bat`** (UAC). A
click-through **wizard** opens (paste works in every box; use "Test hub & load
device types" to check the enroll key before continuing). Fill in:

| Field | What to enter |
|---|---|
| Printer name | what users pick in the print dialog, e.g. `GCMS Printer` |
| Hub base URL | `http://192.168.1.172:8000` (the default) |
| Device name | **unique per machine/instrument**, e.g. `gcms-01` — the hub rejects duplicates (409) |
| Enroll key | the ENROLL KEY from the hub console |
| Device type | `gcms` / `lcms` / `icpms` / … (the list is fetched from the hub) |
| PDF encryption password | per-printer AES-256 password (blank = off) |
| Catalog share folder | accept the default `\\192.168.1.172\limsDocs\.vcp\catalog` |

Scripted rollout (no prompts):

```bat
powershell -ExecutionPolicy Bypass -File setup.ps1 -Action install ^
  -PrinterName "GCMS Printer" -HubUrl http://192.168.1.172:8000 ^
  -DeviceName gcms-01 -DeviceType gcms -EnrollKey <KEY> -Password <PDF-PW> ^
  -CatalogShareDir \\192.168.1.172\limsDocs\.vcp\catalog
```

`add-printer.bat` adds more printers later (same prompts). Passing `-Url`
instead of a hub URL keeps the old direct-URL behavior (no hub, no dropdowns).

### 4. Daily flow

1. Tester creates the registration (+tests) in the LIMS web app.
2. Within ~2 s it is on the hub and in every print dialog on the LAN.
3. Lab user prints from any app → dialog appears → picks the **registration**
   (searchable, filtered to this printer's device type) → picks the **test** →
   *Attach & print*.
4. The PDF lands in `limsDocs\<device>\<type>\<reg>\<test>\` **and** appears in
   the LIMS **Documents** page seconds later (realtime).

### 5. Held documents (registration required — hold if missing)

A job is held (hub dashboard → **Held**) when the user pressed *Skip*, the
dialog timed out, or the registration/test didn't validate
(`missing_registration`, `unknown_registration`, `device_type_mismatch`,
`missing_test`, `unknown_test`). Held PDFs live under `hub\data\held\` — not in
the share, not in LIMS. Pick the correct registration + test in the Held tab →
**Assign** → the file moves into the tree and forwards to LIMS.

---

## Password management (per printer)

| Tool | What it does |
|---|---|
| `change-password.bat` | change/disable ONE printer's AES-256 PDF password (super-admin gated; stored DPAPI-encrypted, never plaintext) |
| `view-password.bat` | show a printer's password if forgotten — requires the **super-admin password** |
| `set-super-admin.bat` | rotate the super-admin password |

The factory super-admin password is distributed to deployment admins out-of-band
(it appears nowhere in this repo — only its PBKDF2-SHA256 hash is embedded in
`setup.ps1`) — **rotate it with `set-super-admin.bat` right after installing**.
It is stored only as a PBKDF2-SHA256 hash; printer passwords are stored as
machine-bound DPAPI blobs.
Encrypted PDFs open with the printer's password (`decode-pdf.bat` or any reader).
If encryption is on but the password/qpdf is missing, jobs fail **closed** into
`failed\` — never sent in the clear.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Print dialog shows "Catalog unavailable" | Hub unreachable from the client AND share fallback unreadable. Check the hub is running, firewall port 8000, and that the user can open `\\192.168.1.172\limsDocs\.vcp\catalog\<type>.json`. The typed number is validated by the hub (held if wrong) so work never stops. |
| Dropdown empty for this printer | No **open** registrations of that device type — create one in LIMS (or check the printer was enrolled with the right type: `status.bat`). |
| Enrollment fails with 409 | Device name already used — pick a unique one. |
| Enrollment fails with 401 | Wrong enroll key (get it from the hub console or dashboard → Devices → Show enroll key). |
| Job never reached the hub | Client-side problem: run `fix-queue.bat`, read `%ProgramData%\VirtualCloudPrinter\log.txt`; unposted PDFs are preserved in `failed\`. |
| Document is "held" | Expected when no/invalid registration — assign it in the hub dashboard (Held tab). |
| Document "forward_failed" | Hub couldn't reach Supabase (internet/keys). It retries forever with backoff; check the error text in the Documents tab and Settings. |
| Nothing in the LIMS Documents page | Check the hub dashboard first: if docs are `filed`/`forwarded` there, check the web app's Supabase project/keys; if `held`, assign them. |
| Clients can't read the catalog share | Share/NTFS permissions on `limsDocs` — clients need READ on `.vcp\catalog`. |

Client deep-dive diagnostics: `fix-queue.bat` → `vcp-diagnostics.txt` (see
[README.md](README.md) troubleshooting). Hub logs: the `run.bat` console.

## Security model (summary)

- Every printer gets its **own ingest token** (in the URL + `X-Device-Token`);
  the hub's device record is authoritative for device name/type.
- **Enroll key** gates device registration; **admin token** gates the dashboard
  and all admin APIs. Both are random, generated on first hub start.
- The Supabase **service_role key never leaves the central desktop** (stored
  DPAPI-encrypted); the web app uses the anon key + row-level security.
- Client-side passwords: DPAPI (machine scope) at rest; super-admin is a
  PBKDF2-SHA256 hash; `view-password` requires super-admin + Administrator.
- The hub sanitizes every path segment built from client input (device, reg,
  test, docname) — path traversal cannot escape `limsDocs`.
- All CLAUDE.md client invariants (locked `%ProgramData%` ACLs, SYSTEM-only
  config, fail-closed encryption, never-lose-a-job) are unchanged.
