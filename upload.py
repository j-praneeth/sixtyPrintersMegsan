#!/usr/bin/env python3
"""
Virtual Cloud Printer - job uploader.

This script is launched by the mfilemon / clawmon print-port monitor for every
print job that is sent to one of our virtual printers.

The port monitor calls us (see setup.ps1, the registry "UserCommand" value) as:

    pythonw.exe upload.py  "<ps_file>" "<job_id>" "<printer_name>" "<user>" "<document_name>"

Flow for one job:
    1. mfilemon has already written the raw PostScript of the job to <ps_file>.
    2. We convert that PostScript to a PDF using Ghostscript.
    3. We look up which HTTPS URL belongs to <printer_name> in config.json.
    4. We POST the PDF (multipart/form-data) to that URL together with the
       document name.
    5. On success we delete the temp files; on failure we keep the PDF under
       .\\failed\\ so nothing is ever lost.

Everything is wrapped so the script NEVER raises out to the spooler and always
leaves a trace in log.txt next to this file. It uses only the Python standard
library, so the virtual-env created by `uv` needs no extra packages.
"""

import sys
import os
import io
import re
import json
import time
import uuid
import ssl
import hashlib
import subprocess
import traceback
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime

# --------------------------------------------------------------------------- #
# Paths (everything lives next to this file, in %ProgramData%\VirtualCloudPrinter)
# --------------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH = os.path.join(BASE_DIR, "log.txt")
FAILED_DIR = os.path.join(BASE_DIR, "failed")
IDS_DIR = os.path.join(BASE_DIR, "ids")  # per-user pending id written by set-id.bat


def log(message):
    """Append a timestamped line to log.txt (best-effort, never throws)."""
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message)
    try:
        with io.open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    # Also print for the case where a console python.exe is used for debugging.
    try:
        print(line)
    except Exception:
        pass


def load_config():
    # utf-8-sig transparently strips a UTF-8 BOM if one is present (Windows
    # PowerShell tools often add one) and still reads BOM-less files fine.
    with io.open(CONFIG_PATH, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def sanitize_userfile(name):
    """Map a Windows user name to a safe filename stem (must match set-id.ps1)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name or "")


def read_pending_id(user_name):
    """Return (reg_id, once) for the value the user set via set-id.bat /
    print-register.bat, or (None, False) if there is none.

    The value lives in ids\\<user>.id as JSON {"id": ..., "once": bool}. This
    function only READS it - the caller decides when to consume a once-id (via
    consume_pending_id), so it can first choose whether to honor it. A plain
    (non-JSON) file is treated as a sticky id (once=False).
    """
    if not user_name:
        return (None, False)
    path = os.path.join(IDS_DIR, sanitize_userfile(user_name) + ".id")
    if not os.path.isfile(path):
        return (None, False)
    try:
        with io.open(path, "r", encoding="utf-8-sig") as fh:
            raw = fh.read().strip()
    except Exception:
        return (None, False)
    if not raw:
        return (None, False)
    reg_id, once = raw, False
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            reg_id = str(obj.get("id", "")).strip()
            once = bool(obj.get("once", False))
    except Exception:
        pass  # not JSON -> treat the whole file as the id (sticky)
    if not reg_id:
        return (None, False)
    return (reg_id, once)


def consume_pending_id(user_name):
    """Delete the user's pending id file (a 'once' id has now been used)."""
    if not user_name:
        return
    path = os.path.join(IDS_DIR, sanitize_userfile(user_name) + ".id")
    try:
        os.remove(path)
    except Exception:
        pass


def is_hub_printer(printer_cfg):
    """A printer entry with BOTH device_name and token is a "hub printer" (catalog
    prompt + full metadata field set, per ARCHITECTURE.md). Anything else is a
    legacy printer and must behave exactly as before."""
    cfg = printer_cfg or {}
    return bool(str(cfg.get("device_name") or "").strip()) and \
        bool(str(cfg.get("token") or "").strip())


def fetch_catalog(config, printer_cfg):
    """GET <hub>/catalog with the printer's X-Device-Token (the hub derives the
    department+equipment from the token and returns the cascading catalog).

    3 s timeout (kept short so a slow/unreachable hub does not delay the dialog;
    ARCHITECTURE.md 6c). Returns the parsed catalog dict, or None on ANY failure
    (logged, never raises) - the prompt then loads the share copy under
    catalog_share_dir itself (it runs as the interactive user, who can reach the
    UNC share even when this SYSTEM process cannot reach the hub over HTTP).
    """
    try:
        hub = config.get("hub") or {}
        base = str(hub.get("base_url") or "").strip().rstrip("/")
        if not base:
            log("Catalog fetch skipped: no hub.base_url configured.")
            return None
        # The hub derives department+equipment from the device token, so no query
        # is needed; it returns the cascading registration/method/parameter tree.
        url = "%s/catalog" % base
        headers = {"X-Device-Token": str(printer_cfg.get("token") or "")}
        request = urllib.request.Request(url, headers=headers, method="GET")
        ctx = None
        if url.lower().startswith("https"):
            ctx = ssl.create_default_context()
            if not bool(hub.get("verify_tls", config.get("verify_tls", True))):
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(request, timeout=3, context=ctx) as resp:
            status = getattr(resp, "status", resp.getcode())
            data = resp.read()
        if not (200 <= status < 300):
            raise RuntimeError("HTTP %s" % status)
        obj = json.loads(data.decode("utf-8"))
        if not isinstance(obj, dict):
            raise RuntimeError("catalog response is not a JSON object")
        log("Catalog fetched from hub (%d registrations)."
            % len(obj.get("registrations") or []))
        return obj
    except Exception as exc:
        log("Catalog fetch failed (prompt will use the share fallback): %s" % exc)
        return None


def prompt_registration_in_user_session(out_file, doc_name, timeout_sec=180,
                                        department=None, equipment=None,
                                        catalog_file=None, fallback_dir=None,
                                        hub_url=None,
                                        batch_note=False):
    """Pop the registration dialog on the interactive user's screen and return
    (reg_id, test_method, test_parameter, cancelled).

    cancelled=True means the user EXPLICITLY dismissed the dialog (Cancel button /
    closed the window): the dialog wrote {"cancel":true} and the caller must
    DISCARD the job - nothing is uploaded, nothing reaches the hub. A timeout or
    any infrastructure failure (no interactive session, a Win32 call fails) is
    NOT a cancel: it returns (None, None, None, False) and NEVER raises, so an
    unattended print still proceeds (hub printers are HELD, never lost).

    upload.py runs as SYSTEM in session 0, so it cannot show UI directly - it must
    launch the prompt INTO the active console session with the user's token
    (WTSQueryUserToken + CreateProcessAsUser).

    department/equipment/catalog_file/fallback_dir switch prompt-id.ps1 into hub
    mode (the 3 cascading dropdowns instead of free text); all None keeps the
    legacy dialog. batch_note=True tells the dialog its answer applies to the
    whole print batch.
    """
    if os.name != "nt":
        return (None, None, None, False)
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        a32 = ctypes.WinDLL("advapi32", use_last_error=True)
        w32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
        env_api = ctypes.WinDLL("userenv", use_last_error=True)

        class STARTUPINFO(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                        ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                        ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

        # NB: WTSGetActiveConsoleSessionId lives in kernel32, NOT wtsapi32.
        k32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
        w32.WTSQueryUserToken.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        w32.WTSQueryUserToken.restype = wintypes.BOOL
        a32.DuplicateTokenEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                         ctypes.c_int, ctypes.c_int, ctypes.POINTER(wintypes.HANDLE)]
        a32.DuplicateTokenEx.restype = wintypes.BOOL
        env_api.CreateEnvironmentBlock.argtypes = [ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.BOOL]
        env_api.CreateEnvironmentBlock.restype = wintypes.BOOL
        env_api.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
        a32.CreateProcessAsUserW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR,
                                             ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                                             ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFO),
                                             ctypes.POINTER(PROCESS_INFORMATION)]
        a32.CreateProcessAsUserW.restype = wintypes.BOOL
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.GetCurrentProcess.restype = wintypes.HANDLE

        # WTSQueryUserToken needs SeTcbPrivilege and CreateProcessAsUser needs
        # SeAssignPrimaryTokenPrivilege + SeIncreaseQuotaPrivilege. SYSTEM holds
        # them but they are often present-but-DISABLED in the spooler's token, which
        # makes those calls fail (1314 ERROR_PRIVILEGE_NOT_HELD) - so enable them.
        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

        a32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        a32.OpenProcessToken.restype = wintypes.BOOL
        a32.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
        a32.LookupPrivilegeValueW.restype = wintypes.BOOL
        a32.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL,
                                              ctypes.POINTER(TOKEN_PRIVILEGES), wintypes.DWORD,
                                              ctypes.c_void_p, ctypes.c_void_p]
        a32.AdjustTokenPrivileges.restype = wintypes.BOOL

        def _enable_privileges(names):
            TOKEN_ADJUST_PRIVILEGES, TOKEN_QUERY, SE_PRIVILEGE_ENABLED = 0x20, 0x8, 0x2
            htok = wintypes.HANDLE()
            if not a32.OpenProcessToken(k32.GetCurrentProcess(),
                                        TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(htok)):
                return
            try:
                for nm in names:
                    luid = LUID()
                    if not a32.LookupPrivilegeValueW(None, nm, ctypes.byref(luid)):
                        continue
                    tp = TOKEN_PRIVILEGES()
                    tp.PrivilegeCount = 1
                    tp.Privileges[0].Luid = luid
                    tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
                    a32.AdjustTokenPrivileges(htok, False, ctypes.byref(tp), 0, None, None)
            finally:
                k32.CloseHandle(htok)

        _enable_privileges(["SeTcbPrivilege", "SeAssignPrimaryTokenPrivilege",
                            "SeIncreaseQuotaPrivilege"])

        INVALID_SESSION = 0xFFFFFFFF
        session_id = k32.WTSGetActiveConsoleSessionId()
        if session_id == INVALID_SESSION:
            log("Registration prompt: no interactive session; skipping.")
            return (None, None, None, False)

        user_tok = wintypes.HANDLE()
        if not w32.WTSQueryUserToken(session_id, ctypes.byref(user_tok)):
            log("Registration prompt: WTSQueryUserToken failed (%d)." % ctypes.get_last_error())
            return (None, None, None, False)

        primary = wintypes.HANDLE()
        env_block = ctypes.c_void_p()
        try:
            TOKEN_ALL_ACCESS = 0xF01FF
            SecurityImpersonation, TokenPrimary = 2, 1
            if not a32.DuplicateTokenEx(user_tok, TOKEN_ALL_ACCESS, None,
                                        SecurityImpersonation, TokenPrimary, ctypes.byref(primary)):
                log("Registration prompt: DuplicateTokenEx failed (%d)." % ctypes.get_last_error())
                return (None, None, None, False)
            env_api.CreateEnvironmentBlock(ctypes.byref(env_block), primary, False)

            ps_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                  "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
            script = os.path.join(BASE_DIR, "prompt-id.ps1")
            safe_doc = (doc_name or "").replace('"', "'")
            cmdline = ('"%s" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden '
                       '-File "%s" -OutFile "%s" -DocName "%s"'
                       % (ps_exe, script, out_file, safe_doc))
            if department or equipment:
                # Hub printer: the dialog shows the 3 cascading dropdowns.
                # -CatalogFile is only passed when upload.py wrote one (hub fetch
                # OK); -FallbackDir always goes along so the user-session prompt
                # can load <FallbackDir>\<dept>__<equipment>.json from the share.
                if department:
                    cmdline += ' -Department "%s"' % department
                if equipment:
                    cmdline += ' -Equipment "%s"' % equipment
                if catalog_file and os.path.isfile(catalog_file):
                    cmdline += ' -CatalogFile "%s"' % catalog_file
                cmdline += ' -FallbackDir "%s"' % (fallback_dir or "")
                if hub_url:
                    cmdline += ' -HubUrl "%s"' % hub_url.replace('"', "'")
            if batch_note:
                cmdline += ' -BatchNote'
            # Tell the dialog to close itself shortly BEFORE we stop waiting, so
            # an answer can never be written after we have already given up (it
            # would be silently discarded and the job would go out without a
            # number - the exact failure this margin prevents).
            dialog_timeout = max(10, int(timeout_sec) - 10)
            cmdline += ' -TimeoutSec %d' % dialog_timeout
            cmd_buf = ctypes.create_unicode_buffer(cmdline)

            si = STARTUPINFO()
            si.cb = ctypes.sizeof(si)
            si.lpDesktop = "winsta0\\default"
            pi = PROCESS_INFORMATION()
            CREATE_UNICODE_ENVIRONMENT, CREATE_NO_WINDOW = 0x00000400, 0x08000000
            ok = a32.CreateProcessAsUserW(primary, None, cmd_buf, None, None, False,
                                          CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
                                          env_block, BASE_DIR, ctypes.byref(si), ctypes.byref(pi))
            if not ok:
                log("Registration prompt: CreateProcessAsUser failed (%d)." % ctypes.get_last_error())
                return (None, None, None, False)
            log("Registration prompt: launched dialog in session %d (pid %d); waiting."
                % (session_id, pi.dwProcessId))
            try:
                WAIT_TIMEOUT = 0x102
                rc = k32.WaitForSingleObject(pi.hProcess, int(timeout_sec * 1000))
                if rc == WAIT_TIMEOUT:
                    # The dialog should have closed itself (-TimeoutSec) - but if
                    # it is somehow still up, kill it so stale dialogs never
                    # linger/stack and a late answer cannot be silently orphaned.
                    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
                    k32.TerminateProcess(pi.hProcess, 1)
                    log("Registration prompt: dialog timed out after %ds; closed "
                        "it and continuing without an answer." % int(timeout_sec))
            finally:
                k32.CloseHandle(pi.hThread)
                k32.CloseHandle(pi.hProcess)
        finally:
            if env_block:
                env_api.DestroyEnvironmentBlock(env_block)
            if primary:
                k32.CloseHandle(primary)
            k32.CloseHandle(user_tok)
    except Exception as exc:
        log("Registration prompt error (ignored): %s" % exc)
        return (None, None, None, False, None)

    # Read whatever the dialog wrote and consume it. JSON
    # {"registration_number","test_method","test_parameter"} = a hub answer;
    # {"id"} = a legacy free-text answer; {"cancel":true} = the user explicitly
    # dismissed the dialog (strict mode: the caller discards the job); no file =
    # timeout / infra failure.
    try:
        if os.path.isfile(out_file):
            with io.open(out_file, "r", encoding="utf-8-sig") as fh:
                raw = fh.read().strip()
            try:
                os.remove(out_file)
            except Exception:
                pass
            if raw:
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        if obj.get("cancel"):
                            return (None, None, None, True, None)
                        reg_val = str(obj.get("registration_number",
                                              obj.get("id", ""))).strip() or None
                        method_val = str(obj.get("test_method", "")).strip() or None
                        param_val = str(obj.get("test_parameter",
                                                obj.get("test", ""))).strip() or None
                        calibration_val = str(obj.get("calibration", "")).strip() or None
                        return (reg_val, method_val, param_val, False, calibration_val)
                except Exception:
                    pass  # not JSON -> treat the whole file as the id (legacy)
                return (raw or None, None, None, False, None)
    except Exception:
        pass
    return (None, None, None, False)


def show_registration_prompt(config, printer_cfg, user_name, job_id, doc_name,
                             batch_note=False):
    """Show the registration dialog for ONE job and return
    (reg_id, test_method, test_parameter, cancelled).

    Hub printers get the catalog dropdowns (fetching the catalog as SYSTEM first,
    parking it in ids\\ for the user-session dialog); legacy printers get the
    free-text dialog and always return test=None. cancelled=True only on an
    EXPLICIT user cancel (the caller discards the job); timeout / any failure
    stays best-effort: (None, None, False)."""
    stem = sanitize_userfile(user_name) or "user"
    out_file = os.path.join(IDS_DIR, "%s.%s.prompt" % (stem, job_id or "job"))
    try:
        timeout = float(config.get("prompt_timeout_seconds", 180))
    except Exception:
        timeout = 180.0
    if is_hub_printer(printer_cfg):
        # Fetch the catalog as SYSTEM (5 s cap) and park it in ids\ (the only
        # user-readable spot under $Base) so the user-session dialog can read
        # it. Fetch failure is fine - the dialog falls back to the share copy.
        catalog_file = None
        catalog = fetch_catalog(config, printer_cfg)
        if catalog is not None:
            catalog_file = os.path.join(
                IDS_DIR, "%s.%s.catalog.json" % (stem, job_id or "job"))
            try:
                # BOM-less UTF-8 (python never writes a BOM with 'utf-8').
                with io.open(catalog_file, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(catalog))
            except Exception as exc:
                log("Could not write catalog temp file (ignored): %s" % exc)
                catalog_file = None
        ingest_url = str(printer_cfg.get("url") or "").strip()
        hub_base_url = ""
        if ingest_url:
            try:
                import urllib.parse as _up
                _p = _up.urlparse(ingest_url)
                hub_base_url = "%s://%s" % (_p.scheme, _p.netloc)
            except Exception:
                pass
        try:
            return prompt_registration_in_user_session(
                out_file, doc_name, timeout_sec=timeout,
                department=str(printer_cfg.get("department_name") or "").strip(),
                equipment=str(printer_cfg.get("equipment_name") or "").strip(),
                catalog_file=catalog_file,
                fallback_dir=str(config.get("catalog_share_dir") or "").strip(),
                hub_url=hub_base_url,
                batch_note=batch_note)
        finally:
            if catalog_file:
                try:
                    os.remove(catalog_file)
                except Exception:
                    pass
    reg_id, _m, _p, cancelled, _c = prompt_registration_in_user_session(
        out_file, doc_name, timeout_sec=timeout, batch_note=batch_note)
    return reg_id, None, None, cancelled, None  # legacy printers carry no method/param/calibration


# --------------------------------------------------------------------------- #
# Batch prompt coalescing.
#
# When an application prints N selected PDFs in one go, the spooler runs one
# upload.py per job, near-simultaneously (mfilemon WaitTermination=0) - which
# used to mean N registration dialogs. These helpers make the first job per
# (user, printer) the batch LEADER: it shows ONE dialog and records the answer
# in ids\<user>.<printer>.batch.json; every job that arrives while that dialog
# is open, or within prompt_batch_grace_seconds of it being ANSWERED, is a
# FOLLOWER and silently reuses the answer (including "cancelled" - one Cancel
# discards the whole batch). The grace window is measured from the ANSWER and
# kept to a few seconds so it only catches the tail of the same burst of jobs;
# the next document the user prints re-prompts rather than silently inheriting
# the previous registration number (which would be wrong data in the LIMS).
#
# All of it is best-effort file-based coordination inside ids\ (the one
# user-writable dir, same trust level as the .id files): a crashed leader's
# lock goes stale and is broken by a follower, and every failure path degrades
# to (None, None) - the job proceeds and the hub holds it, never blocks.
# --------------------------------------------------------------------------- #

def _batch_paths(user_name, printer_name):
    """State + lock file for one (user, printer) batch. Keyed per printer so a
    hub and a legacy printer (different dialogs, different catalogs) never
    share an answer."""
    stem = sanitize_userfile(user_name) or "user"
    pstem = sanitize_userfile(printer_name) or "printer"
    base = os.path.join(IDS_DIR, "%s.%s.batch" % (stem, pstem))
    return base + ".json", base + ".lock"


def _read_batch_state(path):
    try:
        with io.open(path, "r", encoding="utf-8-sig") as fh:
            obj = json.loads(fh.read())
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _write_batch_state(path, obj):
    """Atomic write (tmp + os.replace) so a follower never reads a half-written
    answer. Only the lock holder writes, so the .tmp name cannot collide."""
    try:
        tmp = path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(obj))
        os.replace(tmp, path)
    except Exception as exc:
        log("Batch state write failed (ignored): %s" % exc)


def _try_batch_lock(lock_path, stale_after):
    """Try to become the batch leader (O_CREAT|O_EXCL). A healthy leader always
    deletes the lock when its dialog closes, so a lock older than stale_after
    means the leader died mid-prompt - break it and take over."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(lock_path, flags)
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(lock_path) <= stale_after:
                return False
            os.remove(lock_path)
            fd = os.open(lock_path, flags)
        except Exception:
            return False
    except Exception:
        return False
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    except Exception:
        pass
    finally:
        os.close(fd)
    return True


def prompt_with_batching(config, printer_cfg, printer_name, user_name, job_id,
                         doc_name):
    """One dialog per print BATCH: elect a leader, everyone else reuses its
    answer. Returns (reg_id, test_method, test_parameter, cancelled) exactly like
    show_registration_prompt; a leader's Cancel cancels every follower too."""
    state_path, lock_path = _batch_paths(user_name, printer_name)
    # Post-answer join window: only jobs from the SAME burst (launched moments
    # after the leader's dialog was answered) may reuse the answer; anything
    # later is a new print and must re-prompt.
    try:
        grace = float(config.get("prompt_batch_grace_seconds", 1))
    except Exception:
        grace = 1.0
    try:
        timeout = float(config.get("prompt_timeout_seconds", 180))
    except Exception:
        timeout = 180.0
    # A healthy leader releases the lock within timeout + a few seconds of
    # launch overhead; give followers a little longer so one of them can
    # recover from a leader that died mid-prompt.
    stale_after = timeout + 15
    deadline = time.time() + timeout + 30

    while True:
        state = _read_batch_state(state_path)
        if state:
            try:
                started = float(state.get("started") or 0)
            except Exception:
                started = 0.0
            try:
                answered = float(state.get("answered") or 0)
            except Exception:
                answered = 0.0
            status = str(state.get("status") or "")
            # A follower may reuse the answer if it arrives quickly after the
            # leader answered (grace window), OR if it is a slow GS job that
            # finished conversion after the grace window but is still within the
            # batch session (started within stale_after seconds of the leader).
            # The second condition covers large batches (200+ files) where CPU
            # contention means some GS conversions complete well after the user
            # clicked OK.
            _same_burst = (answered and time.time() <= answered + grace) or \
                          (started and time.time() <= started + stale_after)
            if status == "done" and _same_burst:
                if state.get("cancel"):
                    log("Batch prompt: job %s follows the batch CANCEL from job "
                        "%s - discarding this job too." % (job_id, state.get("job")))
                    return None, None, None, True, None
                reg = str(state.get("id") or "").strip() or None
                method = str(state.get("method") or "").strip() or None
                param = str(state.get("param") or "").strip() or None
                cal = str(state.get("calibration") or "").strip() or None
                log("Batch prompt: job %s reusing the batch answer from job %s "
                    "(reg=%r%s)." % (job_id, state.get("job"), reg or "",
                                     "" if reg else " - leader gave no number, "
                                     "whole batch proceeds without one"))
                return reg, method, param, False, cal
            if status == "prompting" and time.time() <= started + stale_after:
                # The leader's dialog is on screen; wait for its answer.
                if time.time() >= deadline:
                    break
                time.sleep(0.5)
                continue
            # Anything else (expired answer, stale prompting) -> try to lead.
        if _try_batch_lock(lock_path, stale_after):
            try:
                # Double-check: a peer may have written the answer between our
                # read above and winning the lock.
                state = _read_batch_state(state_path)
                if state and str(state.get("status") or "") == "done":
                    try:
                        answered = float(state.get("answered") or 0)
                    except Exception:
                        answered = 0.0
                    try:
                        _dc_started = float(state.get("started") or 0)
                    except Exception:
                        _dc_started = 0.0
                    _dc_same_burst = (answered and time.time() <= answered + grace) or \
                                     (_dc_started and time.time() <= _dc_started + stale_after)
                    if _dc_same_burst:
                        if state.get("cancel"):
                            log("Batch prompt: job %s follows the batch CANCEL "
                                "from job %s." % (job_id, state.get("job")))
                            return None, None, None, True, None
                        reg = str(state.get("id") or "").strip() or None
                        method = str(state.get("method") or "").strip() or None
                        param = str(state.get("param") or "").strip() or None
                        cal = str(state.get("calibration") or "").strip() or None
                        log("Batch prompt: job %s reusing the batch answer from "
                            "job %s (reg=%r)." % (job_id, state.get("job"), reg or ""))
                        return reg, method, param, False, cal
                started = time.time()
                _write_batch_state(state_path, {
                    "status": "prompting", "job": job_id, "started": started})
                log("Batch prompt: job %s is the batch leader - showing one "
                    "dialog for the whole batch." % job_id)
                reg, method, param, cancelled, cal = show_registration_prompt(
                    config, printer_cfg, user_name, job_id, doc_name,
                    batch_note=True)
                _write_batch_state(state_path, {
                    "status": "done", "job": job_id, "started": started,
                    "answered": time.time(),
                    "id": reg or "", "method": method or "", "param": param or "",
                    "calibration": cal or "", "cancel": bool(cancelled)})
                return reg, method, param, cancelled, cal
            finally:
                try:
                    os.remove(lock_path)
                except Exception:
                    pass
        if time.time() >= deadline:
            break
        time.sleep(0.5)
    log("Batch prompt: job %s gave up waiting for the batch leader; continuing "
        "without a prompted number." % job_id)
    return None, None, None, False, None


def get_registration_id(config, printer_cfg, printer_name, user_name, job_id,
                        doc_name):
    """Resolve the registration number (and, for hub printers, the test) for this
    job: prompt the user at print time if enabled (coalescing concurrent jobs
    into one dialog unless prompt_batch is false), else fall back to a value
    pre-set via set-id/print-register. Returns (reg_id, test_method,
    test_parameter, cancelled); method/parameter are
    always None for legacy printers. cancelled=True (the user pressed Cancel /
    closed the dialog) means the job must be DISCARDED - it wins over any pre-set
    sticky id."""
    # An explicit per-job number pre-set via set-id / print-register (once:true)
    # is a decision the user has ALREADY made for THIS job - honor it and skip
    # the prompt entirely. Without this, the "Print & Register" batch tool
    # (which pre-writes one number per file) would pop a registration dialog for
    # every file and stall each job for the whole prompt timeout; worse, if the
    # user typed a number into that dialog it would OVERRIDE the per-file number
    # the tool assigned. A sticky id (once:false) keeps the old ordering below
    # (prompt first, sticky only as the fallback).
    pending, once = read_pending_id(user_name)
    if pending and once:
        consume_pending_id(user_name)
        log("Using pre-set registration id %r (prompt skipped)." % pending)
        return pending, None, None, False, None

    reg_id, method, param, cancelled, calibration = None, None, None, False, None
    if config.get("prompt_registration", True):
        if config.get("prompt_batch", True):
            reg_id, method, param, cancelled, calibration = prompt_with_batching(
                config, printer_cfg, printer_name, user_name, job_id, doc_name)
        else:
            reg_id, method, param, cancelled, calibration = show_registration_prompt(
                config, printer_cfg, user_name, job_id, doc_name)
    if cancelled:
        return None, None, None, True, None  # explicit cancel beats any sticky fallback
    if not reg_id and pending:
        reg_id = pending  # sticky fallback (left in place, applies again next time)
    return reg_id, method, param, False, calibration


def sanitize_filename(name, default="document"):
    """Make a string safe to use as an upload filename and ensure a .pdf suffix."""
    if not name:
        name = default
    # Strip characters that are illegal in Windows filenames / HTTP headers.
    bad = '<>:"/\\|?*\r\n\t'
    cleaned = "".join(ch for ch in name if ch not in bad).strip()
    if not cleaned:
        cleaned = default
    # Trim absurd lengths.
    cleaned = cleaned[:180]
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned


def _version_key(name):
    """Sort key that orders 'gs10.03.1' above 'gs9.55' (numeric, not lexicographic)."""
    nums = re.findall(r"\d+", name)
    return [int(x) for x in nums] if nums else [0]


def find_ghostscript(config):
    """Return a usable Ghostscript console executable path, or None."""
    # 1. Explicit path from config.
    gs = (config.get("ghostscript_path") or "").strip()
    if gs and os.path.isfile(gs):
        return gs
    # 2. Search the usual install locations (highest version first).
    for root in (
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ):
        gs_root = os.path.join(root, "gs")
        if os.path.isdir(gs_root):
            for ver in sorted(os.listdir(gs_root), key=_version_key, reverse=True):
                for exe in ("gswin64c.exe", "gswin32c.exe"):
                    p = os.path.join(gs_root, ver, "bin", exe)
                    if os.path.isfile(p):
                        return p
    # 3. Rely on PATH (no console window, bounded).
    creationflags = 0x08000000 if os.name == "nt" else 0
    for exe in ("gswin64c.exe", "gswin32c.exe", "gs"):
        try:
            subprocess.run(
                [exe, "--version"],
                capture_output=True,
                check=True,
                timeout=15,
                creationflags=creationflags,
            )
            return exe
        except Exception:
            continue
    return None


# Environment variable that overrides the config passphrase. Must be a MACHINE
# (system) variable so the SYSTEM print process can see it, and the spooler must
# be restarted after setting it (fix-queue.bat does that).
PDF_PASSWORD_ENV = "VCP_PDF_PASSWORD"

# DPAPI entropy for per-printer passwords - MUST match setup.ps1's Protect call
# byte-for-byte (ARCHITECTURE.md section 4: UTF-8 of the literal 'VCP-DPAPI-v1').
DPAPI_ENTROPY = b"VCP-DPAPI-v1"


def dpapi_unprotect(b64_blob):
    """Decrypt a machine-scope DPAPI blob (base64, from setup.ps1) to a string.

    LocalMachine scope so this works from the SYSTEM print process regardless of
    which admin encrypted it. RAISES on any failure - the caller fails closed
    (the job is preserved in failed\\, never uploaded unencrypted).
    """
    import base64
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    raw = base64.b64decode(b64_blob)
    # Keep the buffers referenced in locals so they stay alive across the call.
    buf_in = ctypes.create_string_buffer(raw, len(raw))
    buf_ent = ctypes.create_string_buffer(DPAPI_ENTROPY, len(DPAPI_ENTROPY))
    blob_in = DATA_BLOB(len(raw), ctypes.cast(buf_in, ctypes.POINTER(ctypes.c_char)))
    entropy = DATA_BLOB(len(DPAPI_ENTROPY),
                        ctypes.cast(buf_ent, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, ctypes.byref(entropy),
                                    None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise RuntimeError(
            "CryptUnprotectData failed (%d): cannot decrypt the per-printer PDF "
            "password (wrong machine, or the blob is corrupt)." % ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(blob_out.pbData)


def get_pdf_password(config, printer_cfg):
    """Return the PDF encryption passphrase for this job, or None if encryption is off.

    Encryption is ON if the printer entry's `pdf_encryption.enabled` is true, or -
    exactly as before - the global `pdf_encryption.enabled` flag is set or the
    VCP_PDF_PASSWORD machine env var is present (so the env var alone enables it).
    Passphrase resolution order (ARCHITECTURE.md section 3): printer
    `password_dpapi` (DPAPI; a decrypt failure RAISES = fail closed) -> printer
    `password` (plain, discouraged) -> the env var -> global `pdf_encryption`.
    If encryption is enabled but no passphrase resolves we RAISE, so the job is
    preserved in failed\\ rather than uploaded in the clear (fail closed).
    """
    penc = (printer_cfg or {}).get("pdf_encryption") or {}
    genc = config.get("pdf_encryption") or {}
    env_pw = (os.environ.get(PDF_PASSWORD_ENV) or "").strip()
    if not penc.get("enabled") and not genc.get("enabled") and not env_pw:
        return None
    # DPAPI blobs first (printer, then global). A decrypt failure RAISES = fail
    # closed - the job is preserved rather than uploaded unencrypted.
    for blob in (str(penc.get("password_dpapi") or "").strip(),
                 str(genc.get("password_dpapi") or "").strip()):
        if blob:
            pw = dpapi_unprotect(blob)
            if pw:
                return pw
    # Then plaintext fallbacks (discouraged): printer -> env var -> global.
    pw = (str(penc.get("password") or "").strip()
          or env_pw
          or str(genc.get("password") or "").strip())
    if not pw:
        raise RuntimeError(
            "PDF encryption is enabled but no passphrase is set (set the printer's "
            "pdf_encryption.password_dpapi via set-password.bat, or define the %s "
            "machine env var / pdf_encryption.password). Refusing to upload the "
            "document unencrypted." % PDF_PASSWORD_ENV
        )
    return pw


def convert_ps_to_pdf(gs_exe, ps_file, pdf_file):
    """Convert a PostScript file to PDF using Ghostscript. Raises on failure.
    Full quality - no downsampling. (Encryption is a separate qpdf step; Ghostscript
    pdfwrite only supports weak RC4, so we do not use it for encryption.)"""
    cmd = [
        gs_exe,
        "-dBATCH",
        "-dNOPAUSE",
        "-dSAFER",
        "-dQUIET",
        "-sDEVICE=pdfwrite",
        "-sOutputFile=" + pdf_file,
        ps_file,
    ]
    # CREATE_NO_WINDOW so nothing flashes when run from the spooler.
    creationflags = 0x08000000 if os.name == "nt" else 0
    result = subprocess.run(
        cmd, capture_output=True, creationflags=creationflags
    )
    if result.returncode != 0 or not os.path.isfile(pdf_file):
        raise RuntimeError(
            "Ghostscript failed (rc=%s): %s"
            % (result.returncode, result.stderr.decode("utf-8", "replace")[:500])
        )


def _ps_escape(text):
    """Escape a string for a PostScript literal ( ... )."""
    return (text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                .replace("\r", " ").replace("\n", " "))


def stamp_printed_by(gs_exe, pdf_file, user_name):
    """Stamp a "Printed By" e-signature footer onto EVERY page of pdf_file, in
    place, using Ghostscript. Installs an /EndPage handler that draws the text at
    the bottom-left of each page (reason 0 = showpage), so it needs no PDF library
    (stdlib-only rule holds). Raises on failure; the caller keeps it best-effort so
    a print is never lost over a footer."""
    # Professional footer: a thin rule across the page, "Printed by" on the left
    # and the e-signature timestamp on the right, in subtle grey. Page width is
    # read at render time so the right edge aligns on any page size.
    left = _ps_escape("Printed by: " + (user_name or "unknown"))
    right = _ps_escape("Time Stamp: " + time.strftime("%d %b %Y, %H:%M:%S"))
    endpage = (
        "<< /EndPage { exch pop dup 0 eq { gsave "
        "currentpagedevice /PageSize get aload pop /pgH exch def /pgW exch def "
        "0.72 setgray 0.5 setlinewidth 40 31 moveto pgW 40 sub 31 lineto stroke "
        "0.28 setgray /Helvetica findfont 8 scalefont setfont "
        "40 19 moveto (%s) show "
        "(%s) dup stringwidth pop pgW 40 sub exch sub 19 moveto show "
        "grestore } if 2 ne } bind >> setpagedevice" % (left, right)
    )
    out_file = pdf_file + ".stamped"
    cmd = [
        gs_exe, "-dBATCH", "-dNOPAUSE", "-dSAFER", "-dQUIET",
        "-sDEVICE=pdfwrite", "-sOutputFile=" + out_file,
        "-c", endpage, "-f", pdf_file,
    ]
    creationflags = 0x08000000 if os.name == "nt" else 0
    result = subprocess.run(cmd, capture_output=True, creationflags=creationflags)
    if result.returncode != 0 or not os.path.isfile(out_file) or os.path.getsize(out_file) == 0:
        try:
            if os.path.isfile(out_file):
                os.remove(out_file)
        except Exception:
            pass
        raise RuntimeError(
            "Ghostscript stamp failed (rc=%s): %s"
            % (result.returncode, result.stderr.decode("utf-8", "replace")[:300])
        )
    os.replace(out_file, pdf_file)


def find_qpdf(config):
    """Locate the qpdf executable (for AES-256 PDF encryption). Order: config
    'qpdf_path', PATH, then the private copy under %ProgramData%\\...\\qpdf\\."""
    cfg = str((config or {}).get("qpdf_path") or "").strip()
    if cfg and os.path.isfile(cfg):
        return cfg
    creationflags = 0x08000000 if os.name == "nt" else 0
    exe_names = ("qpdf.exe", "qpdf") if os.name == "nt" else ("qpdf",)
    # bundled private copy first (deterministic), then PATH.
    for base in (os.path.join(BASE_DIR, "qpdf"),):
        if os.path.isdir(base):
            for root, _dirs, files in os.walk(base):
                for n in exe_names:
                    if n in files:
                        return os.path.join(root, n)
    for exe in exe_names:
        try:
            subprocess.run([exe, "--version"], capture_output=True, check=True,
                           timeout=15, creationflags=creationflags)
            return exe
        except Exception:
            continue
    return None


def encrypt_pdf(config, pdf_file, password):
    """Encrypt pdf_file IN PLACE with AES-256 using qpdf (lossless - no recompress).
    Raises if qpdf is unavailable or fails, so the caller can fail closed rather
    than upload an unencrypted document."""
    qpdf = find_qpdf(config)
    if not qpdf:
        raise RuntimeError(
            "PDF encryption is enabled but qpdf was not found. Re-run install.bat "
            "(it installs qpdf), or set 'qpdf_path' in config.json."
        )
    creationflags = 0x08000000 if os.name == "nt" else 0
    # qpdf --encrypt <user-pw> <owner-pw> 256 [restrictions] -- --replace-input <file>
    cmd = [qpdf, "--encrypt", password, password, "256", "--", "--replace-input", pdf_file]
    result = subprocess.run(cmd, capture_output=True, creationflags=creationflags)
    # qpdf exit code 3 = warnings (still succeeded); 0 = ok; 2 = error.
    if result.returncode not in (0, 3):
        raise RuntimeError(
            "qpdf encryption failed (rc=%s): %s"
            % (result.returncode, result.stderr.decode("utf-8", "replace")[:500])
        )


def build_multipart(fields, file_field, file_name, file_bytes, content_type):
    """Build a multipart/form-data body. Returns (body_bytes, content_type_header)."""
    boundary = "----VirtualCloudPrinter" + uuid.uuid4().hex
    crlf = b"\r\n"
    buf = io.BytesIO()

    for key, value in fields.items():
        buf.write(b"--" + boundary.encode() + crlf)
        buf.write(
            ('Content-Disposition: form-data; name="%s"' % key).encode("utf-8") + crlf
        )
        buf.write(crlf)
        buf.write(str(value).encode("utf-8") + crlf)

    buf.write(b"--" + boundary.encode() + crlf)
    buf.write(
        (
            'Content-Disposition: form-data; name="%s"; filename="%s"'
            % (file_field, file_name)
        ).encode("utf-8")
        + crlf
    )
    buf.write(("Content-Type: %s" % content_type).encode("utf-8") + crlf)
    buf.write(crlf)
    buf.write(file_bytes)
    buf.write(crlf)
    buf.write(b"--" + boundary.encode() + b"--" + crlf)

    return buf.getvalue(), "multipart/form-data; boundary=%s" % boundary


def post_pdf(config, printer_cfg, doc_name, pdf_bytes, upload_name):
    """POST the PDF to the printer's URL. Raises on non-2xx / network error."""
    url = printer_cfg.get("url", "").strip()
    if not url:
        raise RuntimeError("No URL configured for this printer.")

    docname_field = printer_cfg.get("docname_field", config.get("docname_field", "docname"))
    file_field = printer_cfg.get("file_field", config.get("file_field", "file"))

    fields = {}
    fields.update(config.get("extra_fields", {}) or {})
    fields.update(printer_cfg.get("extra_fields", {}) or {})
    fields[docname_field] = doc_name

    body, content_type = build_multipart(
        fields, file_field, upload_name, pdf_bytes, "application/pdf"
    )

    headers = {}
    headers.update(config.get("headers", {}) or {})
    headers.update(printer_cfg.get("headers", {}) or {})
    headers["Content-Type"] = content_type

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")

    timeout = float(config.get("timeout_seconds", 60))
    verify_tls = bool(printer_cfg.get("verify_tls", config.get("verify_tls", True)))
    ctx = None
    if url.lower().startswith("https"):
        ctx = ssl.create_default_context()
        if not verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

    with urllib.request.urlopen(request, timeout=timeout, context=ctx) as resp:
        status = getattr(resp, "status", resp.getcode())
        preview = resp.read(500).decode("utf-8", "replace")
        if not (200 <= status < 300):
            raise RuntimeError("Server returned HTTP %s: %s" % (status, preview))
        return status, preview


def keep_failed(pdf_file, upload_name):
    r"""Copy the PDF into .\failed\ so a failed upload is never lost."""
    try:
        if not os.path.isdir(FAILED_DIR):
            os.makedirs(FAILED_DIR)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Include a short random token so two failures in the same second with
        # the same document name cannot overwrite each other.
        dest = os.path.join(FAILED_DIR, "%s_%s_%s" % (stamp, uuid.uuid4().hex[:8], upload_name))
        if os.path.isfile(pdf_file):
            with open(pdf_file, "rb") as src, open(dest, "wb") as dst:
                dst.write(src.read())
            log("Saved un-uploaded PDF to %s" % dest)
    except Exception as exc:
        log("Could not save failed PDF: %s" % exc)


def main():
    # ---- parse the arguments handed to us by the port monitor ----
    args = sys.argv[1:]
    ps_file = args[0] if len(args) > 0 else ""
    job_id = args[1] if len(args) > 1 else ""
    printer_name = args[2] if len(args) > 2 else ""
    user_name = args[3] if len(args) > 3 else ""
    # The document name is the tail so titles with spaces survive even if the
    # monitor did not quote them. (user_name = %u is a single token before it.)
    doc_name = " ".join(args[4:]).strip() if len(args) > 4 else ""
    if not doc_name:
        doc_name = os.path.splitext(os.path.basename(ps_file or "document"))[0]

    log("---- job start ---- printer=%r doc=%r jobid=%r user=%r ps=%r"
        % (printer_name, doc_name, job_id, user_name, ps_file))

    pdf_file = None
    try:
        config = load_config()

        if not ps_file or not os.path.isfile(ps_file):
            raise RuntimeError("PostScript spool file not found: %r" % ps_file)

        # Which printer / URL? Match exactly first, then case/space-insensitively,
        # so a small mismatch between the Windows printer name and the config key
        # does not silently mis-route the job.
        printers = config.get("printers", {}) or {}
        printer_cfg = printers.get(printer_name)
        if printer_cfg is None:
            norm = {str(k).strip().casefold(): v for k, v in printers.items()}
            printer_cfg = norm.get((printer_name or "").strip().casefold())
            if printer_cfg is not None:
                log("Printer %r matched a config entry after normalization." % printer_name)
        if printer_cfg is None:
            # Fall back to a default URL if the printer is not explicitly listed.
            default_url = config.get("default_url", "").strip()
            if default_url:
                printer_cfg = {"url": default_url}
                log("WARNING: printer %r not found in config; falling back to default_url %r."
                    % (printer_name, default_url))
            else:
                raise RuntimeError(
                    "Printer %r is not configured and no default_url is set." % printer_name
                )

        # Ask the user for this job's registration number NOW (before the slow
        # PS->PDF step) so the dialog appears promptly after they print. Concurrent
        # jobs from the same user+printer share ONE dialog (batch coalescing). Falls
        # back to any value pre-set via set-id/print-register. Hub printers also
        # get a test back. STRICT dialog: an explicit Cancel discards the job
        # entirely - nothing is converted, uploaded, or preserved.
        reg_id, test_method, test_parameter, cancelled, calibration = get_registration_id(
            config, printer_cfg, printer_name, user_name, job_id, doc_name)
        if cancelled:
            try:
                if ps_file and os.path.isfile(ps_file):
                    os.remove(ps_file)
            except Exception:
                pass
            log("Job CANCELLED by the user at the registration prompt - "
                "discarded (not uploaded, not preserved).")
            log("---- job cancelled ----")
            return

        # Convert PS -> PDF (optionally AES-256 encrypted, same pass = no quality
        # loss). Resolve the passphrase before conversion so an encryption-required
        # job with no passphrase fails closed (preserved, never uploaded in clear).
        gs_exe = find_ghostscript(config)
        if not gs_exe:
            raise RuntimeError("Ghostscript not found. Set 'ghostscript_path' in config.json.")
        pdf_password = get_pdf_password(config, printer_cfg)
        pdf_file = os.path.splitext(ps_file)[0] + ".pdf"
        convert_ps_to_pdf(gs_exe, ps_file, pdf_file)
        log("Converted to PDF: %s (%d bytes)" % (pdf_file, os.path.getsize(pdf_file)))
        # Stamp a "Printed By" e-signature footer on every page (before any
        # encryption so it is part of the document). Best-effort: a stamp failure
        # must not lose the print, so it logs a warning and continues unstamped.
        if config.get("footer_signature", True):
            try:
                stamp_printed_by(gs_exe, pdf_file, user_name)
                log("Stamped 'Printed By' e-sign footer for user %r (%d bytes)."
                    % (user_name, os.path.getsize(pdf_file)))
            except Exception as exc:
                log("WARNING: 'Printed By' footer stamp failed (continuing unstamped): %s" % exc)
        if pdf_password:
            # AES-256 encrypt in place (lossless) before it is read/uploaded.
            encrypt_pdf(config, pdf_file, pdf_password)
            log("Encrypted PDF with AES-256 (qpdf): %s (%d bytes)"
                % (pdf_file, os.path.getsize(pdf_file)))

        with open(pdf_file, "rb") as fh:
            pdf_bytes = fh.read()

        upload_name = sanitize_filename(doc_name)

        # Attach the registration metadata resolved above (prompt or pre-set).
        # Hub printers always send the full field set (ARCHITECTURE.md section 8);
        # an empty registration/test is fine - the hub HOLDS the job (2xx, never
        # lost) until an operator assigns one. Legacy printers keep the old
        # behavior byte-for-byte.
        if is_hub_printer(printer_cfg):
            reg_field = config.get("registration_field", "registration_number")
            printer_cfg = dict(printer_cfg)
            ef = dict(printer_cfg.get("extra_fields") or {})
            ef[reg_field] = reg_id or ""
            ef["test_method"] = test_method or ""
            ef["test_parameter"] = test_parameter or ""
            ef["calibration"] = calibration or ""
            ef["device_name"] = str(printer_cfg.get("device_name") or "")
            ef["department_name"] = str(printer_cfg.get("department_name") or "")
            ef["equipment_name"] = str(printer_cfg.get("equipment_name") or "")
            ef["printed_by"] = user_name
            ef["pdf_from"] = doc_name or ""   # the app's print title / source doc
            ef["job_id"] = job_id
            # Hash of the bytes actually uploaded (post-encryption), so the hub /
            # LIMS can verify integrity end-to-end and detect duplicates.
            ef["sha256"] = hashlib.sha256(pdf_bytes).hexdigest()
            # NB: the per-device PDF password is NOT sent here - it is set once at
            # enrollment and stored (encrypted) in Supabase printer_devices, from
            # where the LIMS reveals it. The PDF is still AES-256 encrypted locally
            # with that same password before upload.
            printer_cfg["extra_fields"] = ef
            if reg_id:
                log("Attached %s=%r method=%r parameter=%r."
                    % (reg_field, reg_id, test_method or "", test_parameter or ""))
            else:
                log("No registration number for this job; uploading anyway - the "
                    "hub will HOLD it (not forward it) until a registration is "
                    "assigned in the hub dashboard.")
        elif reg_id:
            reg_field = config.get("registration_field", "registration_number")
            printer_cfg = dict(printer_cfg)
            ef = dict(printer_cfg.get("extra_fields") or {})
            ef[reg_field] = reg_id
            printer_cfg["extra_fields"] = ef
            log("Attached %s=%r." % (reg_field, reg_id))

        # Upload, with retries. Clamp so retry_count <= 0 still means one attempt.
        retries = max(0, int(config.get("retry_count", 2)))
        delay = float(config.get("retry_delay_seconds", 3))
        success = False
        for attempt in range(1, retries + 2):
            try:
                status, preview = post_pdf(
                    config, printer_cfg, doc_name, pdf_bytes, upload_name
                )
                log("Upload OK (HTTP %s) on attempt %d -> %s"
                    % (status, attempt, printer_cfg.get("url")))
                success = True
                break
            except Exception as exc:
                log("Upload attempt %d failed: %s" % (attempt, exc))
                if attempt <= retries:
                    time.sleep(delay)

        if not success:
            # Preserve the PDF so the job is never lost, then clean the spool copies.
            keep_failed(pdf_file, upload_name)

        # Remove the temporary spool files (the PDF is preserved in .\failed\ on
        # failure; only delete the .ps once we have a PDF so a conversion failure
        # leaves the PostScript behind for debugging).
        for path in (ps_file, pdf_file):
            try:
                if path and os.path.isfile(path):
                    os.remove(path)
            except Exception:
                pass
        log("---- job %s ----" % ("done" if success else "FAILED (preserved in failed/)"))

    except Exception as exc:
        # Never propagate to the spooler. On a hard error (e.g. Ghostscript
        # failed) the .ps is intentionally left in spool\ for debugging.
        log("ERROR: %s" % exc)
        log(traceback.format_exc())
        # If a PDF was already produced, preserve it like an upload failure
        # would - fixqueue clears orphaned spool files, so a converted job that
        # died here (e.g. a transient qpdf/system error) must not be lost.
        # failed\ is SYSTEM+Admins-only, so parking it there is safe even if
        # the crash happened before encryption.
        try:
            if pdf_file and os.path.isfile(pdf_file):
                keep_failed(pdf_file, sanitize_filename(doc_name))
        except Exception:
            pass


if __name__ == "__main__":
    main()
