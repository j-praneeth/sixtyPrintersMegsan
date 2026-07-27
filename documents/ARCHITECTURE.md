# LIMS Print Pipeline — Architecture & Interface Contracts

This document is the **single source of truth** for the LAN print-to-LIMS pipeline.
Every interface below is a hard contract: if you change one side, change the other
in the same commit. It extends (does not replace) the invariants in `CLAUDE.md`.

## 1. Topology

```
[60 client PCs - NO internet, LAN only]
   App -Print-> virtual printer (v3 PS driver) -> mfilemon -> upload.py (SYSTEM)
      |  1. GET  hub /catalog?device_type=X   (X-Device-Token)  -> catalog JSON
      |  2. show prompt (user session): Registration dropdown -> Test dropdown
      |  3. PS->PDF (Ghostscript), optional AES-256 (qpdf, per-printer password)
      |  4. POST hub /ingest/<token>  (multipart: file + metadata)
      v
[Central desktop 192.168.1.172 - has internet, hosts limsDocs share]
   hub/ (FastAPI, port 8000)
      - enrolls devices (unique device_name), issues per-printer tokens
      - files PDFs into  limsDocs\<device_name>\<device_type>\<reg_no>\<test>\
      - HOLDS jobs with missing/unknown registration or test (dashboard assigns)
      - syncs registration catalog FROM Supabase (poll, ~2 s watermark)
      - exports catalog to  limsDocs\.vcp\catalog\*.json  (client fallback)
      - forwards every filed PDF TO Supabase (Storage + documents row, retry queue)
      v
[Cloud LIMS - Supabase + React]
   lims/supabase/schema.sql   tables + RLS + realtime
   lims/web/                  testers create registrations (reg_no, device type,
                              tests); documents appear live (realtime)
```

- Print jobs ONLY are forwarded (manual drops into limsDocs are ignored by the hub).
- Registration number is REQUIRED: a job without a valid one is **held** at the hub
  (never lost, never forwarded) until assigned in the hub dashboard.

## 2. Repo layout

```
setup.ps1, upload.py, prompt-id.ps1, *.bat     client toolkit (Windows, stdlib-only python)
hub/app.py, hub/requirements.txt, hub/run.bat  central hub (FastAPI; third-party deps OK)
lims/supabase/schema.sql                       cloud DB schema (run in Supabase SQL editor)
lims/web/                                      React (Vite) app for testers
simulator/                                     legacy dev simulator (unchanged)
```

## 3. Client `config.json` schema (v2)

Written by `setup.ps1` (BOM-less UTF-8, as before). Read by `upload.py` (`utf-8-sig`).

```json
{
  "hub": { "base_url": "http://192.168.1.172:8000", "verify_tls": true },
  "catalog_share_dir": "\\\\192.168.1.172\\limsDocs\\.vcp\\catalog",

  "printers": {
    "GCMS Printer": {
      "url": "http://192.168.1.172:8000/ingest/<token>",
      "token": "<token>",
      "device_name": "gcms-01",
      "device_type": "gcms",
      "docname_field": "docname",
      "file_field": "file",
      "extra_fields": {},
      "headers": {},
      "verify_tls": true,
      "pdf_encryption": { "enabled": true, "password_dpapi": "<base64 DPAPI blob>" }
    }
  },

  "super_admin": {
    "algo": "pbkdf2-sha256",
    "iterations": 200000,
    "salt": "<base64 16 bytes>",
    "hash": "<base64 32 bytes>"
  },

  "prompt_registration": true,
  "prompt_timeout_seconds": 180,
  "registration_field": "registration_number",
  "default_url": "", "docname_field": "docname", "file_field": "file",
  "extra_fields": {}, "headers": {}, "verify_tls": true,
  "pdf_encryption": { "enabled": false, "password": "" },
  "timeout_seconds": 60, "retry_count": 2, "retry_delay_seconds": 3,
  "ghostscript_path": "", "qpdf_path": ""
}
```

Rules:
- A printer entry **with** `device_name`+`token` is a "hub printer" (catalog prompt,
  metadata fields). An entry without them is a **legacy printer** and behaves exactly
  as before (free-text prompt, plain POST). Never break legacy entries.
- Per-printer `pdf_encryption` overrides the global one. Password resolution order in
  `upload.py`: printer `password_dpapi` -> global `password_dpapi` (both DPAPI-decrypt,
  a failure RAISES = fail closed) -> printer `password` -> `VCP_PDF_PASSWORD` machine
  env var -> global `password` (both plain, discouraged). Both `set-password.bat`
  (global) and `change-password.bat` (per-printer) store `password_dpapi`.
  Fail-closed rule unchanged: encryption enabled + no resolvable password => raise.

## 4. DPAPI contract (per-printer passwords)

- Scope: **LocalMachine** (SYSTEM must decrypt; the spooler-launched upload.py is SYSTEM).
- Optional entropy: UTF-8 bytes of the literal string `VCP-DPAPI-v1` (exact, no BOM).
- Stored as base64 of the protected blob in `password_dpapi`.
- PowerShell side (`setup.ps1`): `Add-Type -AssemblyName System.Security;`
  `[Security.Cryptography.ProtectedData]::Protect($pwBytes, $entropyBytes, 'LocalMachine')`.
- Python side (`upload.py`): ctypes `crypt32.CryptUnprotectData` with the same entropy
  (`DATA_BLOB` in/out, `LocalFree` the output). stdlib-only (ctypes is stdlib).

## 5. Super-admin password

- Stored ONLY as PBKDF2-SHA256 (200,000 iterations, 16-byte random salt) in
  `config.json` `super_admin`. Seeded at install/upgrade, if the key is absent,
  with a precomputed factory PBKDF2 record embedded in `setup.ps1`
  (`$InitialSuperAdminHash`) — the factory plaintext appears nowhere in the repo
  and is distributed to deployment admins out-of-band.
- Verified by `setup.ps1` (`Rfc2898DeriveBytes` with `[Security.Cryptography.HashAlgorithmName]::SHA256`)
  for actions: `viewpassword`, `changepassword`, `setsuperadmin`.
- `-Action setsuperadmin` rotates it (asks current, then new twice).
- Never log or echo passwords. `view-password.bat` shows a printer's decrypted
  password ONLY after super-admin verification (and it runs elevated, since
  config.json is SYSTEM+Admins).

## 6. Catalog contract

### 6a. HTTP (primary; used by upload.py as SYSTEM)
`GET <hub>/catalog?device_type=<type>` with header `X-Device-Token: <printer token>`.
200 response:

```json
{
  "version": "2026-07-18T10:11:12.131415+00:00",
  "device_type": "gcms",
  "registrations": [
    { "reg_no": "R-2025-0001", "product": "Sample A", "status": "open",
      "tests": ["rx-test", "ry-test"] }
  ]
}
```

Only `status = "open"` registrations are returned. 401 on bad token.

### 6b. Share files (fallback; readable by the interactive user)
The hub atomically writes, on every catalog change:
- `<limsDocs>\.vcp\catalog\<device_type>.json` — same JSON schema as 6a
- `<limsDocs>\.vcp\catalog\all.json` — `{version, device_types:[...], registrations:[...]}`
  where each registration also carries `"device_type"`.

Atomic = write to `*.tmp` in the same directory then `os.replace`.

### 6c. Client-side flow
1. `upload.py` (SYSTEM) tries 6a with a **5 s** timeout; on success writes the JSON to
   `ids\<user>.<job>.catalog.json` (BOM-less UTF-8) so the user-session prompt can read it.
2. Launches `prompt-id.ps1` (see §7). If the catalog fetch failed, it still launches
   the prompt with `-FallbackDir <catalog_share_dir>` so the prompt (running AS THE
   USER, who can reach the share) loads `<FallbackDir>\<device_type>.json` itself.
3. The `.catalog.json` temp file is deleted by `upload.py` after the prompt returns.

## 7. Prompt contract (`prompt-id.ps1`)

Launched by `upload.py` via CreateProcessAsUser (mechanism unchanged):

```
powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File prompt-id.ps1
    -OutFile <ids\user.job.prompt> -DocName <doc> -DeviceType <type>
    [-CatalogFile <ids\user.job.catalog.json>] [-FallbackDir <\\host\limsDocs\.vcp\catalog>]
```

- Hub printers: two dropdowns — **Registration number** (from catalog, with a search
  box) then **Test** (that registration's tests). OK writes
  `{"id":"<reg_no>","test":"<test>"}` (compact JSON, BOM-less UTF-8) to `-OutFile`.
- Both dropdowns must have a selection for OK to enable. **Skip** / close writes
  nothing (upload.py then sends the job with empty registration => the hub holds it).
- If no catalog can be loaded (no CatalogFile, share unreachable): show a clear
  "catalog unavailable" banner and a free-text registration box (test optional) so
  work never stops; the hub will validate and hold if unknown.
- Legacy printers (no -DeviceType): behave like the old free-text prompt, writing
  `{"id": "..."}` only.
- `upload.py` parses OutFile JSON: `id` -> registration_number, `test` -> test.

## 8. Ingest contract (`POST <hub>/ingest/<token>`)

Multipart/form-data fields sent by `upload.py` for hub printers (legacy printers just
send what they always sent; the hub tolerates missing fields):

| field                 | value                                              |
|-----------------------|----------------------------------------------------|
| `docname`             | document title                                     |
| `registration_number` | selected reg_no, or empty                          |
| `test`                | selected test name, or empty                       |
| `device_name`         | from printer config (server record wins anyway)    |
| `device_type`         | from printer config                                |
| `user`                | Windows user (%u)                                  |
| `job_id`              | spooler job id (%j)                                |
| `sha256`              | hex sha256 of the uploaded bytes (post-encryption) |
| `encrypted`           | `"1"` if AES-256 encrypted, else `"0"`             |
| `file`                | the PDF                                            |

Hub behaviour:
- 404 unknown token. The device row for the token provides authoritative
  `device_name`/`device_type` (client-sent values logged if they differ).
- Validate against the catalog: missing reg / unknown reg / reg's device_type
  mismatch / missing test / unknown test => store as **held** with a reason;
  otherwise **file** into the limsDocs tree and enqueue the Supabase forward.
- Response: `{"ok": true, "status": "filed"|"held", "reason": <null|str>, "id": <int>}`.
  Return 2xx for held too (the client must treat held as success — job is safe).
- Streaming to disk in 1 MiB chunks, run_in_threadpool for blocking work, WAL SQLite
  (same async discipline as `simulator/app.py`).

## 9. Enrollment contract (device registration)

- `GET <hub>/device-types` (header `X-Enroll-Key`) -> `{"device_types": ["gcms","lcms","icpms",...]}`
- `POST <hub>/admin/devices` (header `X-Enroll-Key`), JSON body
  `{"device_name","device_type","printer_name","hostname"}` ->
  201 `{"token","device_name","device_type","ingest_url"}`;
  **409** if device_name already exists (uniqueness is case-insensitive);
  400 invalid name (must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`) or unknown type.
- `setup.ps1 -Action install|add` prompts for: printer name, hub URL, device name,
  device type (offered from /device-types), PDF password (optional), enroll key.
  It calls the enrollment endpoint; on 409 it tells the user to pick another name.
  The returned `ingest_url`/`token` land in the printer's config entry.
- Hub admin endpoints (dashboard) use `X-Admin-Token`. Enroll key and admin token are
  generated on first hub start, stored in `hub/data/`, printed to console.

## 10. limsDocs filing tree + sanitization

```
<lims_docs_dir>\<device_name>\<device_type>\<reg_no>\<test>\<reg_no>_<test>_<uuid>.pdf
```

- The **filename is `<reg_no>_<test>_<uuid>.pdf`** (via `composed_name()`), NOT the
  app-supplied print title — a lab-usable identifier. The original title is kept only
  as document metadata (`docname`) and shown as "from: …" in the dashboard. The
  Supabase storage object key uses the same `<reg>_<test>_<uuid>` form (uuid4).
- Every path segment passes through ONE shared sanitizer in `hub/app.py`:
  keep `[A-Za-z0-9._ -]`, replace others with `_`, strip leading/trailing dots+spaces,
  collapse to `_` if empty, max 100 chars. **Never** join unsanitized client input
  into a filesystem path (path-traversal defense).
- The `documents` row **snapshots `device_name`/`device_type` at ingest**, so a
  document keeps its device identity in the dashboard even after that device's token
  is revoked (no reliance on a live join to `devices`).
- Held files live under `hub/data/held/` (NOT in limsDocs) until assigned.
- `lims_docs_dir` is the **local** path of the shared folder on the central desktop
  (e.g. `C:\limsDocs`), set in `hub/data/config.json`.
- Dashboard presentation: timestamps stored UTC, **displayed in IST** (Asia/Kolkata);
  the "Printed by" column is the Windows `%u`; the docs/held auto-refresh (3 s) pauses
  while an operator is interacting with a control. Device types are managed in the
  Catalog tab (`POST/DELETE /api/device-types`, admin-token) in local mode.

## 11. Supabase (cloud LIMS)

### Schema (lims/supabase/schema.sql)
- `device_types(id text primary key)` — seeded: `gcms`, `lcms`, `icpms`.
- `registrations(id uuid pk default gen_random_uuid(), reg_no text unique not null,
  device_type text not null references device_types, product text default '',
  status text not null default 'open' check (status in ('open','closed')),
  created_by uuid default auth.uid(), created_at timestamptz default now(),
  updated_at timestamptz default now())`
- `registration_tests(id uuid pk, registration_id uuid references registrations
  on delete cascade, test_name text not null, unique(registration_id, test_name))`
- `documents(id uuid pk, registration_id uuid references registrations,
  reg_no text not null, test_name text not null, device_name text not null,
  device_type text not null, docname text, storage_path text not null,
  size bigint, sha256 text, printed_by text, job_id text, encrypted boolean
  default false, received_at timestamptz default now())`
- `updated_at` trigger on registrations; touching registration_tests also bumps the
  parent registration's `updated_at` (the hub polls a single watermark).
- Function `catalog_version() returns timestamptz` = max(updated_at) over registrations
  (security definer, stable) — the hub polls this (~2 s) and refetches on change.
- RLS enabled on all tables: authenticated users can select everything, insert/update
  registrations + registration_tests; `documents` is insert-only for service role
  (clients read-only). Realtime publication on all four tables.
- Storage bucket `lims-docs`, private. Web app downloads via signed URLs.

### Hub -> Supabase forwarding (service role key, central machine ONLY)
1. `POST {supabase_url}/storage/v1/object/lims-docs/{storage_path}`
   (`Authorization: Bearer <service key>`, `Content-Type: application/pdf`,
   `x-upsert: false`); storage_path mirrors the limsDocs tree with a uuid prefix on
   the filename.
2. `POST {supabase_url}/rest/v1/documents` (`apikey` + `Authorization` headers,
   `Prefer: return=minimal`) with the document row.
3. Any failure -> row stays in the hub `forward_queue` table; background worker
   retries with exponential backoff (5 s .. 5 min cap). Forwarding is at-least-once;
   the `sha256` + storage upsert=false make duplicates detectable.
- Catalog pull: `GET /rest/v1/registrations?select=reg_no,product,status,device_type,registration_tests(test_name)&status=eq.open`.
- The service key is stored DPAPI-encrypted (machine scope, same entropy contract as
  §4) in `hub/data/config.json` on Windows; `SUPABASE_SERVICE_KEY` env var overrides.
- No Supabase configured => hub runs in **local mode**: catalog comes from its own
  SQLite (manageable in the dashboard), forwarding queue idles. Nothing breaks.

### Web app (lims/web) — React + Vite + @supabase/supabase-js
- Auth: Supabase email/password sign-in.
- Registrations page: create (reg_no, device_type dropdown from `device_types`,
  tests as chips), list with realtime updates, close/reopen.
- Documents page: live table (realtime INSERT subscription), filter by reg_no /
  device, download via `createSignedUrl`.
- Config via `.env` (`VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`). Anon key +
  RLS only — the service key must never appear in web code.

## 12. Security invariants (additions — all CLAUDE.md invariants still apply)

1. UserCommand argv contract is **unchanged** (`"%f" "%j" "%r" "%u" "%t"`); all new
   data flows through config.json, the catalog files, and the prompt OutFile.
2. `upload.py` stays stdlib-only and must never raise out to the spooler.
3. `$Base` ACL model unchanged; the only new user-facing files live in `ids\`
   (already Authenticated Users M). Catalog temp files are written there.
4. Per-printer ingest tokens: possession of ONE client's token lets it upload only
   as that device. Catalog reads also require a device token.
5. Enrollment requires the enroll key; the dashboard requires the admin token; both
   are random (secrets.token_urlsafe) and never checked into the repo.
6. Passwords: DPAPI-at-rest on clients; PBKDF2 super-admin hash; nothing plaintext in
   logs; `view-password` requires super-admin verification + elevation.
7. The Supabase **service role key never leaves the central desktop**; the web app
   uses the anon key + RLS.
8. Hub sanitizes every path segment (§10); device_name validated at enrollment.
9. Held documents are stored outside the share and only enter limsDocs/LIMS after an
   operator assigns a valid registration + test.
```
