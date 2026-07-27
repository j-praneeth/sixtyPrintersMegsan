# Running the LIMS Print Hub in production (24/7)

The hub is the one always-on service on the central desktop (`192.168.1.172`). This
guide takes it from "works in a console" to "runs as a hardened, auto-restarting
service that survives reboots, with no secrets on screen."

---

## 1. Where it should live (the production path)

Don't run it from `Downloads`. Put it on a stable path, e.g.:

```
C:\LIMSHub\
   app.py, requirements.txt, run.bat, install-hub-service.ps1, .venv\, data\
```

Steps:
1. Copy the repo's `hub\` folder to **`C:\LIMSHub\`**. Do this while **no hub is
   running** (otherwise a locked `.venv\` is skipped by the copy) — or don't copy
   `.venv\` at all; the installer rebuilds it.
2. You do **not** need to run `run.bat` first. `install-hub-service.bat` (§2) builds
   the `.venv\` itself if it is missing or broken — from a private CPython under
   `C:\LIMSHub\python` so the LocalSystem service can run it. (`run.bat` still works
   for an interactive dev run and also creates `data\` + prints the tokens.)
3. Create the shared documents folder and share it on the LAN:
   ```
   mkdir C:\limsDocs
   ```
   Right-click `C:\limsDocs` → Properties → Sharing → share as `limsDocs`, grant the
   lab user group **Read** (they only need to read the catalog fallback; the hub
   writes locally). Clients then see `\\192.168.1.172\limsDocs`.
4. Dashboard → **Settings**: set `limsDocs directory = C:\limsDocs`, and (for cloud
   mode) the Supabase URL + service-role key. Save.

## 2. Install it as a 24/7 service

Double-click **`install-hub-service.bat`** (it self-elevates). This registers a
Scheduled Task that:
- **auto-starts at boot** (before/without any login — no console window to close),
- runs **headless as LocalSystem**,
- **auto-restarts** within 1 minute if it ever crashes,
- has **no run-time limit**.

Manage it:
```
:: status + health
powershell -ExecutionPolicy Bypass -File install-hub-service.ps1 -Action status
:: remove
powershell -ExecutionPolicy Bypass -File install-hub-service.ps1 -Action uninstall
:: custom port / bind address
powershell -ExecutionPolicy Bypass -File install-hub-service.ps1 -Action install -Port 8000 -BindHost 0.0.0.0
```

Logs go to `data\service.log` (rotate/delete periodically — see §6). After install
you can reboot the machine and the hub comes back on its own.

## 3. Secrets are no longer printed

In service mode the hub runs **quiet**: the startup banner shows
`ADMIN TOKEN: (hidden ...)` instead of the value, so nothing sensitive lands in
`service.log`. Retrieve the secrets when you need them:
- **Admin token** — `data\admin_token.txt` (Administrators-only; the installer locks
  the `data\` folder to SYSTEM + Administrators).
- **Enroll key** — `data\enroll_key.txt`, or in the dashboard: **Devices → Show enroll key**.

To inject your own secrets instead of the generated ones (e.g. from a secrets
manager), set machine environment variables before the service starts:
```
setx HUB_ADMIN_TOKEN "<your-strong-token>" /M
setx HUB_ENROLL_KEY  "<your-strong-key>"   /M
```
(`HUB_QUIET=1` is already forced by the service launcher.)

## 4. Dashboard session timeout

The admin dashboard now **auto-expires after 20 minutes of inactivity**: the token is
cleared from the browser and re-entry is required, so an unattended dashboard can't be
used by a walk-up. Any click/keypress renews the 20-minute window.

> The admin token itself is the root secret (in `data\`, Administrators-only). The
> timeout protects an *open browser session*; keep the token file locked and rotate it
> (`HUB_ADMIN_TOKEN`) if it is ever exposed.

## 5. Put it behind HTTPS (recommended)

The dashboard and the per-device ingest tokens travel in HTTP headers. On a trusted,
isolated LAN plain HTTP is often accepted, but for confidential lab documents use TLS:
- Easiest: front the hub with a local reverse proxy (IIS ARR, nginx, or Caddy) that
  terminates HTTPS on 443 and forwards to `127.0.0.1:8000`. Point clients' hub URL at
  `https://192.168.1.172` and set the printer's `verify_tls` appropriately (use a cert
  the clients trust, or an internal CA).
- Keep port 8000 bound to the LAN and open **only** 8000 (or 443 via the proxy) in
  Windows Firewall:
  ```
  netsh advfirewall firewall add rule name="LIMS Print Hub 8000" dir=in action=allow protocol=TCP localport=8000
  ```

## 6. Backups, retention & monitoring

- **Back up** `C:\LIMSHub\data\` (SQLite DB + held PDFs + tokens + config) and
  `C:\limsDocs\` (the filed documents) on your normal schedule.
- **service.log** grows over time — truncate or rotate it (e.g. a weekly scheduled
  task that renames/deletes it); the hub reopens it on restart.
- **Health check**: `GET http://192.168.1.172:8000/healthz` returns `ok`. Point your
  monitoring at it, or run `install-hub-service.ps1 -Action status`.
- **Supabase forwarding**: watch the Documents tab for `forward_failed` rows (it
  retries forever with backoff); a persistent failure means the internet/keys need
  attention.

## 7. Production checklist

- [ ] Hub copied to `C:\LIMSHub\`. (The venv is auto-built by `install-hub-service.bat` — no `run.bat` needed.)
- [ ] `C:\limsDocs` created and shared (Read) to lab users.
- [ ] Settings: limsDocs dir + (optional) Supabase URL/service key set.
- [ ] `install-hub-service.bat` run → `-Action status` shows Running + health ok.
- [ ] Rebooted once; confirmed the hub came back automatically.
- [ ] Firewall allows inbound 8000 (or 443 via HTTPS proxy).
- [ ] Super-admin password rotated on every client (`set-super-admin.bat`).
- [ ] `data\` backed up; `service.log` rotation scheduled.
- [ ] (Confidential data) HTTPS in front of the hub.
