# LIMS Print Pipeline — Architecture & Interface Contracts

This document is the **single source of truth** for the LAN print-to-LIMS pipeline.
Every interface below is a hard contract: if you change one side, change the other
in the same commit. It extends (does not replace) the invariants in `CLAUDE.md`.

> For a practical, example-driven walkthrough of the integration endpoints (send to
> LIMS, fetch from LIMS, query the hub — with curl examples and how to build your own
> endpoint), see **`documents/LIMS-Integration-Guide.md`**. Production deployment:
> **`hub/PRODUCTION.md`**.

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
    [-BatchNote]
```

- Hub printers: two dropdowns — **Registration number** (from catalog, with a search
  box) then **Test** (that registration's tests). OK writes
  `{"id":"<reg_no>","test":"<test>"}` (compact JSON, BOM-less UTF-8) to `-OutFile`.
- Both dropdowns must have a selection for OK to enable. The dialog is **STRICT**:
  **Cancel** / closing the window writes `{"cancel":true}` and `upload.py`
  **DISCARDS the job** — nothing is converted, uploaded, held, or preserved.
  Only the `-TimeoutSec` auto-close writes nothing (nobody was at the machine to
  decide): the job then proceeds without a number and the hub HOLDS it, so an
  unattended print is never silently destroyed.
- If no catalog can be loaded (no CatalogFile, share unreachable): show a clear
  "catalog unavailable" banner and a free-text registration box (test optional) so
  work never stops; the hub will validate and hold if unknown. OK requires a
  non-empty registration number (strict); Cancel discards the job as above.
- Legacy printers (no -DeviceType): behave like the old free-text prompt, writing
  `{"id": "..."}` only.
- `upload.py` parses OutFile JSON: `id` -> registration_number, `test` -> test.
- **Batch coalescing (client-internal, `prompt_batch` default true):** when N jobs
  from the same user hit the same printer at once (an app printing N selected PDFs),
  only ONE dialog appears. The first job per (user, printer) takes a lock file
  (`ids\<user>.<printer>.batch.lock`, O_CREAT|O_EXCL, stale-broken if the leader
  dies), prompts with `-BatchNote`, and records the answer in
  `ids\<user>.<printer>.batch.json`; jobs arriving while the dialog is open — or
  within `prompt_batch_grace_seconds` (default 1, measured from the ANSWER, so the
  next print the user makes re-prompts instead of inheriting the previous
  number) — reuse it silently. A leader's **Cancel
  cancels the whole batch** (`"cancel": true` in the batch state; every follower
  discards its job too). A leader timeout applies to the whole batch as
  no-answer (all jobs upload without a number; hub printers are held). Every
  failure path degrades to no-answer, never blocks a job.

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
| `pdf_password`        | the AES-256 passphrase (only when `encrypted=1`)   |
| `file`                | the PDF                                            |

`pdf_password` lets the central PC / LIMS open the encrypted document. The hub
stores it ONLY in `protect_secret()` form (DPAPI machine-scope at rest,
`documents.pdf_password_enc`), never logs it, exposes it to operators solely via
the admin-gated `GET /api/documents/{id}/password`, and decrypts it just-in-time
to include it in the Supabase `documents` row (`pdf_password` column) when
forwarding. It travels in the same request body as the PDF itself — run the hub
behind HTTPS if the LAN is not trusted.

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

> **Model (current): printer_data.** The hub is driven by a single flat
> `printer_data` feed keyed on **department + equipment + registration + method +
> parameter**, not the older `device_type`/`registrations`/`registration_tests`
> shape. The migrations live in the **megascan-core-hub** repo
> (`supabase/migrations/2026072412*/13*` + `20260725100000_refresh_printer_data.sql`),
> branch `f/print`. The legacy `lims/supabase/schema.sql` describes the superseded
> device_type model and is kept only for reference.

### Schema (megascan `supabase/migrations/*`)
- `printer_data(registration_number, department_name, equipment_name, status,
  test_method, test_parameter)` — all `text NOT NULL`; PK
  `(registration_number, department_name, test_method, test_parameter)`. The flat
  feed the hub reads. Populated by `refresh_printer_data()` (below).
- `test_parameter_equipment(test_parameter text pk, equipment_name text not null)`
  — the manual test→instrument bridge (the LIMS has no direct test↔equipment
  relation); it supplies `printer_data.equipment_name`.
- `printer_devices(device_name text pk, password_enc bytea, created_at, updated_at)`
  — the per-printer **AES-256 PDF password, encrypted at rest** (pgcrypto, key in
  Supabase **Vault**). Set/rotate + reveal ONLY via the admin-gated SECURITY
  DEFINER RPCs `set_device_password(_device_name,_password)` /
  `reveal_device_password(_device_name)` (the key never leaves the DB; the column
  is ciphertext even to authenticated readers).
- `printer_documents(id uuid pk, pdf_name, pdf_from, device_name→printer_devices,
  equipment_name, registration_number, test_method, department_name,
  test_parameter, printed_by, size_of_pdf, received_time, storage_path)` — the
  landing row per pushed PDF; **unique `storage_path`** is the idempotency key.
- `refresh_printer_data()` + statement-level triggers on `sample_registrations` /
  `test_parameter_equipment` / `test_methods` / `test_parameters` / `departments`
  rebuild `printer_data` from the live registration data (reg_no + method_number +
  test_name + department via `dept_param_map`, equipment via the bridge).
- `printer_data_version()` returns a **text** watermark (`md5(all rows)#count`);
  the hub polls it (~2 s) and refetches only on change (compared opaquely).
- Storage bucket `printer-documents` (private); LIMS downloads via signed URLs.

### Hub -> Supabase (service role key, central machine ONLY)
- **Catalog pull:** poll `POST /rest/v1/rpc/printer_data_version` (empty body);
  on change `GET /rest/v1/printer_data?select=registration_number,department_name,
  equipment_name,status,test_method,test_parameter` and replace the local mirror.
- **Enrollment:** the hub POSTs `/rest/v1/rpc/set_device_password`
  `{_device_name,_password}` so the device's PDF password is stored (encrypted) in
  `printer_devices` for the LIMS to reveal. Best-effort (enrollment still succeeds
  if Supabase is unreachable; the client keeps the password locally to encrypt).
- **Push (forward worker):**
  1. `POST {url}/storage/v1/object/printer-documents/{storage_path}`
     (`Authorization: Bearer <key>`, `Content-Type: application/pdf`,
     `x-upsert:false`; 409 = already uploaded = success). `storage_path` mirrors
     the limsDocs tree `department/equipment/registration/method/parameter/<reg>_<method>_<param>_<uuid>.pdf`.
  2. `POST {url}/rest/v1/printer_documents?on_conflict=storage_path`
     (`Prefer: resolution=merge-duplicates,return=minimal`) — UPSERT on the unique
     `storage_path`, so an at-least-once retry is a no-op.
  3. On success the hub **deletes the local limsDocs copy**. Ordering guarantees
     no data loss: a crash between push and delete leaves `status='filed'` +
     `stored_path`, so the next run re-pushes (409) then deletes.
  Failures keep the row in `forward_queue`; backoff 5 s..5 min.
- The service key is DPAPI-encrypted at rest in `hub/data/config.json` (machine
  scope, §4 entropy); `SUPABASE_SERVICE_KEY` env overrides. No Supabase configured
  => **local mode**: `printer_data` mirror stays empty until synced, forward queue
  idles; nothing breaks.

### Device identity & validation
- A printer/device is enrolled with **department + equipment** (not a single
  device_type). The hub's `/catalog` returns the cascading
  registration→method→parameter tree filtered by the device's department+equipment;
  the print prompt shows 3 dependent searchable dropdowns.
- Ingest validates the full `(department, equipment, registration_number,
  test_method, test_parameter)` tuple against the `printer_data` mirror; a missing
  tuple is **HELD** (`unknown_registration` / `missing_*`), still HTTP 2xx.

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
   Known, accepted trade-off: `ids\` is writable by every authenticated local
   user, so one local user could forge another's `.prompt`/`.id`/batch files
   (wrong number, or a forged `{"cancel":true}` = discard). Same trust level as
   the pre-existing `.id` mechanism; the hub still validates every number.
4. Per-printer ingest tokens: possession of ONE client's token lets it upload only
   as that device. Catalog reads also require a device token.
5. Enrollment requires the enroll key; the dashboard requires the admin token; both
   are random (secrets.token_urlsafe) and never checked into the repo.
6. Passwords: DPAPI-at-rest on clients; PBKDF2 super-admin hash (factory plaintext
   NOT in the repo — only its precomputed hash record in `setup.ps1`); nothing
   plaintext in logs; `view-password` requires super-admin verification + elevation.
   The per-document `pdf_password` (§8) is DPAPI-at-rest on the hub
   (`pdf_password_enc`), never logged, and admin-gated for reveal.
7. The Supabase **service role key never leaves the central desktop**; the web app
   uses the anon key + RLS.
8. Hub sanitizes every path segment (§10); device_name validated at enrollment.
9. Held documents are stored outside the share and only enter limsDocs/LIMS after an
   operator assigns a valid registration + test.
```
