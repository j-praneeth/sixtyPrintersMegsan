# Setup Guide — from `git clone` to a working printer

> **Deploying the LAN → LIMS pipeline** (offline client PCs → central hub on
> 192.168.1.172 → limsDocs share → cloud LIMS)? That is the primary mode now —
> follow **[LAN-SETUP.md](LAN-SETUP.md)** instead. This guide covers a single
> machine with the legacy direct-URL receiver, which still works unchanged; the
> install prompts differ only in that hub mode asks for a device name/type,
> enroll key and per-printer PDF password instead of a URL.

Follow this top-to-bottom on a fresh **Windows 10/11 (64-bit)** machine. It takes
you from cloning the repo to printing a document that arrives as a PDF (with a
registration number) in the receiver.

> **What you'll end up with**
> ```
> Any app ─Print▶ your printer ─(v3 PostScript driver)▶ mfilemon port
>    ▶ upload.py (as SYSTEM): PostScript→PDF (Ghostscript), look up the URL by
>      printer name, prompt you for a registration number, HTTPS POST ▶ your URL
> ```

---

## 0. Prerequisites

| Need | Notes |
|---|---|
| **Windows 10/11 x64** | the printer/port/driver are Windows-only |
| **Administrator rights** | `install.bat` self-elevates (UAC) |
| **[uv](https://docs.astral.sh/uv/)** | provides the private Python; install: `winget install astral-sh.uv` (or see the uv site) |
| **Git** | to clone — <https://git-scm.com/download/win> (or download the repo ZIP) |
| **Internet (first run only)** | fetches Ghostscript + the mfilemon port monitor. Fully offline works if `vendor\mfilemon-setup.exe` is present in the repo. |

Everything else (the Python interpreter, the print driver, the port monitor) is
installed automatically by `install.bat`.

---

## 1. Clone the repo

```bat
git clone https://github.com/arupa444/virtualPrinterMegsan.git
cd virtualPrinterMegsan
```

---

## 2. Start the receiver

You need a URL for the printer to POST to. For local testing, use the bundled
**simulator**; for production, use your real HTTPS endpoint and skip to step 3.

```bat
cd simulator
run.bat
```

`run.bat` builds a venv with `uv`, installs FastAPI, and starts the server:

```
ADMIN TOKEN: <copy this>
Uvicorn running on http://127.0.0.1:8000
```

1. Open **http://localhost:8000/** (use `localhost`/`127.0.0.1` — **not** `0.0.0.0`).
2. Paste the **ADMIN TOKEN** (top-right) → **Save**.
3. Type a label (e.g. `LIMS`) → **+ Create link**.
4. **Copy the whole ingest URL**, e.g. `http://127.0.0.1:8000/ingest/<token>` — the
   token *is* the API key, so keep it intact.
5. Leave this window running. (More detail: [simulator/README.md](simulator/README.md).)

---

## 3. Install the printer  *(Administrator)*

Back in the repo root, **double-click `install.bat`** and approve the UAC prompt
(or run `powershell -ExecutionPolicy Bypass -File setup.ps1 -Action install`).

It prompts for two things:
- **Printer name** — how it appears in the Windows print dialog (e.g. `limsPrinter`).
- **Target URL** — paste the **full ingest URL from step 2** (with its token), or
  your real HTTPS endpoint.

Then it does everything automatically:
- creates `%ProgramData%\VirtualCloudPrinter\` and **hardens its ACL** (SYSTEM +
  Administrators), while opening exactly what the print pipeline needs;
- installs a **private CPython 3.12** under that folder, builds a relocatable venv,
  and **smoke-tests** it;
- ensures **Ghostscript** and the **mfilemon** port monitor (from the author's
  GitHub releases, or the offline copy in `vendor\`);
- creates the shared **redirection port** and a printer using a **v3 PostScript
  driver** (`MS Publisher Color Printer`);
- writes `config.json` (printer name → URL) and drops the **Set Print ID** and
  **Print & Register** desktop shortcuts.

It finishes with `DONE.` and the target URL.

---

## 4. Registration numbers (one per PDF)

The number is supplied by the printing user and sent to your endpoint as the
`registration_number` form field. There are three ways — pick what fits:

- **Automatic prompt (default).** Every time you print, a **"Registration number"**
  dialog pops up — type a value (or **New UUID**) → **Attach & print**. Click
  **Skip** to send without one. Toggle off with `"prompt_registration": false` in
  `config.json`.
- **Set Print ID (single, before printing).** Double-click **Set Print ID** (desktop)
  or `set-id.bat`, enter a number, **Save**, then print. Used as the fallback when
  the prompt is skipped/disabled.
- **Print & Register (batch).** Double-click **Print & Register** or
  `print-register.bat`, pick multiple files, give each its own number (or
  prefix + auto-increment), **Print all** — each PDF gets its own number.

---

## 5. Print and verify

1. Open any app → **Print** → choose your printer (e.g. `limsPrinter`).
2. Answer the registration dialog.
3. In the simulator dashboard → **Refresh** → click the **Files** count on your
   link → you'll see the document name, **registration number**, size, timestamp,
   with **download** / **delete**.

To confirm on the machine: `status.bat`, or `%ProgramData%\VirtualCloudPrinter\log.txt`
(shows a block per job ending in `Upload OK`).

---

## 5a. Encrypt the PDFs (optional, AES-256)

PDFs are sent unencrypted by default. To encrypt every PDF with **AES-256** (via
qpdf — **no quality loss**, it doesn't recompress):

**Easiest — `set-password.bat`:** double-click it, approve the UAC prompt, and type
a passphrase (blank = turn encryption off). It records the passphrase, enables
encryption, and ensures qpdf is installed — all in one step, no spooler restart, no
editing the locked config. *(If qpdf isn't installed yet, run `install.bat` once
first, or let `set-password.bat` fetch it.)*

<details><summary>Advanced: set it yourself instead</summary>

- **Machine env var:** `setx VCP_PDF_PASSWORD "<long random passphrase>" /M`
  (the **`/M`** is required — `upload.py` runs as SYSTEM and only sees *machine*
  variables; a plain `setx` sets a *user* variable it can't see). Then restart the
  spooler with `fix-queue.bat` so the SYSTEM print process picks it up.
- **Config:** set `"pdf_encryption": { "enabled": true, "password": "…" }` from an
  elevated editor.
</details>

Print as usual → the received PDF is AES-256 encrypted and requires the passphrase
to open. **Decode** with **`decode-pdf.bat`** (pick/drag the file, enter the
password → `<name>-decrypted.pdf`), or just open it in any PDF reader with the
password (lossless, no tool).

**How to confirm it's actually encrypted** (do this on a PDF printed *after*
enabling): open it in a PDF reader — it should **prompt for a password**. If it
opens with no prompt, it isn't encrypted. Also, `log.txt` logs
`Encrypted PDF with AES-256 (qpdf)` per job, and `qpdf --show-encryption <file>`
prints `file encryption method: AESv3`. (PDFs printed *before* you enabled it stay
plaintext — reprint to test.)

> **Fail-safe:** if encryption is enabled but the passphrase or qpdf is missing,
> jobs are preserved in `failed\` — never sent in the clear. Strength depends on the
> passphrase; use a long random one and keep it in a secrets manager.

## 6. Manage

| Task | How |
|---|---|
| Add another printer → its own URL | `add-printer.bat` |
| Show monitor / printers / URLs / log tail | `status.bat` |
| **Unjam the queue + full diagnostics** | `fix-queue.bat` (self-elevating) |
| Remove printers, port, files, shortcuts | `uninstall.bat` |

---

## 7. Configuration — `config.json`

Lives at `%ProgramData%\VirtualCloudPrinter\config.json` (edit from an **elevated**
editor — the folder is locked). Changes apply to the next print; no reinstall.

```jsonc
{
  "printers": {
    "limsPrinter": {                         // MUST match the Windows printer name
      "url": "http://127.0.0.1:8000/ingest/<token>",
      "docname_field": "docname",
      "file_field": "file",
      "extra_fields": {},
      "headers": { },                        // e.g. { "Authorization": "Bearer ..." }
      "verify_tls": true                     // false only for self-signed test certs
    }
  },
  "default_url": "",
  "registration_field": "registration_number",
  "prompt_registration": true,               // pop the per-print dialog
  "verify_tls": true,
  "timeout_seconds": 60,
  "retry_count": 2,
  "retry_delay_seconds": 3,
  "ghostscript_path": "C:\\Program Files\\gs\\gs10.07.1\\bin\\gswin64c.exe"
}
```

**Your server receives** a `multipart/form-data` POST: `docname` (the document
name), `file` (the PDF, `application/pdf`), and `registration_number` when set.

---

## 8. Troubleshooting

**Always diagnose as Administrator** — the install folder is locked to
SYSTEM+Admins, and spooler control needs elevation. Use **`fix-queue.bat`**
(self-elevating): it clears a stuck queue, enables mfilemon debug logging, and
writes a full report to **`vcp-diagnostics.txt`** in the repo (interpreter check,
ACLs, `config.json`, `log.txt` tail, `failed\`).

| Symptom | Fix |
|---|---|
| Jobs stuck "Error/Printing"; nothing uploads | run **`fix-queue.bat`** (restarts the spooler, re-applies the ACLs the pipeline needs) |
| Nothing reaches the URL | printer name must **exactly** match a `config.json` key; confirm the token in the ingest URL; check `failed\` |
| No registration dialog | ensure `"prompt_registration": true`; needs an interactive login session; check `log.txt` for a `Registration prompt: …` line |
| `Ghostscript not found` | set `ghostscript_path` to the full `gswin64c.exe` path |
| Huge/slow jobs | the MS Publisher driver defaults to a high DPI; lower the printer's default resolution in its Printing Preferences |

> **Driver note:** the printer must use a **v3 PostScript** driver (`MS Publisher …`).
> A v4 class driver (e.g. "Microsoft PS Class Driver") **cannot** bind to the
> mfilemon port — Windows rejects it with *"may not be used in conjunction with a
> non-inbox port monitor."* `install.bat` already picks the right one.

---

## 9. Start over (clean slate)

1. Stop the simulator (Ctrl+C in its window).
2. **Double-click `uninstall.bat`** → removes the printers, port,
   `%ProgramData%\VirtualCloudPrinter\`, and the desktop shortcuts.
3. Wipe the simulator's received data: `Remove-Item -Recurse -Force .\simulator\data`
   (recreated on next `run.bat`).

Then repeat from step 2. (Ghostscript, uv, and mfilemon are left installed for
reuse — a fresh install is fast.)

---

## 10. Production / confidential documents

The simulator is **HTTP, for development only**. Before real use, read
[COMPLIANCE-AND-PRIVACY.md](COMPLIANCE-AND-PRIVACY.md) (Ghostscript licensing,
retention, privacy) and:

- point printers at an **HTTPS** endpoint with `verify_tls: true`;
- put any auth token in the printer's `headers` (the config file is readable only
  by SYSTEM/Administrators);
- the receiver simulator, if used at all, belongs behind a TLS reverse proxy.
