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
        m = re.search(r"([A-Za-z0-9][0-9A-Za-z _]*?\.lcb)", ln, re.I)
        if m:
            return _normalize(m.group(1))
    # Same-line "Batch File : <value>" fallback (rare — non-columnar reports).
    m = re.search(r"Batch\s*File\s*[:\-]\s*([A-Za-z0-9][0-9A-Za-z _]*?)(?:\r?\n|$)", text, re.I)
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


# --------------------------------------------------------------------------- #
# Extra extractors (DSC / XRD / Particle Size Analyzer / Calibration)
# The hub only EXTRACTS these values; the web app decides the flow & placement.
# --------------------------------------------------------------------------- #
def _reg_no(text):
    """The 'Reg.No' / 'Reg No' value (DSC, Particle Size Analyzer). Same-line first,
    then a bare label with the value on the next line (columnar OCR)."""
    m = re.search(r"Reg\.?\s*No\.?\s*[:\-]\s*([^\r\n]+)", text, re.I)
    if m and m.group(1).strip():
        return _normalize(m.group(1))
    lines = [l.strip() for l in text.splitlines()]
    for i, l in enumerate(lines):
        if re.match(r"Reg\.?\s*No\.?\s*[:\-]?\s*$", l, re.I):
            for j in range(i + 1, min(i + 4, len(lines))):
                if lines[j]:
                    return _normalize(lines[j])
    return None


def _xrd_title(text):
    """The prominent XRD sample title line, e.g.
    '00649_26_ML_Empagliflozin Film Coated Tablets 25 mg _MP-1' — a line beginning
    with <digits>_<digits>_ML_. Returned verbatim (only inner whitespace tidied)."""
    for raw in text.splitlines():
        line = raw.strip()
        if re.match(r"^\d{3,}[_ ]\d{2}[_ ]ML[_ ]", line, re.I):
            return re.sub(r"\s{2,}", " ", line)
    return None


def _calibration(text):
    """(project_name, batch_file) when the print is a Shimadzu BATCH calibration,
    else (None, None). The project header is a year-prefixed token ending in
    'Calibration' (e.g. '*2025_GCMSHS$JUL$Calibration'). OCR frequently reads '$'
    as 'S', so we do NOT require a literal '$'. Being year-anchored keeps it from
    matching the ordinary 'Calibration Method: External Calibration' line that
    normal ICPMS reports carry. project is truncated at 'Calibration'."""
    m = re.search(r"(\*?\s*\d{4}[A-Za-z0-9 _$.\-]*?Calibration)\b", text, re.I)
    if not m:
        return None, None
    project = re.sub(r"\s+", "_", m.group(1).strip())
    bm = re.search(r"([A-Za-z0-9][A-Za-z0-9_\- ]*\.gcb)", text, re.I)
    batch_file = _normalize(bm.group(1)) if bm else None
    return project, batch_file


def _equip_kind(equipment_name):
    """Coarse equipment TYPE from the (possibly instance-suffixed) equipment name.
    Extraction-only: the app does its own routing/normalisation independently."""
    n = (equipment_name or "").lower()
    if "icp" in n:
        return "icpms"
    if "xrd" in n:
        return "xrd"
    if "dsc" in n:
        return "dsc"
    if "psa" in n or "particle" in n:
        return "psa"
    return "chromatograph"  # lcms / gcms / anything else


def extract_document(text, equipment_name):
    """Single entry point used by the forward worker. Returns a dict of extracted
    values; the web app decides the flow/placement from these + the equipment type:
      sample_set_id       - the grouping value for this equipment
      pdf_from            - the batch/header "From" (chromatographs only)
      calibration_project - set only when a calibration batch is detected
    Never raises."""
    out = {"sample_set_id": None, "pdf_from": None, "calibration_project": None}
    if not text:
        return out
    try:
        kind = _equip_kind(equipment_name)
        if kind in ("dsc", "psa"):
            out["sample_set_id"] = _reg_no(text)
        elif kind == "xrd":
            out["sample_set_id"] = _xrd_title(text)
        else:
            sid, frm = parse_batch(text, equipment_name)
            out["sample_set_id"], out["pdf_from"] = sid, frm
        project, _batch = _calibration(text)
        out["calibration_project"] = project
    except Exception as exc:
        log.warning("extract_document failed: %s", exc)
    return out
