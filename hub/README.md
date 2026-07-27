# LIMS Print Hub

> **Running it 24/7 in production?** See **[PRODUCTION.md](PRODUCTION.md)** — install
> it as an auto-start / auto-restart Windows service (`install-hub-service.bat`),
> hide the tokens from the console, and the 20-minute dashboard session timeout.
>
> **Integrating with your LIMS?** See **[../documents/LIMS-Integration-Guide.md](../documents/LIMS-Integration-Guide.md)**
> — the exact endpoints and payloads to send data to the LIMS, fetch the catalog from
> it, and query the hub's own API, with examples for building a custom endpoint.


The central receiver of the LAN print-to-LIMS pipeline (see `../ARCHITECTURE.md`).
It runs on the central desktop (e.g. `192.168.1.172`) — the one machine that hosts
the `limsDocs` share and has internet access — and does four jobs:

1. **Enrolls devices.** Each client printer gets a unique `device_name` and a
   secret per-printer ingest token (`POST /admin/devices`, header `X-Enroll-Key`).
2. **Receives print jobs.** Clients POST PDFs to `/ingest/<token>`. Jobs with a
   valid registration number + test are **filed** into
   `\<limsDocs>\<device_name>\<device_type>\<reg_no>\<test>\...` and queued for
   Supabase. Jobs with a missing/unknown registration or test are **held** under
   `hub/data/held/` (never lost, never forwarded) until an operator assigns them
   in the dashboard. Held is still a 2xx to the client — the job is safe.
3. **Serves the registration catalog** to clients (`GET /catalog`, header
   `X-Device-Token`) and exports it to `\<limsDocs>\.vcp\catalog\*.json` as the
   share-file fallback for the client-side prompt.
4. **Syncs with Supabase** (optional): polls the `catalog_version` watermark
   (~2 s), pulls open registrations into its local cache, and forwards every
   filed PDF to Supabase Storage + the `documents` table with a retry queue
   (exponential backoff, at-least-once).

## Running it

```bat
run.bat          :: Windows (the real deployment) — provisions .venv with uv
```

```bash
./run.sh         # macOS / Linux (development)
```

Requires [uv](https://docs.astral.sh/uv/). Environment overrides:
`HUB_HOST` (default `0.0.0.0` — it must be reachable from the LAN),
`HUB_PORT` (default `8000`), `HUB_DATA_DIR` (default `hub/data/`, for tests),
`HUB_LIMSDOCS_DIR` (overrides the configured limsDocs path),
`SUPABASE_SERVICE_KEY` (overrides the stored service key).

## First run

On first start the hub generates two secrets under `hub/data/` and prints them:

```
 ADMIN TOKEN: <random>   (paste into the dashboard)
 ENROLL KEY : <random>   (give to setup.ps1 when enrolling printers)
```

- **Admin token** (`data/admin_token.txt`) — unlocks the dashboard and the
  `/api/*` endpoints (`X-Admin-Token` header).
- **Enroll key** (`data/enroll_key.txt`) — required to register new devices
  (`X-Enroll-Key` header). The dashboard can display it (Devices tab →
  "Show enroll key") after you enter the admin token.

Open the dashboard at `http://localhost:8000/` (or `http://192.168.1.172:8000/`
from another machine), paste the admin token, and you get: **Devices** (enroll
key display, revoke), **Documents** (filter by status, download, auto-refresh),
**Held** (assign a registration + test to file & forward), **Catalog** (view,
local add/delete), **Settings** (limsDocs dir, Supabase URL/key, poll interval).

## How the 60 clients connect

On each client PC, `setup.ps1 -Action install|add` asks for the hub URL
(`http://192.168.1.172:8000`), a unique device name, the device type (offered
from `GET /device-types`), and the **enroll key**. It calls
`POST /admin/devices` and stores the returned `ingest_url` + `token` in that
printer's `config.json` entry. From then on every print becomes:

```
GET  /catalog?device_type=gcms      (X-Device-Token)  -> registration dropdown
POST /ingest/<token>                (multipart PDF + metadata)
```

Duplicate device names are rejected with 409 (case-insensitive) — pick another.
Revoking a device in the dashboard invalidates its token immediately.

## Local mode vs Supabase mode

- **Local mode** (no Supabase configured — the default): the registration
  catalog lives in the hub's own SQLite and is managed on the Catalog tab.
  Filed PDFs stay in the limsDocs tree; the forward queue idles (jobs queue up
  and are delivered later if Supabase is configured afterwards).
- **Supabase mode** (Settings tab: URL + service role key): the hub polls
  `rpc/catalog_version` every `poll_seconds` (default 2) and replaces its local
  catalog cache from the cloud `registrations`/`registration_tests`/`device_types`
  tables — local catalog edits are overwritten by the next sync. Every filed PDF
  is uploaded to the private `lims-docs` Storage bucket and a row is inserted
  into `documents`. Failures retry with exponential backoff (5 s doubling,
  300 s cap); a storage 409 (already uploaded) counts as success.

## Security notes

- The **Supabase service role key never leaves this machine.** It is stored
  DPAPI-encrypted (machine scope, entropy `VCP-DPAPI-v1`) in `data/config.json`
  on Windows; the `SUPABASE_SERVICE_KEY` env var overrides it. The web app uses
  the anon key + RLS only.
- `hub/data/` contains the admin token, enroll key, held PDFs, and the DB —
  keep it out of any share and out of version control.
- Every filesystem/storage path segment built from client input goes through
  one shared sanitizer (`sanitize_segment`, ARCHITECTURE.md §10) — never join
  raw client input into a path.
- Held documents are stored **outside** the limsDocs share and only enter it
  (and the cloud) after an operator assigns a valid registration + test.
- The hub is plain HTTP on a trusted LAN. If the LAN is not trusted, put a
  TLS-terminating reverse proxy in front and enroll clients with the HTTPS URL.
