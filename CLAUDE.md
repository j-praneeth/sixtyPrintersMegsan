# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Windows **virtual printer** toolkit, now the client tier of a **LAN → LIMS pipeline**. It installs one or more printers that appear in the normal print dialog; anything printed to them is converted to a PDF and POSTed (multipart/form-data) to a per-printer URL. Target OS is **Windows 10/11 x64**, but the Python HTTP layer is developed and tested on any platform.

Two printer modes coexist in `config.json` (both must keep working):
- **Hub printer** (entry has `device_name` + `token`): posts to the LAN hub (`hub/`, FastAPI on the central desktop, default `http://192.168.1.172:8000/ingest/<token>`), with a catalog-driven Registration→Test dropdown prompt and the full metadata field set. The hub files PDFs into the `limsDocs` share tree, **holds** jobs with no/invalid registration, and forwards filed PDFs to a cloud LIMS (Supabase; schema + React app under `lims/`).
- **Legacy printer** (no `device_name`/`token`): behaves exactly as the original tool — free-text prompt, plain `docname`+`file` POST to its URL.

**`ARCHITECTURE.md` is the cross-component interface contract** (config v2 schema, DPAPI, catalog, prompt, ingest, enrollment, filing tree, Supabase). Read it before changing anything that crosses the client↔hub↔LIMS boundary, and change both sides of a contract in the same commit. Deployment runbook: `LAN-SETUP.md`.

## The pipeline (read this before touching anything)

A single print job flows through four layers that live in different files/tools. Understanding the whole chain requires this map:

```
App ─Print▶ "Printer" ─(v3 PostScript driver → PostScript)▶ redirection port
   ▶ mfilemon/clawmon port monitor ─(writes .ps to spool\, launches UserCommand)▶ upload.py
   ▶ upload.py: Ghostscript PS→PDF, look up URL by printer name, HTTPS POST ▶ your URL
```

- **Printer driver must be v3, not v4.** `setup.ps1` (`Resolve-PsDriver`) uses an inbox **v3 PostScript** driver — `"MS Publisher Color Printer"`, falling back to `"MS Publisher Imagesetter"` (both are pscript5 / `PS5UI.DLL`, from `prnge001.inf`). The inbox `"Microsoft PS Class Driver"` is a **v4 class driver**, and v4 drivers **cannot be attached to a port owned by a third-party port monitor** (mfilemon/clawmon): the spooler rejects `Add-Printer`/`Set-Printer` with **ERROR_NOT_SUPPORTED (0x80070032)** and logs *"…may not be used in conjunction with a non-inbox port monitor."* v3 pscript5 drivers bind cleanly and still emit PostScript for Ghostscript. Don't switch back to a v4/class driver.
- **Port monitor = mfilemon** (`"Multi File Port Monitor"`, `mfilemon.dll`) *or* **clawmon** (`"clawmon printer port monitor"`, `clawmon.dll`) — same C++ code/interface. clawmon has no prebuilt binaries, so `setup.ps1` auto-installs mfilemon by default and uses clawmon only if its DLLs are dropped in `vendor/`. mfilemon is fetched from the author's **GitHub releases** (`lomo74/mfilemon`), not SourceForge (whose `/download` endpoint serves an HTML interstitial / 403s to non-browser clients). `Ensure-Monitor` prefers an offline `vendor\mfilemon-setup.exe` if present, validates the download is a real PE (`Test-IsExe`), and picks the correct silent-install flags per toolkit — the GitHub v1.6.x build is **Inno Setup** (`/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`), the older SourceForge 1.5.2 build is NSIS (`/S`).
- The monitor stores each port as a **direct registry subkey** of `HKLM\SYSTEM\CurrentControlSet\Control\Print\Monitors\<MonitorName>\<PortName>`. `setup.ps1` (`Set-Port`) writes the values via the .NET `Microsoft.Win32.Registry` API (needed because the port name ends in a colon). Value names/types are fixed by the monitor's source: `OutputPath`/`FilePattern`/`UserCommand`/`ExecPath`/`User`/`Domain`/`Password` are `REG_SZ`; `Overwrite`/`WaitTermination`/`WaitTimeout`/`PipeData`/`HideProcess` are `REG_DWORD`.
- Ports are loaded from the registry at **spooler start**, so `Set-Port` uses **stop-Spooler → write registry → start-Spooler** ordering (writing while the spooler runs risks the monitor clobbering the key on shutdown).
- The monitor's macros (from its `pattern.cpp`): **`%f`** = spooled file path, **`%j`** = job id, **`%r`** = **printer name**, **`%t`** = **document/job title**. `PipeData=0` means the job is written to `%f` and *then* `UserCommand` is launched (file already complete); the monitor uses `CreateProcess` (no shell), so no cmd metachar interpretation.

## Critical coupling: the UserCommand ↔ upload.py argv contract

`setup.ps1` writes this exact `UserCommand`:
```
"<venv>\Scripts\pythonw.exe" -P "<base>\upload.py" "%f" "%j" "%r" "%u" "%t"
```
`upload.py:main()` maps it as `ps_file=argv[1]`, `job_id=argv[2]`, `printer=argv[3]`, `user=argv[4]`, `docname=" ".join(argv[5:])` (tail-join so document titles with spaces survive; `%u`=user is a single token placed *before* the docname tail). `-P` is a Python **interpreter** flag (keeps the script dir off `sys.path[0]` — a hardening measure, see below), so it does **not** shift the argv mapping. On **Windows 7** the flag is `-I` instead (`$PyIsoFlag` in `setup.ps1`: `-P` is 3.11+, `-I` is the 3.8-compatible superset) and the interpreter is `<base>\python\py38-embed\pythonw.exe` — same argv contract either way. **If you change the argument order or count in either place, change both.**

## OS support: Windows 7 SP1 / 10 / 11 (client tier)

`setup.ps1` self-adapts; keep both branches working:
- `$script:IsLegacyOs` (NT major < 10, or `VCP_COMPAT_FORCE_LEGACY=1` — a testing knob that forces the legacy paths on a modern OS) and `$script:HasPrintCmdlets` (capability probe) drive the branching.
- **Printer/driver management goes ONLY through the shims** (`Get-VcpPrinters`, `Add-/Set-/Remove-VcpPrinter`, `Test-/Add-VcpPrinterDriver`): the PrintManagement cmdlets are Win8+; on Win7 the shims use WMI `Win32_Printer`/`Win32_PrinterDriver` + `printui.dll,PrintUIEntry /ia` (INF: `prnge001.inf` on 10/11, `ntprint.inf` on 7). Never call `Get-Printer`/`Add-Printer`/etc. directly in `setup.ps1` or `print-register.ps1`.
- **Win7 Python**: uv and CPython 3.12 require Win10+. `Ensure-LegacyPython` provisions the **Python 3.8.10 embeddable** zip (vendor\ → python.org) into `$Base\python\py38-embed` — no venv, no pip; its `._pth` pins `sys.path` (script dir never importable, so the sibling-module hardening holds without `-P`). Consequence: **`upload.py` must stay Python 3.8-compatible** (no `list[str]`/`X | None` annotations, no `match`, no `str.removeprefix`) — verified with `uvx vermin --target=3.8- upload.py`.
- **Win7 tool pins**: Ghostscript 9.56.1 (`gs9561w64.exe`) and qpdf 10.6.3 (mingw64 zip) — newer builds aren't tested on Win7. Win 10/11 keep fetching latest.
- **Win7 prereqs** (deployment, not code): **.NET 4.8 (≥4.7.2)** then WMF 5.1 — PowerShell 2.0 lacks `ConvertFrom-Json`/`Invoke-WebRequest` (the script exits early if PS < 5), and the super-admin PBKDF2 hash (`Get-PasswordHash`/`Test-SuperAdminPassword`) uses the `Rfc2898DeriveBytes(…, HashAlgorithmName)` constructor that only exists on .NET ≥ 4.7.2, so 4.6 fails on the first install. See SETUP.md §0.
- **Win11**: `Assert-NoProtectedPrint` refuses to install while Windows Protected Print mode is on (WPP blocks third-party port monitors, which kills mfilemon).
- The **hub** (`hub/`, FastAPI + 3.12) stays on Win 10/11 — the Win7 support is client-only.

## Per-job registration number: set-id + print-register

Both helpers run as the **normal user** (no elevation) and write the same file:
`%ProgramData%\VirtualCloudPrinter\ids\<user>.id` (JSON `{id, once}`). `upload.py:read_pending_id()` reads it keyed by `%u`, attaches it as the `registration_field` (default `registration_number`) form field, and **consumes** the file if `once` (deleted at read time, before upload). The `ids\` subfolder is the **only** user-writable path under `$Base` (granted Authenticated Users `M` in `Do-Install`); it is safe because `upload.py` only reads it as opaque text and never imports from it. The username sanitizer must stay identical on **all three** places (`upload.py:sanitize_userfile` ↔ the `-replace` in `set-id.ps1` ↔ the `-replace` in `print-register.ps1`).

- **Per-print prompt (default on)** — `upload.py:get_registration_id()` pops a dialog for **every** print via `prompt_registration_in_user_session()`. Because `upload.py` runs as **SYSTEM in session 0**, it can't show UI directly, so it launches `prompt-id.ps1` **into the interactive console session** with `WTSQueryUserToken` + `CreateProcessAsUser` (`ctypes`, still stdlib-only). The dialog writes JSON to `ids\<user>.<jobid>.prompt` — `{"id": ...}` legacy, `{"id","test"}` hub, `{"cancel":true}` on Cancel — and `upload.py` reads+consumes it. **The dialog is STRICT: "Cancel" / closing the window DISCARDS the job** (the `.ps` is deleted; nothing is converted, uploaded, held, or preserved — deliberate user decision, requested behavior). Infrastructure failures stay best-effort: no interactive session / any Win32 failure / timeout (`prompt_timeout_seconds`, default 180) → returns no-answer and the print proceeds (falling back to a pre-set value; hub printers upload anyway and are **held** at the hub), so an unattended print is never silently lost. Toggle with `config.json` `"prompt_registration"` (default `true`); turn off for unattended/batch use. `prompt-id.ps1` is copied into `$Base` and granted the print user `(RX)` (read+execute only — see the ACL invariant) so the user-session PowerShell can open it.
- **Batch prompt coalescing (default on)** — when one user's app prints N PDFs at once (N concurrent jobs, since mfilemon `WaitTermination=0`), **one dialog** appears and its answer (or its Cancel, which discards every job in the batch) applies to the whole batch. `upload.py:prompt_with_batching()` elects a leader per (user, printer) via `ids\<user>.<printer>.batch.lock` (`O_CREAT|O_EXCL`; stale locks broken after `prompt_timeout_seconds`+15 s so a dead leader can't strand followers) which prompts with `-BatchNote` and writes the answer atomically to `ids\<user>.<printer>.batch.json`; followers reuse it if they arrive while the dialog is open or within `prompt_batch_grace_seconds` (default 1 s, **measured from the ANSWER** — barely a beat, so it catches just the tail of the same burst of jobs; the next document the user prints re-prompts instead of silently inheriting the previous registration number, which would be wrong data in the LIMS). Keyed per printer so hub/legacy printers never share answers. Failures degrade to no-answer like the rest of the prompt path and the fall-through to `read_pending_id` is unchanged; an explicit batch Cancel wins over any sticky pre-set id (the jobs are discarded). Config: `"prompt_batch"` (default `true`), `"prompt_batch_grace_seconds"` (default 1). (The old `prompt_batch_window_seconds` key is ignored — it allowed reuse for 45 s from dialog OPEN, which let a later unrelated print inherit the previous answer.)
- **Hub prompt flow (catalog dropdowns)** — for hub printers, `upload.py` first GETs `<hub>/catalog?device_type=X` (header `X-Device-Token`, 5 s cap), writes the JSON to `ids\<user>.<jobid>.catalog.json` (BOM-less; deleted after the prompt), and launches `prompt-id.ps1` with `-DeviceType`/`-CatalogFile`/`-FallbackDir`. If the SYSTEM-side fetch fails, the prompt (running AS THE USER, who can reach the SMB share) loads `<catalog_share_dir>\<device_type>.json` itself; if neither loads it degrades to free text with a warning (hub validates + holds). The catalog JSON schema and the `-FallbackDir` filename are contracts with `hub/app.py` — see ARCHITECTURE.md §6/§7.
- **Hub ingest metadata** — hub printers add multipart fields `registration_number`, `test`, `device_name`, `device_type`, `user`, `job_id`, `sha256` (of the uploaded, post-encryption bytes), `encrypted`, and — when the PDF is encrypted — `pdf_password` (the AES-256 passphrase; the hub stores it DPAPI-at-rest in `documents.pdf_password_enc`, reveals it only via the admin-gated `/api/documents/{id}/password`, and forwards it to the Supabase `documents.pdf_password` column; NEVER log it) (ARCHITECTURE.md §8). The hub always answers 2xx (`filed` or `held`); the client treats held as success. Legacy printers send exactly what they always sent.
- **Per-printer PDF passwords are DPAPI blobs** — `printers.<name>.pdf_encryption.password_dpapi` is base64 of a **LocalMachine-scope** DPAPI blob with entropy = UTF-8 `'VCP-DPAPI-v1'` (exact bytes, three implementations must match: `setup.ps1:Protect-VcpPassword`, `upload.py:dpapi_unprotect`, `hub/app.py:_dpapi`). Resolution order in `upload.py:get_pdf_password`: printer `password_dpapi` (decrypt failure RAISES = fail closed) → printer `password` → `VCP_PDF_PASSWORD` env → global block. The **super-admin password** is stored ONLY as a PBKDF2-SHA256 record (the factory plaintext appears NOWHERE in the repo — `setup.ps1` embeds only its precomputed hash record `$InitialSuperAdminHash`) (`super_admin`: 200k iterations, 16 B salt) and gates `-Action changepassword/viewpassword/setsuperadmin` — never store or log it in plaintext.
- **`set-id.bat` → `set-id.ps1`** — single print: set a number (with `once:true`), print one document, the number is consumed. Used as the fallback when the prompt is skipped/disabled.
- **`print-register.bat` → `print-register.ps1`** — batch: pick files, give each its own number (or auto-increment), and it prints them **sequentially**, writing each `{id, once:true}` and **polling until the id file disappears** (i.e. `read_pending_id` consumed it) before printing the next. This per-user serialization + once-consume is what guarantees a distinct number per PDF under batch/concurrent use. Because the number cannot be prompted at print time (the uploader runs as **SYSTEM in session 0** and can't show UI, and Windows won't let you set a per-job title generically), this pre-write-then-print model is the reliable path. Both helpers get a public-desktop shortcut via `New-DesktopShortcut` in `Do-Install`.

## Routing: config.json

Everything runs from `%ProgramData%\VirtualCloudPrinter\` (chosen so the SYSTEM-context spooler can read it). `config.json` maps **printer name → URL**; `upload.py` looks up `config["printers"][<%r>]` and falls back to `default_url`. One shared port serves every printer — adding a printer only adds a driver+printer+config entry, never a new port. `config.template.json` is the seed; `setup.ps1` (`Update-Config`) copies it on first install and edits it thereafter via `ConvertFrom-Json`/`ConvertTo-Json`.

## Security / correctness invariants (do not regress)

- **`$Base` ACL is hardened.** `Do-Install` runs `icacls $Base /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F"` because `upload.py` runs as **SYSTEM** from `$Base`; the default `C:\ProgramData` ACL lets any user create files in child folders, which would let a standard user plant a sibling module (`ssl.py`, `json.py`, …) imported ahead of the stdlib → SYSTEM code execution. The `-P` flag on the launch line is the defense-in-depth partner. Keep both.
- **But `spool\` MUST be writable by the print-submitting user** (`Grant-JobDirAccess`). The spooler performs mfilemon's port I/O **impersonating the user who submitted the print job** (print-to-file semantics — confirmed against the Windows DDK and the CVE-2020-1048 "PrintDemon" write-up). If that user can't stat/create in `spool\`, mfilemon's `GetFileAttributes(spool)` fails → `DirectoryExists`→FALSE → `CreateDirectory` on the existing dir returns **`ERROR_ALREADY_EXISTS` (183)** → the job errors and `upload.py` never launches. Tell-tale: prints only reach the endpoint *after* a spooler restart, which reprocesses the retained jobs **as SYSTEM** (the per-job impersonation token is gone). The grant is deliberately minimal — `*S-1-5-11:(CI)(RX,W)` (container-only: stat/list/create, **no** `(OI)` so no ACE lands on the `.ps` files) plus `*S-1-3-0 (CREATOR OWNER):(OI)(CI)F` so each user controls only the file it created (no cross-user disclosure of in-flight documents, important at ~100 users). `failed\` is **not** opened — it is written by `upload.py` as SYSTEM, never by the impersonated user, and holds preserved PDFs. This does **not** regress the sibling-module hardening: `spool\` is not on `upload.py`'s `sys.path` and holds no importable code; only `ids\` and `spool\` are user-writable, and the `$Base` root (upload.py, venv, config.json) stays SYSTEM+Admins-only. `setup.ps1 -Action fixqueue` re-applies this to repair existing installs. **Requires bypass-traverse-checking (SeChangeNotifyPrivilege, on for Everyone by default) so `GetFileAttributes(spool)` can skip the locked `$Base` parent — don't strip it via GPO.**
- **The interpreter (`venv\`, `python\`) is granted Authenticated Users `(OI)(CI)(RX)`** for the same impersonation reason: the spooler also **launches** the UserCommand under the submitting user's token, and `CreateProcess` must *open* `venv\Scripts\pythonw.exe` — locked to SYSTEM+Admins, that open is denied, so the `.ps` is written but `upload.py` **never launches** (no `log.txt`, `.ps` orphaned in spool, no print-queue error; only restart-reprocessed-as-SYSTEM jobs run). **RX is read+execute only — never write** — so it cannot plant an importable module (the escalation needs *write*); `config.json`, `upload.py`, `log.txt` live at the `$Base` root and stay SYSTEM+Admins-only, so auth headers remain unreadable. The launched `upload.py` still runs as SYSTEM (CreateProcess uses the spooler's primary token). Net: the impersonated print thread needs **write on `spool\`** (create the `.ps`) and **read+execute on the interpreter** (launch it); everything else stays locked.
- **config.json must be written BOM-less.** Windows PowerShell 5.1 `Set-Content -Encoding UTF8` prepends a BOM that breaks Python's `json.load`; `Update-Config` uses `[System.IO.File]::WriteAllText(..., UTF8Encoding($false))` and `upload.py` reads with `utf-8-sig`. Don't revert either.

## Hard constraints

- **`upload.py` and `test_server.py` are standard-library only** (no `requests`, no pip). The `uv`-managed venv exists to provide a private Python interpreter, not packages. Keep it that way — the uploader must run under the SYSTEM account with zero install-time package fetches. (The **simulator** under `simulator/` is a separate app and is *not* under this rule — it uses FastAPI/uvicorn.)
- Per the user's global rule, provision Python/venv with **`uv`** (`uv venv`), never pip.
- **The interpreter must live under `$Base`, not in a user profile.** `Do-Install` sets `UV_PYTHON_INSTALL_DIR=$Base\python`, runs `uv python install --install-dir $Base\python --reinstall 3.12`, then builds a **`--relocatable`** venv from that interpreter and **smoke-tests it** (`python -P -c "import ssl, json, …"`). This is required because `upload.py` runs as **SYSTEM**: uv's default managed-Python dir is in the *installing user's* profile (`%AppData%\uv\python`) and can be broken or unreadable for SYSTEM, which makes the port's launch of `pythonw.exe` fail so every print job errors with nothing uploaded. The smoke test surfaces a corrupt interpreter at install time instead of silently per-print.
- `upload.py` must **never raise out to the spooler** and must preserve failed jobs — on upload failure the PDF is kept under `%ProgramData%\VirtualCloudPrinter\failed\` and errors go to `log.txt`.

## The hub (`hub/app.py`) — the central LAN receiver (FastAPI, NOT stdlib-only)

Runs on the central desktop (192.168.1.172), binds `HUB_HOST`/`HUB_PORT` (default `0.0.0.0:8000`). Like `simulator/`, it is exempt from the stdlib-only rule (FastAPI/uvicorn/httpx/python-multipart) and follows the same async discipline (WAL SQLite via `run_in_threadpool`, 1 MiB streamed uploads, threadpool 128). Key facts:

- **State in `hub/data/`** (`HUB_DATA_DIR` overrides for tests): `hub.db`, `held/`, `config.json`, `admin_token.txt`, `enroll_key.txt` (both random, printed at startup). `HUB_LIMSDOCS_DIR` overrides the filing root.
- **Three auth planes:** per-device ingest tokens (URL + `X-Device-Token` on `/catalog`), `X-Enroll-Key` (`/device-types`, `POST /admin/devices` — 409 on case-insensitive duplicate device_name), `X-Admin-Token` (everything under `/api/*` + the dashboard). Only `/healthz` and `GET /` are open.
- **Filing:** `<lims_docs_dir>\<device_name>\<device_type>\<reg_no>\<test>\<reg_no>_<test>_<uuid>.pdf` (via `composed_name()` — the app print title is kept only as `docname` metadata, shown "from: …" in the dashboard). EVERY client-derived segment goes through the single `sanitize_segment()`. The device ROW (not client fields) decides name/type, and `device_name`/`device_type` are **snapshotted onto the `documents` row** at ingest so the name persists after a token is revoked. `documents` also has a `unique index on storage_path`; the Supabase forward is an **upsert** (`on_conflict=storage_path`, `Prefer: resolution=merge-duplicates`) so a retry never double-inserts.
- **Dashboard:** timestamps stored UTC, shown in **IST** (Asia/Kolkata); "Printed by" = `%u`; docs/held 3 s auto-refresh **pauses while a control is focused/recently-touched** so it can't reset an in-progress Assign. Device types are managed in the Catalog tab via admin-token `POST/DELETE /api/device-types` (local mode; Supabase sync overwrites). The legacy `simulator/` now defaults to **port 8001** so it can't collide with the hub on 8000.
- **Held:** any of `missing_registration / unknown_registration / device_type_mismatch / missing_test / unknown_test` → PDF stored under `data/held/` (never in the share), status `held`, still HTTP 2xx. `POST /api/documents/{id}/assign` validates, moves into the tree, enqueues the forward.
- **Supabase sync:** poll `POST /rest/v1/rpc/catalog_version` every `poll_seconds` (default 2); on change refetch registrations(+tests)+device_types, replace the cache in one transaction, and atomically export `<lims_docs_dir>\.vcp\catalog\<type>.json` + `all.json` (`.tmp` + `os.replace`). Forwarding: Storage upload (`x-upsert: false`, 409 = already uploaded = success; `storage_path` fixed at enqueue so retries hit the same object) then `POST /rest/v1/documents`; exponential backoff 5 s→300 s, never gives up. No Supabase configured = **local mode**: catalog managed via `/api/registrations`, forward queue idles and drains later. The service key is stored DPAPI-encrypted (machine scope, same entropy contract) on Windows; `SUPABASE_SERVICE_KEY` env overrides.

## The simulator (`simulator/app.py`) — async, sized for ~100 concurrent users

The receiver simulator is the local stand-in for the real endpoint; it is what all the print jobs POST to concurrently, so it is the tier that must scale. It is a **FastAPI** app run by uvicorn and is fully non-blocking:

- **No blocking work on the event loop.** Every SQLite call and file write is offloaded via `starlette.concurrency.run_in_threadpool` (the `run_db` helper). Uploads are **streamed to disk in 1 MiB chunks** (`_stream_to_disk`) rather than `read()` into memory, so 100 concurrent PDFs don't balloon RAM. Don't reintroduce a synchronous `db()`/`open().write()` inside an `async def`.
- **SQLite is set to WAL + `busy_timeout=30s`** (and `synchronous=NORMAL`) so concurrent readers/writers don't raise "database is locked".
- The anyio threadpool ceiling is raised to `THREADPOOL_TOKENS = 128` (from the default 40) in the `lifespan` handler so a burst of ~100 uploads runs in parallel.
- Binds **`127.0.0.1`** by default (openable in a browser; override with `SIM_HOST`/`SIM_PORT`). It is HTTP for dev — real confidential documents require HTTPS/TLS in front (see `simulator/README.md`).

## Optional AES-256 PDF encryption

Off by default. When `config.json` `pdf_encryption.enabled` is true, `upload.py`
encrypts each PDF with **AES-256 (AESv3)** *after* the Ghostscript conversion, in a
separate **qpdf** step (`encrypt_pdf` → `qpdf --encrypt pw pw 256 -- --replace-input`).
Ghostscript is **not** used for encryption — its pdfwrite only supports weak RC4
(revisions 2/3); qpdf does real AES-256 and is lossless (no recompression, so full
quality is preserved). qpdf is installed like the other tools (`Get-Qpdf`: GitHub
release zip → `vendor\qpdf-*.zip` → winget) into a private `%ProgramData%\…\qpdf\`,
and `upload.py:find_qpdf()` resolves it via `config.qpdf_path` → PATH → that folder.
qpdf runs as SYSTEM (invoked by `upload.py`, not the impersonated thread), so it needs
no extra ACL. The passphrase is set most reliably with **`set-password.bat`** (`-Action setpassword`,
elevated) which writes `pdf_encryption.enabled`+`password_dpapi` (DPAPI at rest, same
contract as per-printer) and ensures qpdf; it can
also come from the **`VCP_PDF_PASSWORD` machine env var** (must be `/M`/machine scope
— SYSTEM can't see a user var — and needs a spooler restart) or directly from
`pdf_encryption.password`. Encryption is ON if the config flag is true **or** the env
var is present (so the env var alone enables it). **Fail closed:** if encryption is enabled but no passphrase
is set, or qpdf is missing, `get_pdf_password`/`encrypt_pdf` raise → the job is
preserved in `failed\`, never uploaded in the clear. Decrypt with `decode-pdf.bat`
(qpdf-on-PATH → Ghostscript fallback) or any PDF reader + the password.

## Commands

```bash
# Syntax-check the Python (works anywhere)
python3 -m py_compile upload.py test_server.py

# Local end-to-end test of the HTTP contract (no Windows needed):
python3 test_server.py 8000        # terminal 1 — prints received docname + saves PDF to ./received/
#   then point a printer/config URL at http://localhost:8000/upload and print,
#   or drive upload.py's post_pdf()/build_multipart() directly against it.
```

```powershell
# Windows install/manage. install.bat / add-printer.bat elevate and open the GUI
# wizard (setup-gui.ps1), which collects the fields and runs setup.ps1 fully
# parameterized (-NoPassword marks "explicitly no PDF password" so no console
# prompt remains). The other .bat wrappers still call setup.ps1 directly:
powershell -ExecutionPolicy Bypass -File setup.ps1 -Action install                     # deps + first printer (no -Url => hub mode: prompts hub URL/device name/type/enroll key/password)
powershell -ExecutionPolicy Bypass -File setup.ps1 -Action add -PrinterName N -Url U    # add a LEGACY direct-URL printer
powershell -ExecutionPolicy Bypass -File setup.ps1 -Action add -PrinterName N -HubUrl H -DeviceName D -DeviceType T -EnrollKey K -Password P -CatalogShareDir S   # scripted hub enrollment (no prompts)
powershell -ExecutionPolicy Bypass -File setup.ps1 -Action status                       # monitor/printers/devices/log tail
powershell -ExecutionPolicy Bypass -File setup.ps1 -Action fixqueue                     # unjam queue + full diagnostics
powershell -ExecutionPolicy Bypass -File setup.ps1 -Action setpassword                  # set/clear the GLOBAL AES-256 PDF passphrase
powershell -ExecutionPolicy Bypass -File setup.ps1 -Action changepassword -PrinterName N # per-printer password (super-admin gated, DPAPI)
powershell -ExecutionPolicy Bypass -File setup.ps1 -Action viewpassword   -PrinterName N # reveal a printer's password (super-admin gated)
powershell -ExecutionPolicy Bypass -File setup.ps1 -Action setsuperadmin                 # rotate the super-admin password
powershell -ExecutionPolicy Bypass -File setup.ps1 -Action uninstall                    # remove printers/port/files/shortcuts
```

```bat
:: Central hub (on 192.168.1.172) + cloud LIMS web app:
cd hub  && run.bat            &REM prints ADMIN TOKEN + ENROLL KEY on first start
cd lims\web && npm install && npm run dev   &REM needs .env with the Supabase URL + anon key
```

`setup.ps1` requires Administrator (the `.bat` files self-elevate via UAC) and internet on first run (fetches `uv`, Ghostscript, mfilemon). `-Action status` and reading `%ProgramData%\VirtualCloudPrinter\log.txt` are the primary debugging tools; switch the port's `UserCommand` from `pythonw.exe` to `python.exe` to see a job run in a console. **`-Action fixqueue`** (`fix-queue.bat`) is the go-to repair: it refreshes `upload.py`/`prompt-id.ps1`, re-applies the print-user ACLs (spool write + interpreter/prompt RX), restarts the spooler to clear poison jobs, enables mfilemon DEBUG logging, and writes a full report to `vcp-diagnostics.txt` in the repo (the one SYSTEM-side dump readable without elevation). `SETUP.md` is the clone-to-running guide; `README.md` is the user-facing overview.

## Debugging print jobs

Every job writes a block to `log.txt`. If nothing reaches the URL: confirm the printer name exactly matches a `config.json` key (`%r` is case/space-sensitive), confirm `ghostscript_path` resolves, and check `failed/` for preserved PDFs.
