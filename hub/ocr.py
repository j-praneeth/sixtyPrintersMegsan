"""Google Cloud Vision OCR for the hub — extracts the instrument "Sample Set ID"
and a clean batch "From" header out of each chromatograph PDF.

Runs on the central hub only (FastAPI tier — NOT under the stdlib-only rule that
governs upload.py). Mirrors the web app's googleVision.server.ts: mint an RS256
service-account JWT, exchange it for an access token, call the Vision REST API
(files:annotate, DOCUMENT_TEXT_DETECTION). Requires `pyjwt` + `cryptography`.

parse_batch() is deliberately best-effort and isolated so the per-instrument
extraction rules can be tuned against real OCR samples without touching app.py.
Nothing here raises out to the forward worker on a parse miss — it returns
(None, None) and the caller keeps the client-supplied pdf_from.
"""

import base64
import json
import logging
import re
import time

log = logging.getLogger("hub.ocr")

TOKEN_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
VISION_FILE_URL = "https://vision.googleapis.com/v1/files:annotate"
VISION_IMAGE_URL = "https://vision.googleapis.com/v1/images:annotate"
JWT_TTL_SECONDS = 3600

# 1x1 transparent PNG — the smallest valid input for a connectivity probe.
_TEST_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4"
                 "2mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC")

_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"


# --------------------------------------------------------------------------- #
# Google Vision text extraction
# --------------------------------------------------------------------------- #
def _load_account(creds_json):
    sa = json.loads(creds_json)
    if not sa.get("client_email") or not sa.get("private_key"):
        raise ValueError("Google OCR credentials missing client_email/private_key")
    return sa


def _sign_jwt(sa):
    import jwt  # pyjwt; RS256 needs the `cryptography` backend
    now = int(time.time())
    claim = {
        "iss": sa["client_email"],
        "scope": TOKEN_SCOPE,
        "aud": sa.get("token_uri", "https://oauth2.googleapis.com/token"),
        "iat": now,
        "exp": now + JWT_TTL_SECONDS,
    }
    return jwt.encode(claim, sa["private_key"], algorithm="RS256")


async def _access_token(client, sa):
    r = await client.post(
        sa.get("token_uri", "https://oauth2.googleapis.com/token"),
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": _sign_jwt(sa),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    body = r.json()
    token = body.get("access_token")
    if r.status_code >= 300 or not token:
        raise RuntimeError("Google auth failed: %s"
                           % (body.get("error_description") or r.status_code))
    return token


async def vision_test(client, creds_json):
    """Verify credentials + Vision API access without OCRing a real document.
    A tiny images:annotate probe (1 unit) hits the same project-level billing /
    API-enablement gate as files:annotate, so it surfaces the exact error the
    forward worker would hit. Raises on failure; returns True on success."""
    sa = _load_account(creds_json)
    token = await _access_token(client, sa)
    r = await client.post(
        VISION_IMAGE_URL,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"},
        json={"requests": [{"image": {"content": _TEST_PNG_B64},
                            "features": [{"type": "DOCUMENT_TEXT_DETECTION"}]}]},
    )
    body = r.json()
    if r.status_code >= 300:
        raise RuntimeError((body.get("error") or {}).get("message")
                           or ("HTTP %d" % r.status_code))
    # A per-request error can also arrive inside a 200 envelope.
    err = (body.get("responses") or [{}])[0].get("error")
    if err and err.get("message"):
        raise RuntimeError(err["message"])
    return True


async def vision_extract_text(client, creds_json, pdf_bytes):
    """Return the full document text of a PDF via Google Vision. `client` is the
    hub's shared httpx.AsyncClient. Raises on any failure (caller catches)."""
    sa = _load_account(creds_json)
    token = await _access_token(client, sa)
    content = base64.b64encode(pdf_bytes).decode("ascii")
    r = await client.post(
        VISION_FILE_URL,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"},
        json={"requests": [{
            "inputConfig": {"mimeType": "application/pdf", "content": content},
            "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
        }]},
    )
    body = r.json()
    if r.status_code >= 300:
        raise RuntimeError("Vision API error: %s"
                           % ((body.get("error") or {}).get("message") or r.status_code))
    pages = (body.get("responses") or [{}])[0].get("responses") or []
    texts = [(p.get("fullTextAnnotation") or {}).get("text") or "" for p in pages]
    return "\n\n".join(t for t in texts if t)


# --------------------------------------------------------------------------- #
# Per-instrument parsing (tune against real OCR samples)
# --------------------------------------------------------------------------- #
def _normalize(value):
    """Google Vision often renders the underscores in instrument names as spaces
    ('00922 26 BA CT NNU 00.lcb'). Collapse whitespace back to underscores so the
    displayed value matches the real Shimadzu/Agilent name."""
    return re.sub(r"\s+", "_", value.strip())


def _lcms_sample_set(text):
    # Return the ENTIRE Shimadzu Batch File value (e.g. 00922_26_BA_CT_NNU_00.lcb).
    # Match the value's own shape (ends in .lcb), per line, tolerating spaces where
    # OCR dropped the underscores; the header is COLUMNAR so the value is not on the
    # same line as its "Batch File" label anyway.
    for ln in text.splitlines():
        m = re.search(r"([0-9][0-9A-Za-z _]*?\.lcb)", ln, re.I)
        if m:
            return _normalize(m.group(1))
    # Same-line "Batch File : <value>" fallback (rare — non-columnar reports).
    m = re.search(r"Batch\s*File\s*[:\-]\s*([0-9][0-9A-Za-z _]*?)(?:\r?\n|$)", text, re.I)
    if m and m.group(1).strip():
        return _normalize(m.group(1))
    # Last resort: the "...$BatchAnalysis$00922_26_ML..." data path header.
    m = re.search(r"Batch\s*Analysis\s*\$?\s*([0-9A-Za-z_]+)", text, re.I)
    return m.group(1) if m else None


def _icpms_sample_set(text):
    # Return the ENTIRE Batch Folder segment after the month:
    # '.../2025/Jul/08765_25_MV_01_00(2025-07-21_17-20-30).b'.
    m = re.search(r"/(?:%s)[a-z.]*/\s*([^/\r\n]+)" % _MONTHS, text, re.I)
    if not m:
        return None
    return _normalize(m.group(1))


def _lcms_from(text):
    """The FULL batch-analysis header line, e.g.
    '2026_LCMS$JUL$Batch Analysis$00922_26_ML - 5-4-3 - 14 Jul 2026_Standard Solution_04.lcd'
    — returned in its entirety (no truncation)."""
    for raw in text.splitlines():
        line = raw.strip()
        if "batchanalysis" in line.replace(" ", "").lower():
            return line
    return None


def _icpms_from(text):
    m = re.search(r"Batch\s*Folder\s*[:\-]?\s*([^\r\n]+)", text, re.I)
    return m.group(1).strip() if m else None


def _is_icpms(equipment_name):
    return "icp" in (equipment_name or "").lower()


def parse_batch(text, equipment_name):
    """(sample_set_id, pdf_from) from OCR text. Either may be None. Never raises."""
    if not text:
        return None, None
    try:
        if _is_icpms(equipment_name):
            return _icpms_sample_set(text), _icpms_from(text)
        # LCMS / GCMS (Batch File based). Fall through to ICPMS shape if nothing
        # matched, so an unmapped instrument name still gets a best-effort hit.
        sid = _lcms_sample_set(text)
        frm = _lcms_from(text)
        if sid is None and frm is None:
            return _icpms_sample_set(text), _icpms_from(text)
        return sid, frm
    except Exception as exc:  # parsing must never break the forward
        log.warning("parse_batch failed: %s", exc)
        return None, None
