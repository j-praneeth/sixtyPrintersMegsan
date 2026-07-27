# LIMS Integration Guide — endpoints & data flow

How the hub talks to your cloud LIMS, and how any system (including your LIMS) can
talk to the hub. There are **three directions**:

| Direction | Who calls whom | Purpose |
|---|---|---|
| **A. Fetch from LIMS** | hub → LIMS | Pull the registration catalog (reg numbers, tests, device types) so client dropdowns stay current. |
| **B. Send to LIMS** | hub → LIMS | Push each filed PDF + its metadata (storage object + a `documents` row). |
| **C. Query the hub** | your system → hub | Read documents/status, download PDFs, manage the catalog, enroll devices. |

The hub ships with a working **Supabase** integration (A + B). This guide gives the
exact HTTP contract for each call so you can (1) understand it, (2) point the hub at
a Supabase project, or (3) build your **own** LIMS endpoint that speaks the same
shapes. Where you'd change code for a fully custom backend is called out per section.

Configuration lives in `hub/data/config.json` (or the dashboard **Settings** tab):

```jsonc
{
  "lims_docs_dir": "C:\\limsDocs",
  "supabase_url": "https://YOUR-PROJECT.supabase.co",   // your LIMS API base
  "supabase_service_key_dpapi": "…",                    // DPAPI-encrypted on Windows
  "bucket": "lims-docs",                                // object-storage bucket
  "poll_seconds": 2                                      // catalog poll cadence
}
```
`SUPABASE_SERVICE_KEY` (env) overrides the stored key. With no `supabase_url` the hub
runs in **local mode**: the catalog is managed on the hub (section C) and the send
queue idles until you configure a LIMS.

---

## A. Fetch from LIMS — the catalog pull (LIMS → hub cache → clients)

The hub keeps a local cache of **open registrations** (each with its device type and
its allowed tests) and the list of **device types**. It refreshes on a change
watermark so it isn't constantly re-downloading.

### A.1 Change watermark (poll)
```
POST {supabase_url}/rest/v1/rpc/catalog_version
Headers: apikey: <key>   Authorization: Bearer <key>   Content-Type: application/json
Body: {}
→ 200  "2026-07-20T07:32:37.0018+00:00"        (a timestamptz string)
```
The hub calls this every `poll_seconds`. When the value changes, it refetches A.2/A.3.
*Your LIMS must return a value that changes whenever any registration/test changes.*

### A.2 Registrations (+ their tests)
```
GET {supabase_url}/rest/v1/registrations?select=reg_no,product,status,device_type,registration_tests(test_name)&status=eq.open
Headers: apikey: <key>   Authorization: Bearer <key>
→ 200
[
  { "reg_no":"R-2026-0001", "product":"Paracetamol batch A", "status":"open",
    "device_type":"gcms",
    "registration_tests":[ {"test_name":"assay"}, {"test_name":"dissolution"} ] }
]
```

### A.3 Device types
```
GET {supabase_url}/rest/v1/device_types?select=id
Headers: apikey: <key>   Authorization: Bearer <key>
→ 200  [ {"id":"gcms"}, {"id":"lcms"}, {"id":"icpms"} ]
```

After a successful refetch the hub replaces its cache in one transaction and exports
`\<lims_docs_dir>\.vcp\catalog\<device_type>.json` + `all.json` (the offline fallback
the client dialog reads when it can't reach the hub over HTTP).

> **Build your own:** expose A.1/A.2/A.3 at your API base with these exact response
> shapes and set `supabase_url` to it. If your API can't match the PostgREST dialect,
> adapt `catalog_sync_task()` and `_refresh_from_supabase()` in `hub/app.py` (they are
> the only two functions that issue these three calls).

---

## B. Send to LIMS — forwarding a filed document (hub → LIMS)

Every job that is **filed** (has a valid registration + test) is queued and forwarded
in two steps by the background `forward_worker`. `storage_path` is fixed when the job
is queued and reused on every retry, so the whole operation is **idempotent**.

### B.1 Upload the PDF to object storage
```
POST {supabase_url}/storage/v1/object/{bucket}/{storage_path}
Headers: Authorization: Bearer <key>   Content-Type: application/pdf   x-upsert: false
Body:  <raw PDF bytes>
→ 200 (created)  |  409 (already exists → treated as success on retry)
```
`storage_path` = `<device>/<type>/<reg>/<test>/<reg>_<test>_<uuid>.pdf` (sanitized).

### B.2 Insert/upsert the documents row
```
POST {supabase_url}/rest/v1/documents?on_conflict=storage_path
Headers: apikey: <key>   Authorization: Bearer <key>
         Prefer: resolution=merge-duplicates,return=minimal
Body:
{
  "reg_no":"R-2026-0001", "test_name":"assay",
  "device_name":"gcms-01", "device_type":"gcms",
  "docname":"New tab",                       // original app print title (metadata)
  "storage_path":"gcms-01/gcms/R-2026-0001/assay/R-2026-0001_assay_8ee38172.pdf",
  "size":91797, "sha256":"…", "printed_by":"priya", "job_id":"101",
  "encrypted": true
}
→ 2xx  (upsert on storage_path → a retry updates instead of duplicating)
```
On success the document flips to `forwarded`. On failure it becomes `forward_failed`
and retries with exponential backoff (5 s → 5 min cap), forever. The web app reads the
PDF back with a **signed URL** from the same bucket.

> **Build your own:** implement a bucket-style object PUT (B.1) and a documents upsert
> (B.2) with these fields, keyed on `storage_path` for idempotency. The only function
> to adapt is `_forward_one()` in `hub/app.py`.

Required LIMS DB shape (Supabase schema is in `lims/supabase/schema.sql`): tables
`device_types(id)`, `registrations(reg_no unique, device_type, product, status, updated_at)`,
`registration_tests(registration_id, test_name)`, `documents(storage_path unique, reg_no,
test_name, device_name, device_type, docname, size, sha256, printed_by, job_id, encrypted,
received_at)`, plus the `catalog_version()` function and a private storage bucket.

---

## C. Query the hub — the hub's own REST API (your system → hub)

Base URL: `http://<hub-host>:8000`. Three auth planes:

| Plane | Header | Grants |
|---|---|---|
| Device token | `X-Device-Token` (or the token in the ingest URL) | ingest + read the catalog |
| Enroll key | `X-Enroll-Key` | list device types, enroll a device |
| Admin token | `X-Admin-Token` | everything under `/api/*` + the dashboard |

Open (no auth): `GET /healthz`, `GET /` (dashboard HTML). Tokens compared in constant
time. The admin/enroll secrets are in `hub/data/*.txt`.

### C.1 Ingest a print job (this is what the client prints to)
```
POST /ingest/{token}          multipart/form-data
fields: file=<pdf>, docname, registration_number, test, device_name, device_type,
        user, job_id, sha256, encrypted
→ 2xx { "ok":true, "status":"filed"|"held", "reason":<null|str>, "id":<int> }
```
`held` is still 2xx — the job is safe; assign it later (C.6).

### C.2 Catalog for a device (client dropdowns)
```
GET /catalog?device_type=gcms       Header: X-Device-Token: <token>
→ 200 { "version":"…", "device_type":"gcms",
        "registrations":[ {"reg_no":"…","product":"…","status":"open","tests":[…]} ] }
```

### C.3 Enrollment
```
GET  /device-types                  Header: X-Enroll-Key: <key>
     → { "device_types":["gcms","lcms","icpms"] }
POST /admin/devices                 Header: X-Enroll-Key: <key>   (JSON)
     { "device_name":"gcms-01", "device_type":"gcms", "printer_name":"…", "hostname":"…" }
     → 201 { "token":"…", "device_name":"gcms-01", "device_type":"gcms", "ingest_url":"…" }
     → 409 duplicate name · 400 bad name/type
```

### C.4 Documents (read status / metadata)  — `X-Admin-Token`
```
GET /api/documents[?status=filed|held|forwarded|forward_failed]
→ [ { id, device_name, device_type, reg_no, test_name, name, docname, size, sha256,
      printed_by, job_id, encrypted, status, held_reason, received, forwarded_at, last_error } ]
```
`name` is the `reg_test_id.pdf` identifier; `received` is UTC (the dashboard shows IST).

### C.5 Download a PDF  — `X-Admin-Token`
```
GET /api/documents/{id}/file   → application/pdf (the stored file)
```

### C.6 Assign a held document  — `X-Admin-Token`
```
POST /api/documents/{id}/assign      (JSON) { "reg_no":"R-2026-0001", "test":"assay" }
→ 200 { "ok":true, "status":"filed", "id":<int> }   (moves it into the tree + forwards)
```

### C.7 Catalog management (local mode)  — `X-Admin-Token`
```
GET    /api/catalog                                  → full snapshot {version, device_types, registrations}
POST   /api/registrations   {reg_no, device_type, product, tests:[…]}
DELETE /api/registrations/{reg_no}
POST   /api/device-types    {id}
DELETE /api/device-types/{id}
```

### C.8 Devices, settings, health  — `X-Admin-Token` (health is open)
```
GET    /api/devices                 → enrolled devices (+ ingest URLs, doc counts)
DELETE /api/devices/{id}            → revoke a device's token (documents keep their name)
GET    /api/enroll-key              → the enroll key (admin only)
GET    /api/settings   /   POST /api/settings   {lims_docs_dir, supabase_url, supabase_service_key, poll_seconds}
GET    /healthz                     → "ok"
```

### Example — pull all forwarded documents from the hub
```bash
curl -s -H "X-Admin-Token: $ADMIN" "http://192.168.1.172:8000/api/documents?status=forwarded"
```

---

## Which mechanism to use

- **You have (or will build) a cloud LIMS** → configure `supabase_url` + key so the hub
  auto-**fetches** the catalog (A) and **sends** every document (B). Your LIMS UI reads
  the `documents` table / storage. Build a Supabase-compatible endpoint, or adapt the
  two/one functions noted above.
- **You want another system to read the hub directly** (dashboards, ERP, audit) → use
  the hub's **admin API** (C.4–C.8) to pull document metadata/status and download PDFs.
- **No cloud yet** → local mode: manage the catalog via C.7 and read via C.4; turn on
  forwarding later by setting `supabase_url` — the queued backlog drains automatically.

See `../ARCHITECTURE.md` (§6, §8, §9, §11) for the underlying contracts and
`hub/PRODUCTION.md` for running the hub 24/7.
