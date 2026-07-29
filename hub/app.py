#!/usr/bin/env python3
r"""
LIMS Print Hub (FastAPI) - the central receiver on 192.168.1.172
================================================================

Every client PC's virtual printer POSTs its PDFs here (multipart, per-device
token in the URL). The hub:

  1. Enrolls devices (unique device_name; each bound to a department + equipment)
     and issues per-printer ingest tokens.
  2. Validates each job's registration_number + test_method + test_parameter
     against the printer_data cache; valid jobs are FILED into the limsDocs tree
         <lims_docs_dir>\<department>\<equipment>\<reg_no>\<method>\<parameter>\...
     invalid/missing ones are HELD under hub/data/held/ until an operator
     assigns them in the dashboard. Held is a 2xx to the client: the job is safe.
  3. Serves the printer_data catalog to clients (GET /catalog, device token)
     and exports it to <lims_docs_dir>\.vcp\catalog\*.json as the share fallback.
  4. Syncs the catalog FROM Supabase (poll printer_data_version ~2 s) and forwards
     every filed PDF TO Supabase (Storage + documents row) via a retry queue.
     No Supabase configured => local mode: catalog is managed in the dashboard,
     the forward queue idles, nothing breaks.

Async discipline mirrors simulator/app.py: WAL SQLite via run_in_threadpool,
uploads streamed to disk in 1 MiB chunks, threadpool ceiling raised for burst
load. Interface contracts: ARCHITECTURE.md sections 6, 8, 9, 10, 11.
"""

import os
import re
import sys
import json
import time
import uuid
import base64
import shutil
import asyncio
import hashlib
import logging
import sqlite3
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

import ocr  # Google Vision Sample Set ID / batch-From extraction (hub-side)

# Same sizing rationale as simulator/app.py: a burst of ~60-100 concurrent
# uploads must not queue on anyio's default threadpool limit of 40. Each op is
# short (small INSERT or a streamed chunk copy), so the loop never blocks.
THREADPOOL_TOKENS = 128
STREAM_CHUNK = 1 << 20  # 1 MiB - stream uploads to disk instead of buffering

# DPAPI entropy contract (ARCHITECTURE.md section 4) - must match setup.ps1.
DPAPI_ENTROPY = b"VCP-DPAPI-v1"

DEVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HELD_REASONS = ("missing_registration", "missing_method", "missing_test",
                "unknown_registration")

log = logging.getLogger("hub")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# --------------------------------------------------------------------------- #
# Paths & storage (HUB_DATA_DIR override lets tests use a scratch directory)
# --------------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("HUB_DATA_DIR", "").strip() or os.path.join(BASE_DIR, "data")
HELD_DIR = os.path.join(DATA_DIR, "held")
TMP_DIR = os.path.join(DATA_DIR, "tmp")
# Internal staging for filed PDFs when no limsDocs directory is configured: the
# file lives here only until it is forwarded to Supabase, then it is deleted. No
# department/equipment/... folder tree is created anywhere on disk.
OUTBOX_DIR = os.path.join(DATA_DIR, "outbox")
DB_PATH = os.path.join(DATA_DIR, "hub.db")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
ADMIN_TOKEN_FILE = os.path.join(DATA_DIR, "admin_token.txt")
ENROLL_KEY_FILE = os.path.join(DATA_DIR, "enroll_key.txt")

def ensure_dir(p):
    """Create a directory, tolerating Windows quirks. os.makedirs(exist_ok=True) can
    still raise FileExistsError (WinError 183) on Windows when the dir already exists
    but its isdir() check transiently fails - e.g. two hub processes starting at once
    on the same data dir, or an ACL that makes stat() fail for the caller. We treat
    "already there" as success and only fail on a real file-in-the-way collision."""
    if os.path.isdir(p):
        return
    try:
        os.makedirs(p, exist_ok=True)
    except FileExistsError:
        if os.path.isfile(p):
            raise RuntimeError("%r exists as a FILE but must be a directory - remove it." % p)
        # Exists but not confirmable as a dir (race / ACL): assume dir, proceed.
    except PermissionError:
        pass  # a locked existing dir; the service (SYSTEM) can still read/write it


ensure_dir(HELD_DIR)
ensure_dir(TMP_DIR)
ensure_dir(OUTBOX_DIR)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _load_or_create_secret(path, env=None):
    """Secret resolution order: env var (production secret injection) -> persisted
    file (generated + stored on first run). Files live in hub/data/, which the
    operator locks to Administrators (see PRODUCTION.md)."""
    if env:
        v = os.environ.get(env, "").strip()
        if v:
            return v
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            tok = fh.read().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(24)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(tok)
    return tok


ADMIN_TOKEN = _load_or_create_secret(ADMIN_TOKEN_FILE, "HUB_ADMIN_TOKEN")
ENROLL_KEY = _load_or_create_secret(ENROLL_KEY_FILE, "HUB_ENROLL_KEY")

# Production/service runs are non-interactive: never echo the secret VALUES to the
# console (they would land in the service log). Auto-on when stdout is not a TTY
# (i.e. redirected by the service) or when HUB_QUIET is set. Interactive dev keeps
# the convenient full print. Secrets are always retrievable from hub/data/*.txt
# (admins) or the dashboard ("Show enroll key").
def _quiet_mode():
    if os.environ.get("HUB_QUIET", "").strip():
        return True
    try:
        return not sys.stdout.isatty()
    except Exception:
        return True


# --------------------------------------------------------------------------- #
# Config (hub/data/config.json)
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG = {
    "lims_docs_dir": "",
    "supabase_url": "",
    # Service key at rest: DPAPI (machine scope) on Windows; the plain field is
    # the non-Windows dev fallback. SUPABASE_SERVICE_KEY env var overrides both.
    "supabase_service_key_dpapi": "",
    "supabase_service_key": "",
    "bucket": "printer-documents",
    "poll_seconds": 2,
    # Self-hosted Supabase database credentials (Secret key section)
    "supabase_db_host": "",
    "supabase_db_port": 5432,
    "supabase_db_name": "",
    "supabase_db_user": "",
    "supabase_db_password_dpapi": "",
    "supabase_db_password": "",
    # Application variables for self-hosted Supabase
    "supabase_publishable_key_dpapi": "", # SUPABASE_PUBLISHABLE_KEY (DPAPI at rest)
    "supabase_publishable_key": "",       # SUPABASE_PUBLISHABLE_KEY (non-Windows dev)
    "supabase_session_secret_dpapi": "", # SESSION_SECRET_CURRENT (DPAPI at rest)
    "supabase_session_secret": "",       # SESSION_SECRET_CURRENT (non-Windows dev)
    # Google Cloud Vision OCR (Sample Set ID + batch "From" extraction). The
    # service-account JSON is stored DPAPI-encrypted at rest (same contract as the
    # service key); the plain field is the non-Windows dev fallback. The
    # GOOGLE_OCR_CREDENTIALS env var overrides both.
    "google_ocr_enabled": False,
    "google_ocr_credentials_dpapi": "",
    "google_ocr_credentials": "",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8-sig") as fh:
                cfg.update(json.load(fh))
        except Exception as exc:
            log.error("config.json unreadable (%s); using defaults", exc)
    return cfg


def save_config(cfg):
    # Atomic + BOM-less UTF-8 (Python never writes a BOM, but keep the .tmp +
    # replace so a crash mid-write can't corrupt the config).
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, CONFIG_PATH)


CONFIG = load_config()
if not os.path.isfile(CONFIG_PATH):
    save_config(CONFIG)


def lims_docs_dir():
    """The local limsDocs share root, or "" when not configured. When empty, no
    local folder tree is created — filed PDFs stage in OUTBOX_DIR until forwarded
    to Supabase, and the catalog share export is skipped."""
    return (os.environ.get("HUB_LIMSDOCS_DIR", "").strip()
            or (CONFIG.get("lims_docs_dir") or "").strip())


def poll_seconds():
    try:
        return max(1.0, float(CONFIG.get("poll_seconds") or 2))
    except (TypeError, ValueError):
        return 2.0


# --------------------------------------------------------------------------- #
# DPAPI (Windows only; ARCHITECTURE.md section 4 contract)
# --------------------------------------------------------------------------- #
def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]

    def blob(b):
        buf = ctypes.create_string_buffer(b, len(b))
        return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.c_void_p)), buf

    kernel32 = ctypes.windll.kernel32
    # 64-bit pointers: without explicit argtypes ctypes truncates to c_int.
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    inp, _kb = blob(data)
    ent, _ke = blob(DPAPI_ENTROPY)
    out = DATA_BLOB()
    # CRYPTPROTECT_UI_FORBIDDEN | CRYPTPROTECT_LOCAL_MACHINE - SYSTEM and any
    # admin on this machine can decrypt; no user-profile dependency.
    flags = 0x1 | 0x4
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, ctypes.byref(ent), None, None, flags, ctypes.byref(out)):
        raise OSError("DPAPI %s failed" % ("protect" if protect else "unprotect"))
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def dpapi_protect_b64(text):
    return base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")


def dpapi_unprotect_b64(blob_b64):
    return _dpapi(base64.b64decode(blob_b64), False).decode("utf-8")


def get_service_key():
    """Env var overrides config; DPAPI blob is the Windows at-rest form."""
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if key:
        return key
    key = (CONFIG.get("supabase_service_key") or "").strip()
    if key:
        return key
    blob = (CONFIG.get("supabase_service_key_dpapi") or "").strip()
    if blob:
        try:
            return dpapi_unprotect_b64(blob)
        except Exception as exc:
            log.error("DPAPI decrypt of service key failed: %s", exc)
    return ""


def supabase_configured():
    return bool((CONFIG.get("supabase_url") or "").strip() and get_service_key())


def get_db_password():
    """Resolve DB password: plain field (dev) or DPAPI blob (Windows at-rest)."""
    pw = (CONFIG.get("supabase_db_password") or "").strip()
    if pw:
        return pw
    blob = (CONFIG.get("supabase_db_password_dpapi") or "").strip()
    if blob:
        try:
            return dpapi_unprotect_b64(blob)
        except Exception as exc:
            log.error("DPAPI decrypt of DB password failed: %s", exc)
    return ""


def get_publishable_key():
    """Resolve SUPABASE_PUBLISHABLE_KEY: plain field (dev) or DPAPI blob (Windows at-rest)."""
    val = (CONFIG.get("supabase_publishable_key") or "").strip()
    if val:
        return val
    blob = (CONFIG.get("supabase_publishable_key_dpapi") or "").strip()
    if blob:
        try:
            return dpapi_unprotect_b64(blob)
        except Exception as exc:
            log.error("DPAPI decrypt of publishable key failed: %s", exc)
    return ""


def get_session_secret():
    """Resolve SESSION_SECRET_CURRENT: plain field (dev) or DPAPI blob (Windows at-rest)."""
    sec = (CONFIG.get("supabase_session_secret") or "").strip()
    if sec:
        return sec
    blob = (CONFIG.get("supabase_session_secret_dpapi") or "").strip()
    if blob:
        try:
            return dpapi_unprotect_b64(blob)
        except Exception as exc:
            log.error("DPAPI decrypt of session secret failed: %s", exc)
    return ""


def get_google_ocr_credentials():
    """Resolve the Google Vision service-account JSON: env var overrides config;
    plain field (dev) or DPAPI blob (Windows at-rest). Empty string when unset."""
    creds = os.environ.get("GOOGLE_OCR_CREDENTIALS", "").strip()
    if creds:
        return creds
    creds = (CONFIG.get("google_ocr_credentials") or "").strip()
    if creds:
        return creds
    blob = (CONFIG.get("google_ocr_credentials_dpapi") or "").strip()
    if blob:
        try:
            return dpapi_unprotect_b64(blob)
        except Exception as exc:
            log.error("DPAPI decrypt of Google OCR credentials failed: %s", exc)
    return ""


def google_ocr_configured():
    return bool(CONFIG.get("google_ocr_enabled") and get_google_ocr_credentials())


def protect_secret(text):
    """At-rest form for a client-supplied secret (the per-document PDF password):
    DPAPI machine-scope on Windows, plain base64 tagging on other platforms (dev
    only). The prefix records which form was used so reveal_secret() can invert it."""
    if not text:
        return ""
    if os.name == "nt":
        try:
            return "dpapi:" + dpapi_protect_b64(text)
        except Exception as exc:
            log.error("DPAPI protect of a document password failed: %s", exc)
    return "b64:" + base64.b64encode(text.encode("utf-8")).decode("ascii")


def reveal_secret(stored):
    """Invert protect_secret(). Returns '' when nothing is stored or the blob
    cannot be decrypted on this machine."""
    stored = (stored or "").strip()
    if not stored:
        return ""
    try:
        if stored.startswith("dpapi:"):
            return dpapi_unprotect_b64(stored[len("dpapi:"):])
        if stored.startswith("b64:"):
            return base64.b64decode(stored[len("b64:"):]).decode("utf-8")
    except Exception as exc:
        log.error("could not reveal a stored document password: %s", exc)
    return ""


# --------------------------------------------------------------------------- #
# Path sanitization (ARCHITECTURE.md section 10) - the ONE shared sanitizer.
# EVERY filesystem/storage path segment built from client input goes through
# this; never join unsanitized input into a path (path-traversal defense).
# --------------------------------------------------------------------------- #
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._ -]")


def sanitize_segment(segment):
    seg = _SANITIZE_RE.sub("_", str(segment or ""))
    seg = seg.strip(". ")
    seg = seg[:100].strip(". ")  # re-strip: truncation may expose a dot/space
    return seg or "_"


def pdf_filename(docname):
    name = sanitize_segment(docname or "document")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


# --------------------------------------------------------------------------- #
# SQLite (same discipline as simulator/app.py: WAL, busy_timeout, threadpool)
# --------------------------------------------------------------------------- #
def db():
    # Fresh short-lived connection per operation (always opened inside a
    # threadpool worker, never shared across threads). WAL lets readers run
    # concurrently with a writer; busy_timeout makes concurrent writers wait
    # instead of raising "database is locked" under burst load.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _run(fn):
    """Open a connection, run fn(conn), commit, close - one transaction."""
    conn = db()
    try:
        result = fn(conn)
        conn.commit()
        return result
    finally:
        conn.close()


async def run_db(fn):
    """Await a blocking DB operation off the event loop."""
    return await run_in_threadpool(_run, fn)


def init_db():
    def _init(conn):
        conn.executescript(
            """
            -- A printer/device is now identified by (department_name,
            -- equipment_name) instead of a single device_type. equipment_name
            -- is the instrument class (GCMS, LCMS, ...) chosen at install.
            CREATE TABLE IF NOT EXISTS devices (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                device_name     TEXT UNIQUE COLLATE NOCASE,
                department_name TEXT,
                equipment_name  TEXT,
                printer_name    TEXT,
                hostname        TEXT,
                token           TEXT UNIQUE,
                created         TEXT,
                last_seen       TEXT
            );
            -- Local mirror of the Supabase printer_data feed. Refreshed whole
            -- whenever printer_data_version() changes. One row per
            -- (registration_number, department_name, test_method, test_parameter).
            CREATE TABLE IF NOT EXISTS printer_data (
                registration_number TEXT,
                department_name     TEXT,
                equipment_name      TEXT,
                status              TEXT,
                test_method         TEXT,
                test_parameter      TEXT,
                UNIQUE (registration_number, department_name, test_method, test_parameter)
            );
            CREATE INDEX IF NOT EXISTS printer_data_dept_equip
                ON printer_data (department_name, equipment_name);
            -- Each printed PDF. Mirrors the Supabase printer_documents columns
            -- (plus local bookkeeping: device_id, stored_path, status, queue).
            CREATE TABLE IF NOT EXISTS documents (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id           INTEGER,
                device_name         TEXT,   -- snapshot at ingest (survives revoke)
                department_name     TEXT,
                equipment_name      TEXT,
                registration_number TEXT,
                test_method         TEXT,
                test_parameter      TEXT,
                pdf_name            TEXT,   -- stored file name
                pdf_from            TEXT,   -- the app's print title / source doc
                sample_set_id       TEXT,   -- instrument Sample Set ID (OCR-derived)
                stored_path         TEXT,   -- local limsDocs path (deleted after push)
                storage_path        TEXT,   -- Supabase Storage object key (idempotency)
                size                INTEGER,
                sha256              TEXT,
                printed_by          TEXT,
                job_id              TEXT,
                status              TEXT,   -- filed | held | forwarded | forward_failed
                held_reason         TEXT,
                received            TEXT,
                forwarded_at        TEXT
            );
            CREATE TABLE IF NOT EXISTS forward_queue (
                document_id  INTEGER PRIMARY KEY,
                storage_path TEXT,      -- fixed at enqueue so retries hit the same
                attempts     INTEGER DEFAULT 0,  -- object (409 = already uploaded)
                next_attempt REAL DEFAULT 0,
                last_error   TEXT
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        # Migration for a hub.db created by the OLD (device_type) schema:
        # CREATE TABLE IF NOT EXISTS never alters an existing table, so add any
        # column the reworked code writes but an old devices/documents table
        # lacks. Old columns are left in place (harmless). Idempotent - on a
        # fresh DB the CREATEs above already made these, so nothing is added.
        def _ensure_cols(table, cols):
            have = {r["name"] for r in conn.execute(
                "PRAGMA table_info(%s)" % table).fetchall()}
            for col, typ in cols:
                if col not in have:
                    conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))
        _ensure_cols("devices", [("department_name", "TEXT"), ("equipment_name", "TEXT")])
        _ensure_cols("documents", [
            ("department_name", "TEXT"), ("equipment_name", "TEXT"),
            ("registration_number", "TEXT"), ("test_method", "TEXT"),
            ("test_parameter", "TEXT"), ("pdf_name", "TEXT"),
            ("pdf_from", "TEXT"), ("storage_path", "TEXT"),
            ("sample_set_id", "TEXT")])
        if not conn.execute("SELECT 1 FROM meta WHERE key='pd_version'").fetchone():
            conn.execute("INSERT INTO meta (key, value) VALUES ('pd_version', '')")
    _run(_init)


init_db()


def get_meta(conn, key, default=""):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


# --------------------------------------------------------------------------- #
# Auth helpers
# --------------------------------------------------------------------------- #
def require_admin(x_admin_token):
    if not x_admin_token or not secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing admin token.")


def require_enroll(x_enroll_key):
    if not x_enroll_key or not secrets.compare_digest(x_enroll_key, ENROLL_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing enroll key.")


async def device_by_token(token):
    if not token:
        return None
    return await run_db(lambda conn: conn.execute(
        "SELECT * FROM devices WHERE token = ?", (token,)).fetchone())


# --------------------------------------------------------------------------- #
# Catalog cache: snapshot, validation, share-file export
# --------------------------------------------------------------------------- #
_UNAVAILABLE_STATUS = ("closed", "cancelled", "canceled", "complete",
                       "completed", "done")


def _pd_available(status):
    """A printer_data row is offered to a printer unless its status marks it
    finished/closed. (The exact status vocabulary is the LIMS's; anything not in
    the closed-like set counts as available.)"""
    return (status or "").strip().lower() not in _UNAVAILABLE_STATUS


def _catalog_snapshot(conn):
    """Full printer_data mirror + version, for the share export and the API."""
    version = get_meta(conn, "pd_version", "")
    rows = [dict(r) for r in conn.execute(
        "SELECT registration_number, department_name, equipment_name, status, "
        "test_method, test_parameter FROM printer_data").fetchall()]
    return {"version": version, "rows": rows}


def catalog_payload(snapshot, department_name, equipment_name):
    """Cascading catalog for one printer (its department + equipment): a list of
    registrations, each with its test methods, each method with its parameters.
    Feeds the 3 dependent dropdowns in the print prompt."""
    tree = {}  # reg -> {method -> set(param)}
    for r in snapshot["rows"]:
        if r["department_name"] != department_name or r["equipment_name"] != equipment_name:
            continue
        if not _pd_available(r["status"]):
            continue
        tree.setdefault(r["registration_number"], {}) \
            .setdefault(r["test_method"], set()).add(r["test_parameter"])
    regs = []
    for reg in sorted(k for k in tree if k):
        methods = []
        for m in sorted(k for k in tree[reg] if k):
            params = sorted(p for p in tree[reg][m] if p)
            if params:   # skip a method with no real parameters (the prompt's
                methods.append({"test_method": m, "parameters": params})  # OK would never enable)
        if methods:      # skip a registration left with no usable method
            regs.append({"registration_number": reg, "methods": methods})
    return {"version": snapshot["version"],
            "department_name": department_name,
            "equipment_name": equipment_name,
            "registrations": regs}


def _write_catalog_files(snapshot):
    """Atomic share export (section 6b): write .tmp then os.replace. Blocking -
    run via threadpool. Best-effort: the share dir may be missing/readonly. When no
    limsDocs directory is configured there is no share to export to, so skip."""
    root = lims_docs_dir()
    if not root:
        return
    outdir = os.path.join(root, ".vcp", "catalog")
    os.makedirs(outdir, exist_ok=True)

    def write_json(name, payload):
        path = os.path.join(outdir, name + ".json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)
        os.replace(tmp, path)

    # One catalog file per (department, equipment) pair present in the feed, so
    # a client whose SYSTEM-side fetch fails can still load its dropdowns from
    # the share. Filename = "<dept>__<equipment>.json" (both sanitized) - the
    # client computes the same name (see catalog_share_name()).
    pairs = sorted({(r["department_name"], r["equipment_name"])
                    for r in snapshot["rows"]
                    if r["department_name"] and r["equipment_name"]})
    for dept, equip in pairs:
        name = sanitize_segment(dept) + "__" + sanitize_segment(equip)
        write_json(name, catalog_payload(snapshot, dept, equip))
    write_json("all", {"version": snapshot["version"], "rows": snapshot["rows"]})


async def export_catalog():
    """Snapshot the cache and rewrite the share files. Never raises (a broken
    share must not fail an ingest or a catalog mutation)."""
    try:
        snapshot = await run_db(_catalog_snapshot)
        await run_in_threadpool(_write_catalog_files, snapshot)
    except Exception as exc:
        log.warning("catalog share export failed: %s", exc)


def validate_job(conn, department_name, equipment_name, reg_no, test_method, test_parameter):
    """Returns a held_reason string, or None when the job may be filed. The full
    (department, equipment, registration, method, parameter) tuple must exist and
    be available in the printer_data mirror."""
    if not reg_no:
        return "missing_registration"
    if not test_method:
        return "missing_method"
    if not test_parameter:
        return "missing_test"
    row = conn.execute(
        "SELECT status FROM printer_data WHERE registration_number = ? "
        "AND department_name = ? AND equipment_name = ? "
        "AND test_method = ? AND test_parameter = ?",
        (reg_no, department_name, equipment_name, test_method, test_parameter)).fetchone()
    if not row or not _pd_available(row["status"]):
        return "unknown_registration"
    return None


# --------------------------------------------------------------------------- #
# Filing & forwarding helpers
# --------------------------------------------------------------------------- #
def composed_name(reg, method, param, uid):
    """The document's identifying filename:
    <reg>_<method>_<param>_<uuid>.pdf (each segment sanitized; uid keeps unique).
    Replaces the app-supplied print title with something a lab can identify."""
    return "%s_%s_%s_%s.pdf" % (sanitize_segment(reg), sanitize_segment(method),
                                sanitize_segment(param), uid)


def filed_dest_path(department, equipment, reg, method, param):
    """Where a filed PDF is written locally. When a limsDocs directory is
    configured, mirror the tree:
      <root>/department/equipment/registration/test_method/test_parameter/<name>.pdf
    When it is NOT configured, stage the file FLAT in OUTBOX_DIR — no folder tree is
    created — and it is deleted after the Supabase forward. The Supabase object key
    (storage_path_for) is independent of this, so the web-app breadcrumb is
    unaffected either way."""
    fname = composed_name(reg, method, param, secrets.token_hex(4))
    root = lims_docs_dir()
    if not root:
        return os.path.join(OUTBOX_DIR, fname)
    return os.path.join(root,
                        sanitize_segment(department), sanitize_segment(equipment),
                        sanitize_segment(reg), sanitize_segment(method),
                        sanitize_segment(param), fname)


def storage_path_for(department, equipment, reg, method, param):
    """Supabase Storage object key: mirrors the limsDocs tree. Fixed at enqueue
    time so every retry targets the same object (a 409 duplicate = success)."""
    return "/".join([
        sanitize_segment(department), sanitize_segment(equipment),
        sanitize_segment(reg), sanitize_segment(method), sanitize_segment(param),
        composed_name(reg, method, param, uuid.uuid4().hex),
    ])


def _move_file(src, dest):
    """Blocking move (may cross volumes: data dir vs limsDocs) - threadpool."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(src, dest)


def _stream_to_disk(upload_file, dest_path):
    """Copy an upload to disk in 1 MiB chunks, hashing as we go (blocking; run
    via threadpool). Returns (byte_count, sha256hex) - the computed hash is
    authoritative over the client-sent field."""
    total = 0
    digest = hashlib.sha256()
    upload_file.seek(0)
    with open(dest_path, "wb") as fh:
        while True:
            chunk = upload_file.read(STREAM_CHUNK)
            if not chunk:
                break
            fh.write(chunk)
            digest.update(chunk)
            total += len(chunk)
    return total, digest.hexdigest()


def _enqueue_forward(conn, doc_id, storage_path):
    # Enqueued even in local mode: the worker idles until Supabase is
    # configured, then drains the backlog. document_id is the PK, so a
    # re-filed doc replaces its pending entry instead of duplicating it. The
    # storage_path is fixed once (at ingest) and reused across retries, so it is
    # a stable idempotency key (a 409 on re-upload = already delivered).
    conn.execute(
        "INSERT OR REPLACE INTO forward_queue "
        "(document_id, storage_path, attempts, next_attempt, last_error) "
        "VALUES (?, ?, 0, 0, NULL)",
        (doc_id, storage_path))


# --------------------------------------------------------------------------- #
# Background tasks (started in lifespan)
# --------------------------------------------------------------------------- #
HTTP_CLIENT = None  # shared httpx.AsyncClient, created in lifespan

# Sync status — updated by catalog_sync_task/_refresh_from_supabase, read by
# GET /api/sync-status so the dashboard can show the last error without the
# admin having to inspect the server terminal.
_sync_status = {
    "last_ok": None,       # ISO-UTC timestamp of last successful table fetch
    "last_error": None,    # last error string, or None when last sync succeeded
    "row_count": 0,        # row count from last successful fetch
    "rpc_available": None, # True/False once known; None = not yet tried
}


def _rest_headers(key):
    return {"apikey": key, "Authorization": "Bearer " + key}


async def _refresh_from_supabase(url, key, version):
    """Refetch printer_data and replace the local mirror + version watermark in
    one transaction, then export the share files (which read that watermark)."""
    global _sync_status
    r = await HTTP_CLIENT.get(
        url + "/rest/v1/printer_data"
              "?select=registration_number,department_name,equipment_name,"
              "status,test_method,test_parameter",
        headers=_rest_headers(key))
    if not r.is_success:
        # Surface the response body so the admin can see why (e.g. auth error,
        # wrong URL, RLS denial) rather than just the status code.
        try:
            detail = r.json()
        except Exception:
            detail = r.text[:200]
        raise ValueError("HTTP %d fetching printer_data: %s" % (r.status_code, detail))
    rows = r.json()
    if not isinstance(rows, list):
        # A non-list body usually means the URL points at the Studio UI or a
        # gateway that returned an HTML/text page instead of PostgREST JSON.
        raise ValueError(
            "Unexpected response type (%s) — is SUPABASE_URL the REST API "
            "endpoint (PostgREST), not the Studio UI? Got: %r" % (type(rows).__name__, str(rows)[:120]))

    def _replace(conn):
        conn.execute("DELETE FROM printer_data")
        for row in rows:
            conn.execute(
                "INSERT OR IGNORE INTO printer_data (registration_number, "
                "department_name, equipment_name, status, test_method, test_parameter) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (row.get("registration_number"), row.get("department_name"),
                 row.get("equipment_name"), row.get("status"),
                 row.get("test_method"), row.get("test_parameter")))
        # Stamp the version in the SAME transaction, BEFORE export_catalog() reads
        # it, so the exported files / /catalog report the version they actually
        # contain (not the previous one). Skip when version is None (fallback
        # unconditional-poll mode: no version function installed).
        if version is not None:
            set_meta(conn, "pd_version", str(version))

    await run_db(_replace)
    await export_catalog()
    _sync_status["last_ok"] = datetime.now(timezone.utc).isoformat()
    _sync_status["last_error"] = None
    _sync_status["row_count"] = len(rows)
    log.info("printer_data synced from Supabase: %d rows (version %s)",
             len(rows), str(version)[:12] if version is not None else "n/a")


async def catalog_sync_task():
    """Poll printer_data_version() (~poll_seconds); on change, refetch
    printer_data. Idles when Supabase is not configured.

    Falls back to unconditional polling when printer_data_version() is not
    installed in Supabase (RPC returns non-2xx). Uses a sentinel initial value
    so a null/None return from the function still triggers the first sync.
    Errors are recorded in _sync_status so the dashboard can surface them.
    """
    global _sync_status
    last_version = object()  # unique sentinel; no JSON value (incl. null) equals this
    rpc_available = None     # None=unknown, True=working, False=unavailable
    while True:
        try:
            if not supabase_configured():
                await asyncio.sleep(poll_seconds())
                continue
            url = CONFIG["supabase_url"].rstrip("/")
            key = get_service_key()
            # PostgREST exposes functions as RPC via POST (empty JSON body).
            r = await HTTP_CLIENT.post(url + "/rest/v1/rpc/printer_data_version",
                                       headers=_rest_headers(key), json={})
            if r.is_success:
                if rpc_available is False:
                    log.info("printer_data_version() RPC now available")
                rpc_available = True
                _sync_status["rpc_available"] = True
                version = r.json()
                if version != last_version:
                    await _refresh_from_supabase(url, key, version)
                    last_version = version
            else:
                # Function not installed or RPC unavailable: poll unconditionally.
                if rpc_available is not False:
                    log.info("printer_data_version() RPC unavailable (HTTP %d); "
                             "polling printer_data unconditionally", r.status_code)
                rpc_available = False
                _sync_status["rpc_available"] = False
                await _refresh_from_supabase(url, key, None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            err = str(exc)
            _sync_status["last_error"] = err
            log.warning("printer_data sync: %s", err)
        await asyncio.sleep(poll_seconds())


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


async def _extract_sample_set(row, pdf_bytes, current_from):
    """Run Google Vision on the PDF and derive the Sample Set ID + a clean batch
    'From' header (see ocr.parse_batch). Persists both onto the documents row and
    returns (sample_set_id, pdf_from) for the forward payload. Best-effort: any
    failure leaves the Sample Set ID unset and keeps the client-supplied From.
    The transient 'extracting' status drives the dashboard's progress indicator."""
    doc_id = row["document_id"]
    await run_db(lambda c: c.execute(
        "UPDATE documents SET status = 'extracting' WHERE id = ? "
        "AND status IN ('filed', 'forward_failed', 'extracting')", (doc_id,)))
    sample_set_id, pdf_from = None, None
    try:
        text = await ocr.vision_extract_text(
            HTTP_CLIENT, get_google_ocr_credentials(), pdf_bytes)
        sample_set_id, pdf_from = ocr.parse_batch(text, row["equipment_name"])
        log.info("OCR doc %s (%s): sample_set_id=%r",
                 doc_id, row["equipment_name"], sample_set_id)
    except Exception as exc:
        log.warning("OCR doc %s failed (forwarding without a Sample Set ID): %s",
                    doc_id, exc)
    new_sid = (sample_set_id or "").strip() or None
    new_from = (pdf_from or "").strip() or current_from
    await run_db(lambda c: c.execute(
        "UPDATE documents SET sample_set_id = ?, pdf_from = ?, status = 'filed' "
        "WHERE id = ?", (new_sid, new_from, doc_id)))
    return new_sid, new_from


async def _forward_one(row):
    """Storage upload + documents row insert for one queue entry. Raises on
    failure (the caller schedules the backoff retry)."""
    url = CONFIG["supabase_url"].rstrip("/")
    key = get_service_key()
    bucket = CONFIG.get("bucket") or "printer-documents"
    data = await run_in_threadpool(_read_bytes, row["stored_path"])

    # OCR the PDF once, before it leaves for Supabase, to fill in the Sample Set
    # ID and correct the batch 'From'. Guarded on sample_set_id being unset so a
    # forward retry never re-bills a Vision call.
    sample_set_id = row["sample_set_id"]
    pdf_from = row["pdf_from"]
    if google_ocr_configured() and not (sample_set_id or "").strip():
        sample_set_id, pdf_from = await _extract_sample_set(row, data, pdf_from)

    r = await HTTP_CLIENT.post(
        "%s/storage/v1/object/%s/%s" % (url, bucket, row["storage_path"]),
        content=data,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/pdf", "x-upsert": "false"})
    # 409 = object already exists: a previous attempt uploaded it but the
    # printer_documents insert failed. Treat as success for the storage step.
    if r.status_code >= 300 and r.status_code != 409:
        raise RuntimeError("storage upload HTTP %d: %s" % (r.status_code, r.text[:300]))

    payload = {
        "pdf_name": row["pdf_name"], "pdf_from": pdf_from,
        "device_name": row["device_name"], "equipment_name": row["equipment_name"],
        "registration_number": row["registration_number"],
        "test_method": row["test_method"], "department_name": row["department_name"],
        "test_parameter": row["test_parameter"], "printed_by": row["printed_by"],
        "size_of_pdf": row["size"], "storage_path": row["storage_path"],
        "sample_set_id": sample_set_id or None,
    }
    # UPSERT on storage_path (unique in printer_documents) so an at-least-once
    # retry after a post-commit failure (hub restart, or a gateway 5xx/timeout
    # returned while the row was actually inserted) is a no-op instead of a
    # duplicate. storage_path is fixed at ingest and reused across retries, so it
    # is a stable idempotency key; the storage step is idempotent (409 = success).
    r = await HTTP_CLIENT.post(
        url + "/rest/v1/printer_documents?on_conflict=storage_path", json=payload,
        headers=dict(_rest_headers(key),
                     **{"Prefer": "resolution=merge-duplicates,return=minimal"}))
    if r.status_code >= 300:
        raise RuntimeError("printer_documents insert HTTP %d: %s"
                           % (r.status_code, r.text[:300]))


def _next_due(conn):
    return conn.execute(
        "SELECT q.document_id, q.storage_path, q.attempts,"
        "       d.registration_number, d.department_name, d.equipment_name,"
        "       d.test_method, d.test_parameter, d.pdf_name, d.pdf_from,"
        "       d.sample_set_id, d.device_name, d.stored_path, d.size, d.sha256,"
        "       d.printed_by, d.job_id, d.status"
        "  FROM forward_queue q JOIN documents d ON d.id = q.document_id"
        " WHERE q.next_attempt <= ? ORDER BY q.next_attempt LIMIT 1",
        (time.time(),)).fetchone()


async def forward_worker_task():
    """Drain forward_queue: at-least-once delivery to Supabase with exponential
    backoff (5 s doubling, 300 s cap). Idles in local mode."""
    while True:
        try:
            if not supabase_configured():
                await asyncio.sleep(5)
                continue
            row = await run_db(_next_due)
            if row is None:
                await asyncio.sleep(1)
                continue
            doc_id = row["document_id"]
            if row["status"] not in ("filed", "forward_failed", "extracting"):
                # held again / deleted meanwhile - drop the stale queue entry
                await run_db(lambda c: c.execute(
                    "DELETE FROM forward_queue WHERE document_id = ?", (doc_id,)))
                continue
            try:
                await _forward_one(row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                err = str(exc)[:500]
                delay = min(5 * (2 ** row["attempts"]), 300)
                nxt = time.time() + delay
                attempts = row["attempts"] + 1
                log.warning("forward doc %s failed (attempt %d, retry in %ds): %s",
                            doc_id, attempts, delay, err)
                await run_db(lambda c: (
                    c.execute("UPDATE forward_queue SET attempts = ?, next_attempt = ?,"
                              " last_error = ? WHERE document_id = ?",
                              (attempts, nxt, err, doc_id)),
                    c.execute("UPDATE documents SET status = 'forward_failed' "
                              "WHERE id = ? AND status IN ('filed','forward_failed')",
                              (doc_id,)),
                ))
                continue
            # Push succeeded. Commit the DB change FIRST (status forwarded, drop
            # the queue entry, forget the local path), THEN best-effort delete the
            # local file. Ordering is critical: if we crash AFTER the commit the
            # file is just an orphan (harmless leak, not re-processed - the queue
            # entry is gone); if we crash BEFORE it, status stays 'filed' with the
            # file + queue entry intact, so the next run re-pushes (409 = success)
            # and finishes. Deleting first (the previous ordering) could strand a
            # already-delivered doc as 'forward_failed' forever, because the retry
            # would try to _read_bytes() a file that is already gone.
            stored = row["stored_path"]
            stamp = now_iso()
            await run_db(lambda c: (
                c.execute("UPDATE documents SET status = 'forwarded', forwarded_at = ?, "
                          "stored_path = NULL WHERE id = ?", (stamp, doc_id)),
                c.execute("DELETE FROM forward_queue WHERE document_id = ?", (doc_id,)),
            ))
            await run_in_threadpool(
                lambda: stored and os.path.exists(stored) and os.remove(stored))
            log.info("forwarded doc %s -> %s (local copy deleted)",
                     doc_id, row["storage_path"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("forward worker: %s", exc)
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(_app):
    global HTTP_CLIENT
    # Raise the threadpool ceiling so ~100 concurrent uploads don't serialize.
    try:
        import anyio
        anyio.to_thread.current_default_thread_limiter().total_tokens = THREADPOOL_TOKENS
    except Exception:
        pass
    HTTP_CLIENT = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10))
    await export_catalog()  # make the share fallback exist from the first start
    tasks = [asyncio.create_task(catalog_sync_task()),
             asyncio.create_task(forward_worker_task())]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await HTTP_CLIENT.aclose()


app = FastAPI(title="LIMS Print Hub", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Ingest (section 8) - what every client printer POSTs to
# --------------------------------------------------------------------------- #
@app.post("/ingest/{token}")
async def ingest(token: str, request: Request):
    dev = await device_by_token(token)
    if not dev:
        raise HTTPException(status_code=404, detail="Unknown ingest token.")
    seen = now_iso()
    await run_db(lambda c: c.execute(
        "UPDATE devices SET last_seen = ? WHERE id = ?", (seen, dev["id"])))

    form = await request.form()
    fields = {}
    upload = None
    for key, value in form.multi_items():
        # Plain strings are metadata; anything else is the uploaded file
        # (avoids UploadFile-subclass isinstance mismatches, like the simulator).
        if isinstance(value, str):
            fields[key] = value
        else:
            upload = value
    if upload is None:
        raise HTTPException(status_code=400, detail="No file part in the upload.")

    pdf_from = (fields.get("pdf_from") or fields.get("docname")
                or upload.filename or "document")
    reg_no = (fields.get("registration_number") or "").strip()
    test_method = (fields.get("test_method") or "").strip()
    test_parameter = (fields.get("test_parameter") or "").strip()
    printed_by = (fields.get("printed_by") or fields.get("user") or "").strip()
    # The device row is authoritative for identity/routing; client-sent values
    # are only logged when they disagree (a misconfigured client, not a decider).
    device_name = dev["device_name"]
    department_name = dev["department_name"]
    equipment_name = dev["equipment_name"]
    for fld, enrolled in (("device_name", device_name),
                          ("department_name", department_name),
                          ("equipment_name", equipment_name)):
        if fields.get(fld) and fields[fld] != enrolled:
            log.warning("ingest %s: client %s %r != enrolled %r",
                        token[:8], fld, fields[fld], enrolled)

    # Stream to a temp file first; it is renamed/moved only after validation.
    tmp_path = os.path.join(TMP_DIR, uuid.uuid4().hex + ".pdf")
    size, sha256 = await run_in_threadpool(_stream_to_disk, upload.file, tmp_path)
    if fields.get("sha256") and fields["sha256"].lower() != sha256:
        log.warning("ingest %s: client sha256 mismatch for %r (client %s != computed %s)",
                    token[:8], pdf_from, fields["sha256"][:16], sha256[:16])

    reason = await run_db(lambda c: validate_job(
        c, department_name, equipment_name, reg_no, test_method, test_parameter))
    received = now_iso()

    if reason is None:
        dest = filed_dest_path(department_name, equipment_name,
                               reg_no, test_method, test_parameter)
        storage_path = storage_path_for(department_name, equipment_name,
                                         reg_no, test_method, test_parameter)
        status = "filed"
    else:
        # Held files live under data/held/, NOT in limsDocs.
        dest = os.path.join(HELD_DIR, uuid.uuid4().hex + "_" + pdf_filename(pdf_from))
        storage_path = None
        status = "held"
    try:
        await run_in_threadpool(_move_file, tmp_path, dest)
    except Exception:
        await run_in_threadpool(lambda: os.path.exists(tmp_path) and os.remove(tmp_path))
        raise

    pdf_name = os.path.basename(dest)

    def _insert(conn):
        cur = conn.execute(
            "INSERT INTO documents (device_id, device_name, department_name, equipment_name,"
            " registration_number, test_method, test_parameter, pdf_name, pdf_from,"
            " stored_path, storage_path, size, sha256, printed_by, job_id,"
            " status, held_reason, received)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (dev["id"], device_name, department_name, equipment_name,
             reg_no, test_method, test_parameter, pdf_name, pdf_from,
             dest, storage_path, size, sha256, printed_by,
             fields.get("job_id") or "", status, reason, received))
        doc_id = cur.lastrowid
        if status == "filed":
            _enqueue_forward(conn, doc_id, storage_path)
        return doc_id

    doc_id = await run_db(_insert)
    if reason:
        log.info("ingest %s: HELD doc %s (%s) reason=%s", token[:8], doc_id, pdf_from, reason)
    # Always 2xx (held included): the client must treat held as success.
    return JSONResponse({"ok": True, "status": status, "reason": reason, "id": doc_id})


# --------------------------------------------------------------------------- #
# Catalog for clients (section 6a) - any enrolled device's token
# --------------------------------------------------------------------------- #
@app.get("/catalog")
async def get_catalog(x_device_token: str = Header(default="")):
    dev = await device_by_token(x_device_token)
    if not dev:
        raise HTTPException(status_code=401, detail="Invalid device token.")
    snapshot = await run_db(_catalog_snapshot)
    return catalog_payload(snapshot, dev["department_name"], dev["equipment_name"])


# --------------------------------------------------------------------------- #
# Enrollment (section 9) - X-Enroll-Key
# --------------------------------------------------------------------------- #
@app.get("/departments")
async def list_departments(x_enroll_key: str = Header(default="")):
    """Distinct departments in the printer_data feed - feeds the installer's
    'Choose Department' dropdown."""
    require_enroll(x_enroll_key)
    rows = await run_db(lambda c: c.execute(
        "SELECT DISTINCT department_name FROM printer_data "
        "WHERE department_name IS NOT NULL AND department_name <> '' "
        "ORDER BY department_name").fetchall())
    return {"departments": [r["department_name"] for r in rows]}


@app.get("/equipment")
async def list_equipment(department: str = "", x_enroll_key: str = Header(default="")):
    """Distinct equipment names, optionally scoped to a department - feeds the
    installer's 'Choose Equipment' dropdown after a department is picked."""
    require_enroll(x_enroll_key)
    if department:
        rows = await run_db(lambda c: c.execute(
            "SELECT DISTINCT equipment_name FROM printer_data "
            "WHERE department_name = ? AND equipment_name IS NOT NULL "
            "AND equipment_name <> '' ORDER BY equipment_name",
            (department,)).fetchall())
    else:
        rows = await run_db(lambda c: c.execute(
            "SELECT DISTINCT equipment_name FROM printer_data "
            "WHERE equipment_name IS NOT NULL AND equipment_name <> '' "
            "ORDER BY equipment_name").fetchall())
    return {"equipment": [r["equipment_name"] for r in rows]}


async def _set_device_password_remote(device_name, password):
    """Store the per-device PDF password in Supabase (encrypted at rest via the
    set_device_password RPC, service role). Raises on failure."""
    url = CONFIG["supabase_url"].rstrip("/")
    key = get_service_key()
    r = await HTTP_CLIENT.post(
        url + "/rest/v1/rpc/set_device_password",
        headers=_rest_headers(key),
        json={"_device_name": device_name, "_password": password})
    r.raise_for_status()


@app.post("/admin/devices", status_code=201)
async def enroll_device(request: Request, x_enroll_key: str = Header(default="")):
    require_enroll(x_enroll_key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body required.")
    name = (body.get("device_name") or "").strip()
    department = (body.get("department_name") or "").strip()
    equipment = (body.get("equipment_name") or "").strip()
    password = body.get("pdf_password") or ""
    if not DEVICE_NAME_RE.match(name):
        raise HTTPException(status_code=400,
                            detail="Invalid device_name (must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$).")
    if not department or not equipment:
        raise HTTPException(status_code=400,
                            detail="department_name and equipment_name are required.")
    known = await run_db(lambda c: c.execute(
        "SELECT 1 FROM printer_data WHERE department_name = ? AND equipment_name = ? LIMIT 1",
        (department, equipment)).fetchone())
    if not known:
        raise HTTPException(status_code=400,
                            detail="Unknown department/equipment: %r / %r" % (department, equipment))
    dup = await run_db(lambda c: c.execute(
        "SELECT 1 FROM devices WHERE device_name = ? COLLATE NOCASE", (name,)).fetchone())
    if dup:
        raise HTTPException(status_code=409,
                            detail="device_name already exists (case-insensitive): %r" % name)

    token = secrets.token_urlsafe(24)
    created = now_iso()
    try:
        await run_db(lambda c: c.execute(
            "INSERT INTO devices (device_name, department_name, equipment_name, printer_name,"
            " hostname, token, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, department, equipment, (body.get("printer_name") or "").strip(),
             (body.get("hostname") or "").strip(), token, created)))
    except sqlite3.IntegrityError:
        # Lost a race with a concurrent enrollment of the same name.
        raise HTTPException(status_code=409,
                            detail="device_name already exists (case-insensitive): %r" % name)

    # Store the per-device PDF password in Supabase (encrypted at rest) so the
    # LIMS can reveal it. Best-effort: enrollment still succeeds if Supabase is
    # unreachable (the client keeps the password locally for encryption).
    if password and supabase_configured():
        try:
            await _set_device_password_remote(name, password)
        except Exception as exc:
            log.warning("enroll %s: could not store PDF password in Supabase: %s", name, exc)

    ingest_url = str(request.base_url).rstrip("/") + "/ingest/" + token
    return JSONResponse(status_code=201, content={
        "token": token, "device_name": name, "department_name": department,
        "equipment_name": equipment, "ingest_url": ingest_url})


# --------------------------------------------------------------------------- #
# Admin API (X-Admin-Token) - powers the dashboard
# --------------------------------------------------------------------------- #
@app.get("/api/enroll-key")
async def api_enroll_key(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    return {"enroll_key": ENROLL_KEY}


@app.get("/api/devices")
async def api_devices(request: Request, x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    base = str(request.base_url).rstrip("/")
    rows = await run_db(lambda c: c.execute(
        "SELECT d.*, (SELECT COUNT(*) FROM documents WHERE device_id = d.id) doc_count"
        "  FROM devices d ORDER BY d.device_name").fetchall())
    return [{
        "id": r["id"], "device_name": r["device_name"],
        "department_name": r["department_name"], "equipment_name": r["equipment_name"],
        "printer_name": r["printer_name"], "hostname": r["hostname"],
        "created": r["created"], "last_seen": r["last_seen"],
        "doc_count": r["doc_count"], "ingest_url": base + "/ingest/" + r["token"],
    } for r in rows]


@app.delete("/api/devices/{device_id}")
async def api_delete_device(device_id: int, x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    # Revokes the token: the device row is removed, its documents remain.
    await run_db(lambda c: c.execute("DELETE FROM devices WHERE id = ?", (device_id,)))
    return {"ok": True}


def _doc_row(r):
    return {
        "id": r["id"], "device_id": r["device_id"], "device_name": r["device_name"],
        "department_name": r["department_name"], "equipment_name": r["equipment_name"],
        "registration_number": r["registration_number"],
        "test_method": r["test_method"], "test_parameter": r["test_parameter"],
        "pdf_name": r["pdf_name"], "pdf_from": r["pdf_from"],
        "size": r["size"], "sha256": r["sha256"], "printed_by": r["printed_by"],
        "job_id": r["job_id"], "status": r["status"],
        "held_reason": r["held_reason"], "received": r["received"],
        "forwarded_at": r["forwarded_at"], "last_error": r["last_error"],
    }


@app.get("/api/documents")
async def api_documents(status: str = "", x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    # All identity/routing columns live ON the document (snapshot at ingest), so
    # the name persists even after the device is revoked.
    sql = ("SELECT d.*, q.last_error"
           "  FROM documents d"
           "  LEFT JOIN forward_queue q ON q.document_id = d.id")
    args = ()
    if status:
        sql += " WHERE d.status = ?"
        args = (status,)
    sql += " ORDER BY d.id DESC LIMIT 500"
    rows = await run_db(lambda c: c.execute(sql, args).fetchall())
    return [_doc_row(r) for r in rows]


@app.get("/api/documents/{doc_id}/file")
async def api_document_file(doc_id: int, x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    row = await run_db(lambda c: c.execute(
        "SELECT stored_path, pdf_name, pdf_from, storage_path FROM documents WHERE id = ?",
        (doc_id,)).fetchone())
    if not row:
        raise HTTPException(status_code=404, detail="Not found.")
    fname = pdf_filename(row["pdf_from"] or row["pdf_name"])
    # Local copy still exists (filed / held docs before forwarding).
    if row["stored_path"] and os.path.isfile(row["stored_path"]):
        return FileResponse(row["stored_path"], media_type="application/pdf", filename=fname)
    # Forwarded docs: local copy deleted after push → proxy from Supabase Storage.
    storage_path = (row["storage_path"] or "").strip()
    if storage_path and supabase_configured():
        url = CONFIG["supabase_url"].rstrip("/")
        key = get_service_key()
        bucket = CONFIG.get("bucket") or "printer-documents"
        try:
            r = await HTTP_CLIENT.get(
                "%s/storage/v1/object/%s/%s" % (url, bucket, storage_path),
                headers={"Authorization": "Bearer " + key},
            )
            if r.status_code == 200:
                return Response(
                    content=r.content,
                    media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % fname},
                )
            raise HTTPException(
                status_code=r.status_code,
                detail="Supabase Storage returned HTTP %d — check bucket/storage_path." % r.status_code,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Storage proxy failed: %s" % exc)
    raise HTTPException(
        status_code=404,
        detail="Local copy deleted (forwarded) and Supabase Storage not reachable.",
    )


@app.post("/api/documents/{doc_id}/assign")
async def api_assign(doc_id: int, request: Request, x_admin_token: str = Header(default="")):
    """Operator assigns registration+method+parameter to a HELD document:
    validate against printer_data, move the file into the limsDocs tree, enqueue
    the forward."""
    require_admin(x_admin_token)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body required.")
    reg_no = (body.get("registration_number") or "").strip()
    method = (body.get("test_method") or "").strip()
    param = (body.get("test_parameter") or "").strip()

    def _load(conn):
        doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if not doc:
            return None, None
        return doc, validate_job(conn, doc["department_name"], doc["equipment_name"],
                                 reg_no, method, param)

    doc, reason = await run_db(_load)
    if not doc:
        raise HTTPException(status_code=404, detail="Not found.")
    if doc["status"] != "held":
        raise HTTPException(status_code=409, detail="Document is not held.")
    if reason:
        raise HTTPException(status_code=400, detail="Invalid assignment: " + reason)

    dest = filed_dest_path(doc["department_name"], doc["equipment_name"], reg_no, method, param)
    storage_path = storage_path_for(doc["department_name"], doc["equipment_name"],
                                     reg_no, method, param)
    await run_in_threadpool(_move_file, doc["stored_path"], dest)

    def _update(conn):
        conn.execute("UPDATE documents SET registration_number = ?, test_method = ?,"
                     " test_parameter = ?, pdf_name = ?, stored_path = ?, storage_path = ?,"
                     " status = 'filed', held_reason = NULL WHERE id = ?",
                     (reg_no, method, param, os.path.basename(dest), dest, storage_path, doc_id))
        _enqueue_forward(conn, doc_id, storage_path)

    await run_db(_update)
    return {"ok": True, "status": "filed", "id": doc_id}


@app.get("/api/catalog")
async def api_catalog(x_admin_token: str = Header(default="")):
    """Read-only view of the printer_data mirror (managed in the LIMS)."""
    require_admin(x_admin_token)
    return await run_db(_catalog_snapshot)


@app.get("/api/settings")
async def api_get_settings(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    key_set = bool(get_service_key())
    return {
        "lims_docs_dir": lims_docs_dir(),
        "supabase_url": CONFIG.get("supabase_url") or "",
        "supabase_service_key": "********" if key_set else "",
        "supabase_configured": supabase_configured(),
        "bucket": CONFIG.get("bucket") or "printer-documents",
        "poll_seconds": poll_seconds(),
        # Self-hosted Supabase database credentials
        "supabase_db_host": CONFIG.get("supabase_db_host") or "",
        "supabase_db_port": CONFIG.get("supabase_db_port") or 5432,
        "supabase_db_name": CONFIG.get("supabase_db_name") or "",
        "supabase_db_user": CONFIG.get("supabase_db_user") or "",
        "supabase_db_password_set": bool(get_db_password()),
        # Application variables
        "supabase_publishable_key_set": bool(get_publishable_key()),
        "supabase_session_secret_set": bool(get_session_secret()),
        # Google Cloud Vision OCR
        "google_ocr_enabled": bool(CONFIG.get("google_ocr_enabled")),
        "google_ocr_credentials_set": bool(get_google_ocr_credentials()),
    }


@app.post("/api/settings")
async def api_set_settings(request: Request, x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body required.")
    if "lims_docs_dir" in body:
        # Empty is allowed and meaningful: no local folder tree is created and PDFs
        # are staged internally, then forwarded to Supabase. Do NOT re-default it.
        CONFIG["lims_docs_dir"] = str(body["lims_docs_dir"] or "").strip()
    if "supabase_url" in body:
        CONFIG["supabase_url"] = str(body["supabase_url"] or "").strip()
    if "poll_seconds" in body:
        try:
            CONFIG["poll_seconds"] = max(1, int(body["poll_seconds"]))
        except (TypeError, ValueError):
            pass
    key = str(body.get("supabase_service_key") or "").strip()
    # Blank or the mask placeholder = keep the stored key unchanged.
    if key and key != "********":
        if os.name == "nt":
            # DPAPI machine scope: at rest the key is only decryptable on this
            # machine (section 11: the service key never leaves the central desktop).
            CONFIG["supabase_service_key_dpapi"] = dpapi_protect_b64(key)
            CONFIG["supabase_service_key"] = ""
        else:
            CONFIG["supabase_service_key"] = key
            CONFIG["supabase_service_key_dpapi"] = ""
    # Self-hosted Supabase database credentials
    if "supabase_db_host" in body:
        CONFIG["supabase_db_host"] = str(body["supabase_db_host"] or "").strip()
    if "supabase_db_port" in body:
        try:
            CONFIG["supabase_db_port"] = max(1, min(65535, int(body["supabase_db_port"])))
        except (TypeError, ValueError):
            pass
    if "supabase_db_name" in body:
        CONFIG["supabase_db_name"] = str(body["supabase_db_name"] or "").strip()
    if "supabase_db_user" in body:
        CONFIG["supabase_db_user"] = str(body["supabase_db_user"] or "").strip()
    db_pass = str(body.get("supabase_db_password") or "").strip()
    if db_pass:
        if os.name == "nt":
            CONFIG["supabase_db_password_dpapi"] = dpapi_protect_b64(db_pass)
            CONFIG["supabase_db_password"] = ""
        else:
            CONFIG["supabase_db_password"] = db_pass
            CONFIG["supabase_db_password_dpapi"] = ""
    # Application variables
    pub_key = str(body.get("supabase_publishable_key") or "").strip()
    # Blank or the mask placeholder = keep the stored key unchanged.
    if pub_key and pub_key != "********":
        if os.name == "nt":
            CONFIG["supabase_publishable_key_dpapi"] = dpapi_protect_b64(pub_key)
            CONFIG["supabase_publishable_key"] = ""
        else:
            CONFIG["supabase_publishable_key"] = pub_key
            CONFIG["supabase_publishable_key_dpapi"] = ""
    session_secret = str(body.get("supabase_session_secret") or "").strip()
    if session_secret:
        if os.name == "nt":
            CONFIG["supabase_session_secret_dpapi"] = dpapi_protect_b64(session_secret)
            CONFIG["supabase_session_secret"] = ""
        else:
            CONFIG["supabase_session_secret"] = session_secret
            CONFIG["supabase_session_secret_dpapi"] = ""
    # Google Cloud Vision OCR
    if "google_ocr_enabled" in body:
        CONFIG["google_ocr_enabled"] = bool(body["google_ocr_enabled"])
    google_creds = str(body.get("google_ocr_credentials") or "").strip()
    # Blank or the mask placeholder = keep the stored credentials unchanged.
    if google_creds and google_creds != "********":
        if os.name == "nt":
            CONFIG["google_ocr_credentials_dpapi"] = dpapi_protect_b64(google_creds)
            CONFIG["google_ocr_credentials"] = ""
        else:
            CONFIG["google_ocr_credentials"] = google_creds
            CONFIG["google_ocr_credentials_dpapi"] = ""
    await run_in_threadpool(save_config, CONFIG)
    await export_catalog()  # a changed lims_docs_dir gets the share files immediately
    return await api_get_settings(x_admin_token)


@app.post("/api/catalog/sync")
async def api_catalog_sync(x_admin_token: str = Header(default="")):
    """Force an immediate printer_data fetch from Supabase, bypassing the
    version-check / background-task cycle. Useful when the sync task hasn't
    run yet or the RPC version function is not installed."""
    require_admin(x_admin_token)
    if not supabase_configured():
        raise HTTPException(status_code=400, detail="Supabase is not configured.")
    url = CONFIG["supabase_url"].rstrip("/")
    key = get_service_key()
    try:
        await _refresh_from_supabase(url, key, None)
        return {"ok": True, "row_count": _sync_status.get("row_count", 0),
                "last_error": _sync_status.get("last_error")}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/settings/test-connection")
async def api_test_connection(x_admin_token: str = Header(default="")):
    """Test connectivity + printer_data read access (detects RLS blocking)."""
    require_admin(x_admin_token)
    url = (CONFIG.get("supabase_url") or "").strip()
    key = get_service_key()
    if not url:
        return {"connected": False, "error": "Supabase URL is not configured.", "rls_warning": ""}
    if not key:
        return {"connected": False, "error": "Service role key is not configured.", "rls_warning": ""}
    hdrs = {"apikey": key, "Authorization": "Bearer " + key}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            # Step 1: basic API reachability
            r = await client.get(url.rstrip("/") + "/rest/v1/", headers=hdrs)
            if r.status_code >= 500:
                return {"connected": False, "error": "Server error: HTTP %d" % r.status_code, "rls_warning": ""}
            if r.status_code == 401:
                return {"connected": False, "error": "Authentication failed (401) — check your service role key.", "rls_warning": ""}
            if r.status_code >= 400:
                return {"connected": False, "error": "HTTP %d from REST API." % r.status_code, "rls_warning": ""}
            # Step 2: probe printer_data row access (catches RLS silently returning [])
            r2 = await client.get(
                url.rstrip("/") + "/rest/v1/printer_data?select=registration_number&limit=1",
                headers=hdrs)
            rls_warning = ""
            if r2.is_success:
                try:
                    payload = r2.json()
                    if isinstance(payload, list) and len(payload) == 0:
                        rls_warning = ("printer_data returned 0 rows. If rows exist in Supabase this is "
                                       "an RLS issue — run in Supabase SQL editor: "
                                       "ALTER ROLE service_role BYPASSRLS;")
                except Exception:
                    pass
            elif r2.status_code == 404:
                rls_warning = "printer_data table not found (HTTP 404) — verify the table exists in the public schema."
            else:
                rls_warning = "Could not read printer_data: HTTP %d." % r2.status_code
        return {"connected": True, "error": "", "rls_warning": rls_warning}
    except httpx.ConnectError as exc:
        return {"connected": False, "error": "Connection refused – check host/port. (%s)" % str(exc), "rls_warning": ""}
    except httpx.TimeoutException:
        return {"connected": False, "error": "Connection timed out.", "rls_warning": ""}
    except Exception as exc:
        return {"connected": False, "error": str(exc), "rls_warning": ""}


@app.get("/api/settings/test-ocr")
async def api_test_ocr(x_admin_token: str = Header(default="")):
    """Test Google Vision reachability with the stored credentials. Surfaces the
    real error (e.g. billing not enabled) instead of the Supabase 'Connected'."""
    require_admin(x_admin_token)
    if not CONFIG.get("google_ocr_enabled"):
        return {"ok": False, "error": "OCR extraction is disabled."}
    creds = get_google_ocr_credentials()
    if not creds:
        return {"ok": False, "error": "No Google OCR credentials set."}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await ocr.vision_test(client, creds)
        return {"ok": True, "error": ""}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500]}


@app.get("/api/sync-status")
async def api_sync_status(x_admin_token: str = Header(default="")):
    """Last printer_data sync state — used by the Catalog tab to surface errors."""
    require_admin(x_admin_token)
    return dict(_sync_status)


@app.get("/healthz")
async def healthz():
    return PlainTextResponse("ok")


# --------------------------------------------------------------------------- #
# Dashboard (single self-contained page, same visual style as the simulator)
# --------------------------------------------------------------------------- #
DASHBOARD = """<!doctype html>
<html><head><meta charset="utf-8"><title>LIMS Print Hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--bg:#0f1216;--card:#171c24;--line:#262d38;--fg:#e6edf3;--mut:#9aa7b4;--acc:#4c8dff;--ok:#2ea043;--warn:#d29922;--err:#ff6b6b}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
 header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center;flex-wrap:wrap}
 h1{font-size:16px;margin:0;font-weight:600} .sub{color:var(--mut);font-size:12px}
 main{padding:24px;max-width:1200px;margin:0 auto} .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
 input,select,button{font:inherit} input[type=text],input[type=password],input[type=number],select{background:#0d1117;border:1px solid var(--line);color:var(--fg);border-radius:7px;padding:8px 10px}
 button{background:var(--acc);color:#fff;border:0;border-radius:7px;padding:8px 14px;cursor:pointer} button.ghost{background:#222b36}
 table{width:100%;border-collapse:collapse;margin-top:8px} th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
 th{color:var(--mut);font-weight:500;font-size:12px} code{background:#0d1117;padding:2px 6px;border-radius:5px;word-break:break-all}
 .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center} .pill{background:#0d1117;border:1px solid var(--line);border-radius:20px;padding:2px 10px;font-size:12px;color:var(--mut)}
 .copy{cursor:pointer;color:var(--acc)} .muted{color:var(--mut)} a{color:var(--acc)} .del{cursor:pointer;color:var(--err)}
 nav{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}
 nav button{background:#222b36;color:var(--mut)} nav button.on{background:var(--acc);color:#fff}
 .st-filed{color:var(--acc)} .st-held{color:var(--warn)} .st-forwarded{color:var(--ok)} .st-forward_failed{color:var(--err)}
 .st-extracting{color:var(--warn)}
 @keyframes vcp-pulse{0%,100%{opacity:1}50%{opacity:.45}} .st-extracting{animation:vcp-pulse 1s ease-in-out infinite}
 label{color:var(--mut);font-size:12px;display:block;margin:8px 0 2px}
</style></head><body>
<header>
  <h1>LIMS Print Hub</h1><span class="sub">devices &middot; documents &middot; catalog &middot; Supabase forwarding</span>
  <div style="flex:1"></div>
  <div class="row"><span id="sessmsg" class="pill" style="display:none;color:var(--warn);border-color:var(--warn)"></span><input id="admin" type="password" placeholder="admin token" size="30"><button class="ghost" onclick="saveTok()">Save</button></div>
</header>
<main>
<nav>
  <button id="tb-devices" class="on" onclick="show('devices')">Devices</button>
  <button id="tb-docs" onclick="show('docs')">Documents</button>
  <button id="tb-held" onclick="show('held')">Held</button>
  <button id="tb-catalog" onclick="show('catalog')">Catalog</button>
  <button id="tb-settings" onclick="show('settings')">Settings</button>
</nav>

<section id="sec-devices">
  <div class="card">
    <div class="row"><b>Devices</b>
      <button class="ghost" onclick="loadDevices()">Refresh</button>
      <button class="ghost" onclick="showEnrollKey()">Show enroll key</button>
      <span id="enrollkey" class="pill" style="display:none"></span>
    </div>
    <p class="muted">Devices are enrolled from each client's <code>setup.ps1</code> using the enroll key. Revoking removes the device's ingest token; its documents remain.</p>
    <table id="devices"><thead><tr><th>Name</th><th>Department</th><th>Equipment</th><th>Printer</th><th>Host</th><th>Ingest URL</th><th>Docs</th><th>Last seen</th><th></th></tr></thead><tbody></tbody></table>
  </div>
</section>

<section id="sec-docs" style="display:none">
  <div class="card">
    <div class="row"><b>Documents</b>
      <select id="stfilter" onchange="loadDocs()">
        <option value="">all statuses</option><option value="filed">filed</option>
        <option value="held">held</option><option value="forwarded">forwarded</option>
        <option value="forward_failed">forward_failed</option>
      </select>
      <button class="ghost" onclick="loadDocs()">Refresh</button>
      <span class="muted">auto-refreshes every 3 s</span>
    </div>
    <table id="docs"><thead><tr><th>Document</th><th>Device</th><th>Reg / Method / Parameter</th><th>Status</th><th>Printed by</th><th>Size</th><th>Received (IST)</th><th></th></tr></thead><tbody></tbody></table>
  </div>
</section>

<section id="sec-held" style="display:none">
  <div class="card">
    <div class="row"><b>Held documents</b><button class="ghost" onclick="loadHeld()">Refresh</button>
      <span class="muted">Assign registration + method + parameter to file &amp; forward the job.</span></div>
    <table id="held"><thead><tr><th>Name</th><th>Device</th><th>Reason</th><th>Registration</th><th>Method</th><th>Parameter</th><th></th></tr></thead><tbody></tbody></table>
  </div>
</section>

<section id="sec-catalog" style="display:none">
  <div class="card">
    <div class="row"><b>Printer data</b><span id="catver" class="pill"></span><button class="ghost" onclick="loadCatalog()">Refresh</button><button class="ghost" id="syncnow-btn" onclick="syncNow()">Sync now</button>
      <input id="cat-q" type="text" placeholder="filter..." size="20" oninput="loadCatalog()"></div>
    <p class="muted" id="catmode"></p>
    <div id="syncerr" style="display:none;margin:6px 0 4px;padding:8px 12px;border-radius:6px;background:#3a1a1a;color:#ff8080;font-size:0.88em;word-break:break-word"></div>
    <p class="muted">Read-only mirror of the LIMS <code>printer_data</code> feed (managed in the LIMS, synced here). One row per registration / department / equipment / method / parameter.</p>
    <table id="catalog"><thead><tr><th>Registration</th><th>Department</th><th>Equipment</th><th>Status</th><th>Method</th><th>Parameter</th></tr></thead><tbody></tbody></table>
  </div>
</section>

<section id="sec-settings" style="display:none">
  <div class="card">
    <b>Settings</b>
    <label>limsDocs directory (local shared folder; leave blank to store in Supabase only &mdash; no local folder is created)</label>
    <input id="s-dir" type="text" size="50" placeholder="(not set &mdash; documents go to Supabase only)">
    <label>Catalog poll seconds</label>
    <input id="s-poll" type="number" min="1" style="width:90px">

    <hr style="margin:16px 0;border:none;border-top:1px solid var(--border,#ddd)">
    <b>Self-hosted Supabase credentials</b>
    <p class="muted" style="margin:6px 0 10px">Secret key &mdash; database connection</p>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 24px;max-width:640px">
      <div><label>host</label><input id="s-db-host" type="text" size="28"></div>
      <div><label>port</label><input id="s-db-port" type="number" min="1" max="65535" style="width:90px" value="5432"></div>
      <div><label>dbname</label><input id="s-db-name" type="text" size="28"></div>
      <div><label>username</label><input id="s-db-user" type="text" size="28"></div>
      <div><label>password</label><input id="s-db-pass" type="password" size="28"></div>
    </div>

    <p class="muted" style="margin:14px 0 6px">Application variables</p>
    <label>SUPABASE_URL</label>
    <input id="s-url" type="text" size="50" placeholder="http://your-supabase-host:3000">
    <label>SUPABASE_SERVICE_ROLE_KEY_CURRENT (stored DPAPI-encrypted; blank = keep current)</label>
    <input id="s-key" type="password" size="50">
    <label>SUPABASE_PUBLISHABLE_KEY (anon / public key; stored hidden, blank = keep current)</label>
    <input id="s-pub-key" type="password" size="50">
    <label>SESSION_SECRET_CURRENT (stored DPAPI-encrypted; blank = keep current)</label>
    <input id="s-session-secret" type="password" size="50">

    <hr style="margin:16px 0;border:none;border-top:1px solid var(--border,#ddd)">
    <b>Google Cloud Vision OCR</b>
    <p class="muted" style="margin:6px 0 10px">Reads the instrument Sample Set ID and batch "From" header out of each chromatograph PDF before it is forwarded.</p>
    <label><input id="s-ocr-enabled" type="checkbox"> Enable OCR extraction</label>
    <label style="margin-top:8px">Service-account JSON (stored DPAPI-encrypted; blank = keep current)</label>
    <textarea id="s-ocr-creds" rows="4" style="width:100%;max-width:640px;font-family:monospace;font-size:0.85em" placeholder='{"type":"service_account", ...}'></textarea>
    <div class="row" style="margin-top:10px">
      <button onclick="checkOcr()" id="s-ocr-btn">Test OCR connection</button>
      <span id="s-ocr-status" style="display:none;padding:6px 12px;border-radius:6px;font-size:0.9em;font-weight:500"></span>
    </div>

    <div class="row" style="margin-top:14px">
      <button onclick="saveSettings()">Save settings</button>
      <span id="s-msg" class="muted"></span>
    </div>
    <div id="s-conn-status" style="display:none;margin-top:10px;padding:8px 14px;border-radius:6px;font-size:0.92em;font-weight:500"></div>
  </div>
</section>
</main>
<script>
 const tok = () => localStorage.getItem('hub_admin') || '';
 // --- Admin session: auto-expires after 20 minutes of INACTIVITY. The token is
 // cleared from the browser and re-entry is required (an unattended dashboard
 // cannot be used by a walk-up after the timeout). Any click/keypress renews it.
 const SESSION_MS = 20*60*1000;
 function touchSession(){ localStorage.setItem('hub_admin_exp', String(Date.now()+SESSION_MS)); }
 function sessionExpired(){ const e=parseInt(localStorage.getItem('hub_admin_exp')||'0'); return tok() && e && Date.now()>e; }
 function expireSession(silent){
   localStorage.removeItem('hub_admin'); localStorage.removeItem('hub_admin_exp');
   const a=document.getElementById('admin'); if(a) a.value='';
   const m=document.getElementById('sessmsg'); if(m){ m.style.display=silent?'none':''; m.textContent='Session expired after 20 min of inactivity - re-enter the admin token.'; }
 }
 function saveTok(){ localStorage.setItem('hub_admin', document.getElementById('admin').value.trim());
   touchSession(); const m=document.getElementById('sessmsg'); if(m) m.style.display='none'; refreshAll(); }
 function hdr(){ return {'X-Admin-Token': tok(), 'Content-Type':'application/json'}; }
 function copy(t){ navigator.clipboard.writeText(t); }
 function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
 // Timestamps are stored in UTC; show them in India Standard Time (Asia/Kolkata).
 function fmtTime(iso){ if(!iso) return ''; const d=new Date(iso); if(isNaN(d)) return esc(iso);
   return d.toLocaleString('en-IN',{timeZone:'Asia/Kolkata',year:'numeric',month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:true})+' IST'; }
 function sizeH(n){ n=n||0; return n<1024?n+' B':(n<1048576?(n/1024).toFixed(1)+' KB':(n/1048576).toFixed(1)+' MB'); }
 let tab='devices', CATALOG=null, HELD={};
 function show(t){
   tab=t;
   for(const s of ['devices','docs','held','catalog','settings']){
     document.getElementById('sec-'+s).style.display = s===t?'':'none';
     document.getElementById('tb-'+(s==='docs'?'docs':s)).className = s===t?'on':'';
   }
   refreshAll();
 }
 async function api(path, opts){
   const r = await fetch(path, Object.assign({headers: hdr()}, opts||{}));
   if(!r.ok){ throw new Error((await r.json().catch(()=>({detail:r.status}))).detail || r.status); }
   return r.json();
 }
 async function loadDevices(){
   const tb=document.querySelector('#devices tbody');
   try{
     const rows = await api('/api/devices'); tb.innerHTML='';
     for(const d of rows){
       const tr=document.createElement('tr');
       tr.innerHTML='<td><b>'+esc(d.device_name)+'</b></td><td>'+esc(d.department_name||'')+'</td><td>'+esc(d.equipment_name||'')+'</td><td>'+esc(d.printer_name)+'</td><td>'+esc(d.hostname)+'</td>'+
         '<td><code>'+esc(d.ingest_url)+'</code> <span class="copy" onclick="copy(\\''+esc(d.ingest_url)+'\\')">[copy]</span></td>'+
         '<td>'+d.doc_count+'</td><td class="muted">'+esc(d.last_seen||'never')+'</td>'+
         '<td><span class="del" onclick="revoke('+d.id+',\\''+esc(d.device_name)+'\\')">[revoke]</span></td>';
       tb.appendChild(tr);
     }
     if(!rows.length) tb.innerHTML='<tr><td colspan=9 class="muted">No devices enrolled yet.</td></tr>';
   }catch(e){ tb.innerHTML='<tr><td colspan=9 class="muted">'+esc(e.message)+' (check admin token)</td></tr>'; }
 }
 async function showEnrollKey(){
   try{ const j = await api('/api/enroll-key');
     const el=document.getElementById('enrollkey'); el.style.display='';
     el.innerHTML='enroll key: <code>'+esc(j.enroll_key)+'</code> <span class="copy" onclick="copy(\\''+esc(j.enroll_key)+'\\')">[copy]</span>';
   }catch(e){ alert('Failed: '+e.message); }
 }
 async function revoke(id,name){
   if(!confirm('Revoke device "'+name+'"? Its printers will stop being able to upload.')) return;
   try{ await api('/api/devices/'+id,{method:'DELETE'}); loadDevices(); }catch(e){ alert('Failed: '+e.message); }
 }
 function stcls(s){ return 'st-'+s; }
 async function loadDocs(){
   const tb=document.querySelector('#docs tbody');
   try{
     const st=document.getElementById('stfilter').value;
     const rows = await api('/api/documents'+(st?'?status='+st:'')); tb.innerHTML='';
     for(const d of rows){
       const reg = d.registration_number ? esc(d.registration_number)+' / '+esc(d.test_method)+' / '+esc(d.test_parameter) : '<span class="muted">&mdash;</span>';
       const err = d.status==='forward_failed' && d.last_error ? '<div class="muted" style="font-size:11px">'+esc(d.last_error.slice(0,120))+'</div>' : '';
       const tr=document.createElement('tr');
       const dev = d.device_name ? esc(d.device_name)+' <span class="muted">('+esc(d.department_name||'?')+' / '+esc(d.equipment_name||'?')+')</span>' : '<span class="muted">&mdash;</span>';
       const orig = d.pdf_from ? '<div class="muted" style="font-size:11px">from: '+esc(d.pdf_from)+'</div>' : '';
       const stlabel = d.status==='extracting' ? 'Extracting PDF…' : esc(d.status);
       tr.innerHTML='<td>'+esc(d.pdf_name||d.pdf_from)+orig+'</td><td>'+dev+'</td>'+
         '<td>'+reg+'</td><td><span class="'+stcls(d.status)+'">'+stlabel+(d.held_reason?' &middot; '+esc(d.held_reason):'')+'</span>'+err+'</td>'+
         '<td class="muted">'+esc(d.printed_by)+'</td><td class="muted">'+sizeH(d.size)+'</td><td class="muted">'+fmtTime(d.received)+'</td>'+
         '<td><a href="#" onclick="dl(event,'+d.id+',\\''+esc(d.pdf_name||d.pdf_from)+'\\')">download</a></td>';
       tb.appendChild(tr);
     }
     if(!rows.length) tb.innerHTML='<tr><td colspan=8 class="muted">No documents.</td></tr>';
   }catch(e){ tb.innerHTML='<tr><td colspan=8 class="muted">'+esc(e.message)+' (check admin token)</td></tr>'; }
 }
 async function dl(e,id,name){
   e.preventDefault();
   const r = await fetch('/api/documents/'+id+'/file',{headers:{'X-Admin-Token':tok()}});
   if(!r.ok){ alert('Download failed: '+r.status); return; }
   const blob = await r.blob(); const u=URL.createObjectURL(blob);
   const a=document.createElement('a'); a.href=u; a.download=(name||'document')+(name&&name.toLowerCase().endsWith('.pdf')?'':'.pdf');
   a.click(); URL.revokeObjectURL(u);
 }
 function pdRows(){ return (CATALOG && CATALOG.rows) ? CATALOG.rows : []; }
 function uniq(a){ return [...new Set(a)].filter(Boolean).sort(); }
 // Mirror the server's _pd_available so the assign dropdowns only offer rows the
 // server would actually accept (a closed/completed row would be rejected 400).
 var PD_UNAVAIL=['closed','cancelled','canceled','complete','completed','done'];
 function pdAvail(s){ return PD_UNAVAIL.indexOf((''+(s||'')).trim().toLowerCase())<0; }
 function pdAvailRows(dept,equip){ return pdRows().filter(r=>r.department_name===dept&&r.equipment_name===equip&&pdAvail(r.status)); }
 function pdRegs(dept,equip){ return uniq(pdAvailRows(dept,equip).map(r=>r.registration_number)); }
 function pdMethods(dept,equip,reg){ return uniq(pdAvailRows(dept,equip).filter(r=>r.registration_number===reg).map(r=>r.test_method)); }
 function pdParams(dept,equip,reg,m){ return uniq(pdAvailRows(dept,equip).filter(r=>r.registration_number===reg&&r.test_method===m).map(r=>r.test_parameter)); }
 async function loadHeld(){
   const tb=document.querySelector('#held tbody');
   try{
     const [rows, cat] = await Promise.all([api('/api/documents?status=held'), api('/api/catalog')]);
     CATALOG = cat; HELD = {}; tb.innerHTML='';
     for(const d of rows){
       HELD[d.id] = {dept:d.department_name, equip:d.equipment_name};
       let opts='<option value="">select registration</option>';
       for(const rn of pdRegs(d.department_name,d.equipment_name)) opts+='<option value="'+esc(rn)+'">'+esc(rn)+'</option>';
       const tr=document.createElement('tr');
       tr.innerHTML='<td>'+esc(d.pdf_name||d.pdf_from)+'<div class="muted" style="font-size:11px">from: '+esc(d.pdf_from)+'<br>'+fmtTime(d.received)+' &middot; '+esc(d.printed_by)+'</div></td>'+
         '<td>'+(d.device_name?esc(d.device_name)+' <span class="muted">('+esc(d.department_name||'?')+' / '+esc(d.equipment_name||'?')+')</span>':'<span class="muted">&mdash;</span>')+'</td>'+
         '<td><span class="st-held">'+esc(d.held_reason||'')+'</span></td>'+
         '<td><select id="areg-'+d.id+'" onchange="fillMethods('+d.id+')">'+opts+'</select></td>'+
         '<td><select id="amethod-'+d.id+'" onchange="fillParams('+d.id+')"><option value="">select method</option></select></td>'+
         '<td><select id="aparam-'+d.id+'"><option value="">select parameter</option></select></td>'+
         '<td><button onclick="assign('+d.id+')">Assign</button> <a href="#" onclick="dl(event,'+d.id+',\\''+esc(d.pdf_name||d.pdf_from)+'\\')">download</a></td>';
       tb.appendChild(tr);
     }
     if(!rows.length) tb.innerHTML='<tr><td colspan=7 class="muted">Nothing held. All jobs filed cleanly.</td></tr>';
   }catch(e){ tb.innerHTML='<tr><td colspan=7 class="muted">'+esc(e.message)+' (check admin token)</td></tr>'; }
 }
 function fillMethods(id){
   const h=HELD[id]||{}; const reg=document.getElementById('areg-'+id).value;
   const sel=document.getElementById('amethod-'+id); sel.innerHTML='<option value="">select method</option>';
   document.getElementById('aparam-'+id).innerHTML='<option value="">select parameter</option>';
   if(!reg) return;
   for(const m of pdMethods(h.dept,h.equip,reg)) sel.innerHTML+='<option value="'+esc(m)+'">'+esc(m)+'</option>';
 }
 function fillParams(id){
   const h=HELD[id]||{}; const reg=document.getElementById('areg-'+id).value; const m=document.getElementById('amethod-'+id).value;
   const sel=document.getElementById('aparam-'+id); sel.innerHTML='<option value="">select parameter</option>';
   if(!reg||!m) return;
   for(const p of pdParams(h.dept,h.equip,reg,m)) sel.innerHTML+='<option value="'+esc(p)+'">'+esc(p)+'</option>';
 }
 async function assign(id){
   const reg=document.getElementById('areg-'+id).value;
   const m=document.getElementById('amethod-'+id).value;
   const p=document.getElementById('aparam-'+id).value;
   if(!reg||!m||!p){ alert('Pick registration, method and parameter first.'); return; }
   try{
     await api('/api/documents/'+id+'/assign',{method:'POST',body:JSON.stringify({registration_number:reg,test_method:m,test_parameter:p})});
     loadHeld(); loadDocs();
   }catch(e){ alert('Assign failed: '+e.message); }
 }
 async function loadCatalog(){
   const tb=document.querySelector('#catalog tbody');
   try{
     const [cat, st, sync] = await Promise.all([
       api('/api/catalog'), api('/api/settings'), api('/api/sync-status')
     ]);
     CATALOG=cat;
     document.getElementById('catver').textContent='version '+String(cat.version||'').slice(0,10);
     const errel=document.getElementById('syncerr');
     if(!st.supabase_configured){
       document.getElementById('catmode').textContent='LOCAL MODE (no Supabase configured): printer_data is empty until Supabase is set in Settings.';
       errel.style.display='none';
     } else {
       const rowInfo=sync.last_ok
         ? ' Last sync: '+sync.row_count+' row(s) fetched at '+fmtTime(sync.last_ok)+'.'
         : ' (no sync completed yet)';
       document.getElementById('catmode').textContent='Synced from Supabase printer_data every '+st.poll_seconds+' s.'+rowInfo;
       if(sync.last_error){
         errel.style.display=''; errel.style.background='#3a1a1a'; errel.style.color='#ff8080';
         errel.textContent='⚠ Sync error: '+sync.last_error;
       } else if(sync.last_ok && sync.row_count===0){
         // Sync worked (no error) but Supabase returned 0 rows — almost always RLS.
         errel.style.display=''; errel.style.background='#2d2500'; errel.style.color='#ffc107';
         errel.textContent='⚠ Supabase returned 0 rows even though data may exist. '
           +'This is usually caused by Row Level Security (RLS) blocking the service_role. '
           +'Fix in Supabase Studio → Table Editor → printer_data → RLS policies: '
           +'add a policy "FOR SELECT TO service_role USING (true)", '
           +'or run: ALTER ROLE service_role BYPASSRLS; in the SQL editor.';
       } else { errel.style.display='none'; }
     }
     const q=(document.getElementById('cat-q').value||'').toLowerCase();
     const rows=(cat.rows||[]).filter(r=>!q || [r.registration_number,r.department_name,r.equipment_name,r.test_method,r.test_parameter].some(v=>(v||'').toLowerCase().includes(q)));
     tb.innerHTML='';
     for(const r of rows){
       const tr=document.createElement('tr');
       tr.innerHTML='<td><b>'+esc(r.registration_number)+'</b></td><td>'+esc(r.department_name)+'</td><td>'+esc(r.equipment_name)+'</td>'+
         '<td>'+esc(r.status)+'</td><td>'+esc(r.test_method)+'</td><td>'+esc(r.test_parameter)+'</td>';
       tb.appendChild(tr);
     }
     if(!rows.length) tb.innerHTML='<tr><td colspan=6 class="muted">'+(q?'No rows match the filter.':'printer_data is empty.')+'</td></tr>';
   }catch(e){ tb.innerHTML='<tr><td colspan=6 class="muted">'+esc(e.message)+' (check admin token)</td></tr>'; }
 }
 async function syncNow(){
   const btn=document.getElementById('syncnow-btn');
   btn.disabled=true; btn.textContent='Syncing…';
   try{
     const r=await api('/api/catalog/sync',{method:'POST'});
     btn.textContent='Sync now';
     if(r.last_error){
       alert('Sync error: '+r.last_error);
     } else {
       loadCatalog();
     }
   }catch(e){
     btn.textContent='Sync now';
     alert('Sync failed: '+e.message);
   }
   btn.disabled=false;
 }
 async function loadSettings(){
   try{
     const s = await api('/api/settings');
     document.getElementById('s-dir').value=s.lims_docs_dir||'';
     document.getElementById('s-url').value=s.supabase_url;
     document.getElementById('s-key').value='';
     document.getElementById('s-key').placeholder=s.supabase_service_key?'(key set - blank keeps it)':'(no key set)';
     document.getElementById('s-poll').value=s.poll_seconds;
     document.getElementById('s-db-host').value=s.supabase_db_host||'';
     document.getElementById('s-db-port').value=s.supabase_db_port||5432;
     document.getElementById('s-db-name').value=s.supabase_db_name||'';
     document.getElementById('s-db-user').value=s.supabase_db_user||'';
     document.getElementById('s-db-pass').value='';
     document.getElementById('s-db-pass').placeholder=s.supabase_db_password_set?'(set - blank keeps it)':'(no password set)';
     document.getElementById('s-pub-key').value='';
     document.getElementById('s-pub-key').placeholder=s.supabase_publishable_key_set?'(set - blank keeps it)':'(no key set)';
     document.getElementById('s-session-secret').value='';
     document.getElementById('s-session-secret').placeholder=s.supabase_session_secret_set?'(set - blank keeps it)':'(no secret set)';
     document.getElementById('s-ocr-enabled').checked=!!s.google_ocr_enabled;
     document.getElementById('s-ocr-creds').value='';
     document.getElementById('s-ocr-creds').placeholder=s.google_ocr_credentials_set?'(credentials set - blank keeps them)':'{"type":"service_account", ...}';
     if(s.google_ocr_enabled && s.google_ocr_credentials_set){ checkOcr(); }
     else { const b=document.getElementById('s-ocr-status'); b.style.display='none'; }
   }catch(e){ document.getElementById('s-msg').textContent=e.message; }
 }
 async function saveSettings(){
   try{
     await api('/api/settings',{method:'POST',body:JSON.stringify({
       lims_docs_dir: document.getElementById('s-dir').value,
       supabase_url: document.getElementById('s-url').value,
       supabase_service_key: document.getElementById('s-key').value,
       poll_seconds: parseInt(document.getElementById('s-poll').value)||2,
       supabase_db_host: document.getElementById('s-db-host').value,
       supabase_db_port: parseInt(document.getElementById('s-db-port').value)||5432,
       supabase_db_name: document.getElementById('s-db-name').value,
       supabase_db_user: document.getElementById('s-db-user').value,
       supabase_db_password: document.getElementById('s-db-pass').value,
       supabase_publishable_key: document.getElementById('s-pub-key').value,
       supabase_session_secret: document.getElementById('s-session-secret').value,
       google_ocr_enabled: document.getElementById('s-ocr-enabled').checked,
       google_ocr_credentials: document.getElementById('s-ocr-creds').value,
     })});
     document.getElementById('s-msg').textContent='Saved. Testing connection…';
     loadSettings();
     await checkConnection();
     await checkOcr();
     document.getElementById('s-msg').textContent='Saved.';
   }catch(e){ document.getElementById('s-msg').textContent='Save failed: '+e.message; }
 }
 async function checkConnection(){
   const box=document.getElementById('s-conn-status');
   box.style.display=''; box.textContent='Testing connection…';
   box.style.background='#f0f0f0'; box.style.color='inherit';
   try{
     const r=await api('/api/settings/test-connection');
     if(r.connected){
       if(r.rls_warning){
         box.textContent='✔ Connected — ⚠ '+r.rls_warning;
         box.style.background='#2d2500'; box.style.color='#ffc107';
       } else {
         box.textContent='✔ Connected';
         box.style.background='#d4edda'; box.style.color='#155724';
       }
     } else {
       box.textContent='✘ Not Connected'+(r.error?': '+r.error:'');
       box.style.background='#f8d7da'; box.style.color='#721c24';
     }
   }catch(e){
     box.textContent='✘ Not Connected: '+e.message;
     box.style.background='#f8d7da'; box.style.color='#721c24';
   }
 }
 async function checkOcr(){
   const box=document.getElementById('s-ocr-status');
   box.style.display=''; box.textContent='Testing Google Vision…';
   box.style.background='#f0f0f0'; box.style.color='inherit';
   try{
     const r=await api('/api/settings/test-ocr');
     if(r.ok){
       box.textContent='✔ OCR connected — Google Vision ready';
       box.style.background='#d4edda'; box.style.color='#155724';
     } else {
       box.textContent='✘ OCR not working: '+(r.error||'unknown error');
       box.style.background='#f8d7da'; box.style.color='#721c24';
     }
   }catch(e){
     box.textContent='✘ OCR test failed: '+e.message;
     box.style.background='#f8d7da'; box.style.color='#721c24';
   }
 }
 function refreshAll(){
   if(tab==='devices') loadDevices();
   else if(tab==='docs') loadDocs();
   else if(tab==='held') loadHeld();
   else if(tab==='catalog') loadCatalog();
   else if(tab==='settings') loadSettings();
 }
 // Auto-refresh must not clobber a dropdown/field the operator is using (esp. the
 // Held tab's Assign selects). Skip a cycle while a form control is focused or was
 // touched in the last 8 s; the manual Refresh button always works.
 let lastTouch = 0;
 document.addEventListener('focusin', e => { const t=e.target.tagName; if(t==='SELECT'||t==='INPUT'||t==='TEXTAREA') lastTouch=Date.now(); });
 document.addEventListener('change',  e => { const t=e.target.tagName; if(t==='SELECT'||t==='INPUT'||t==='TEXTAREA') lastTouch=Date.now(); });
 // Any real interaction renews the 20-min admin session.
 for(const ev of ['click','keydown','change']) document.addEventListener(ev, ()=>{ if(tok()) touchSession(); });
 function interacting(){
   const a=document.activeElement, t=a&&a.tagName;
   return (t==='SELECT'||t==='INPUT'||t==='TEXTAREA') || (Date.now()-lastTouch < 8000);
 }
 // Poll so new prints appear quickly while the docs/held tabs are open. First check
 // the session: if it has timed out, clear it and stop (re-entry required).
 setInterval(()=>{
   if(sessionExpired()){ expireSession(false); refreshAll(); return; }
   if(interacting()) return;
   if(tab==='docs') loadDocs(); else if(tab==='held') loadHeld();
 }, 3000);
 // On load: honor an expired session; otherwise start/renew the timer.
 if(sessionExpired()) expireSession(true); else if(tok()) touchSession();
 document.getElementById('admin').value = tok(); refreshAll();
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD)


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("HUB_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.environ.get("HUB_PORT", "8000"))
    quiet = _quiet_mode()
    lines = ["=" * 70, " LIMS Print Hub",
             " Dashboard  : http://localhost:%d/  (LAN: http://<this-ip>:%d/)" % (port, port),
             " Data dir   : %s" % DATA_DIR,
             " limsDocs   : %s" % (lims_docs_dir() or "(not set - Supabase only, no local tree)"),
             " Supabase   : %s" % ("configured" if supabase_configured() else "not configured (local mode)")]
    if quiet:
        # Production / service mode: keep secrets out of the console + service log.
        lines += [" ADMIN TOKEN: (hidden - see admin_token.txt in the data dir)",
                  " ENROLL KEY : (hidden - see enroll_key.txt, or dashboard > Devices > Show enroll key)"]
    else:
        lines += [" ADMIN TOKEN: %s   (paste into the dashboard)" % ADMIN_TOKEN,
                  " ENROLL KEY : %s   (give to setup.ps1 when enrolling printers)" % ENROLL_KEY]
    lines.append("=" * 70)
    print("\n".join(lines), flush=True)  # flush so a service log shows startup promptly
    # 0.0.0.0 by default: the 60 client PCs must reach this over the LAN.
    uvicorn.run(app, host=host, port=port, log_level=("warning" if quiet else "info"))
