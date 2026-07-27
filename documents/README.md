# LIMS Print Pipeline — documentation bundle

Everything you need to understand, deploy, operate, and test the pipeline. Start
with the illustrated manual; the others go deeper.

## PDFs (read in this order)

| File | What it is | Read it when |
|---|---|---|
| **LIMS-Pipeline-Illustrated-Manual.pdf** | Step-by-step manual with **real screenshots** of the hub dashboard, the install wizard, and the print dialog. Covers every part end-to-end. | You are setting up or operating the system. **Start here.** |
| **LIMS-Pipeline-Operations-Manual.pdf** | Text reference: every command, every wizard field, every value, in order. | You want a no-pictures checklist / quick lookup. |
| **LIMS-Pipeline-Reference.pdf** | Architecture, data-flow diagrams, the security model, and a from-fresh test chart. | You want to understand *how* it works or review security. |
| **LIMS-Pipeline-Overview.pdf** | Two-page high-level summary. | You are briefing a stakeholder. |
| **LIMS-Supabase-Setup.pdf** | Step-by-step: create the Supabase project, wire the hub to it, deploy the tester web app. | You are connecting the cloud LIMS. |

## Companion markdown (also copied here)

| File | What it is |
|---|---|
| `LAN-SETUP.md` | The deployment runbook (cloud → hub → 60 clients). |
| `PRODUCTION.md` | Running the hub 24/7 as a service, hiding tokens, session timeout, HTTPS. |
| `LIMS-Integration-Guide.md` | **Endpoints & data flow: send to LIMS, fetch from LIMS, query the hub** — with request/response examples for building your own endpoint. |
| `ARCHITECTURE.md` | The exact interface contracts between client, hub, and cloud. |
| `README.md.project` | The project overview / file map (repo root README). |
| `SETUP.md` | Single-machine / legacy clone-to-running guide. |

## What's current in this edition

- **GUI install wizard** (`install.bat` / `add-printer.bat`) — click-through, paste-friendly.
- **Documents are named `<registration>_<test>_<id>.pdf`** (the app print title is kept as metadata).
- **Device name persists** on each document even after the device is revoked.
- **Times shown in IST**; the "Printed by" column; docs/held auto-refresh **pauses while you interact**.
- **Device types** are managed in the Catalog tab (add/remove).
- Per-printer & global PDF passwords are **DPAPI-encrypted**; the wizard passes them via a locked file, never on the command line.
- Supabase forwarding is **idempotent** (no duplicate cloud rows on retry).
- The legacy `simulator/` now runs on **port 8001** (the hub owns 8000).

> Screenshots in the illustrated manual are genuine captures of the running hub
> dashboard and the actual WinForms install/print dialogs.
