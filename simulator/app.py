#!/usr/bin/env python3
"""
Virtual Cloud Printer - receiving-server SIMULATOR (FastAPI)
============================================================

A local stand-in for the real endpoint your virtual printers POST to. You:

  1. Open the dashboard, paste the admin token, and CREATE A LINK.
     A link is an ingest URL whose secret token *is* the API key, e.g.
        http://localhost:8000/ingest/8f3c...   (the token authenticates it)
  2. Paste that URL into a printer in the Virtual Cloud Printer app
     (install.bat / add-printer.bat, or config.json "url").
  3. Print anything -> the PDF + document name arrive here and are stored,
     visible per-link in the dashboard.

This is a DEVELOPMENT SIMULATOR. For real confidential documents it MUST be
run behind HTTPS/TLS (e.g. a reverse proxy) - see simulator/README.md.

Standard library + FastAPI. Storage: SQLite (metadata) + files on disk under
simulator/data/. No external services; nothing leaves this machine.
"""

import os
import json
import shutil
import sqlite3
import secrets
import mimetypes
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

# How many blocking DB/file operations may run in parallel on the threadpool.
# Sized for the ~100-concurrent-user target so a burst of uploads never queues
# on anyio's default limit of 40. Each op is short (a small INSERT or a streamed
# write), so the event loop itself is never blocked.
THREADPOOL_TOKENS = 128
STREAM_CHUNK = 1 << 20  # 1 MiB - stream uploads to disk instead of buffering

# --------------------------------------------------------------------------- #
# Paths & storage
# --------------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
FILES_DIR = os.path.join(DATA_DIR, "files")
DB_PATH = os.path.join(DATA_DIR, "sim.db")
ADMIN_TOKEN_FILE = os.path.join(DATA_DIR, "admin_token.txt")

os.makedirs(FILES_DIR, exist_ok=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def get_admin_token():
    """Admin token from env, else a persisted generated one (printed on startup)."""
    tok = os.environ.get("SIM_ADMIN_TOKEN", "").strip()
    if tok:
        return tok
    if os.path.isfile(ADMIN_TOKEN_FILE):
        with open(ADMIN_TOKEN_FILE) as fh:
            return fh.read().strip()
    tok = secrets.token_urlsafe(24)
    with open(ADMIN_TOKEN_FILE, "w") as fh:
        fh.write(tok)
    return tok


ADMIN_TOKEN = get_admin_token()


def db():
    # A fresh short-lived connection per operation (always opened inside a
    # threadpool worker, so never shared across threads). WAL lets readers run
    # concurrently with a writer; busy_timeout makes concurrent writers wait for
    # the single-writer lock instead of raising "database is locked" under load.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _run(fn):
    """Open a connection, run fn(conn), commit, close - for use via threadpool."""
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
    _run(lambda conn: conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS links (
            token   TEXT PRIMARY KEY,
            label   TEXT,
            created TEXT
        );
        CREATE TABLE IF NOT EXISTS files (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            token        TEXT,
            docname      TEXT,
            stored_name  TEXT,
            size         INTEGER,
            content_type TEXT,
            meta         TEXT,
            received     TEXT
        );
        """
    ))


init_db()


@asynccontextmanager
async def lifespan(_app):
    # Raise the threadpool ceiling so ~100 concurrent uploads don't serialize.
    try:
        import anyio
        anyio.to_thread.current_default_thread_limiter().total_tokens = THREADPOOL_TOKENS
    except Exception:
        pass
    yield


app = FastAPI(title="Virtual Cloud Printer - Receiver Simulator", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Auth helpers
# --------------------------------------------------------------------------- #
def require_admin(x_admin_token):
    if not x_admin_token or not secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing admin token.")


async def link_exists(token):
    return await run_db(
        lambda conn: conn.execute(
            "SELECT 1 FROM links WHERE token = ?", (token,)
        ).fetchone() is not None
    )


def _stream_to_disk(upload_file, dest_path):
    """Copy an upload to disk in chunks (blocking; run via threadpool). Returns
    the byte count. Streaming avoids holding a whole PDF in memory per request."""
    total = 0
    upload_file.seek(0)
    with open(dest_path, "wb") as fh:
        while True:
            chunk = upload_file.read(STREAM_CHUNK)
            if not chunk:
                break
            fh.write(chunk)
            total += len(chunk)
    return total


# --------------------------------------------------------------------------- #
# Ingest endpoint (this is what the printer posts to)
# --------------------------------------------------------------------------- #
@app.post("/ingest/{token}")
async def ingest(token: str, request: Request):
    # The token in the URL IS the API key for this link.
    if not await link_exists(token):
        raise HTTPException(status_code=404, detail="Unknown link token.")

    form = await request.form()
    docname = None
    upload = None
    meta = {}
    for key, value in form.multi_items():
        # Form values are either plain strings or file uploads; anything that
        # isn't a string is the uploaded file (avoids UploadFile-subclass
        # isinstance mismatches between fastapi and starlette).
        if isinstance(value, str):
            if key == "docname":
                docname = value
            else:
                # Any other field (e.g. registration_number) is captured as meta.
                meta[key] = value
        else:
            upload = value
    if upload is None:
        raise HTTPException(status_code=400, detail="No file part in the upload.")

    if not docname:
        docname = upload.filename or "document"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe = "".join(c for c in (upload.filename or "document.pdf") if c.isalnum() or c in "._- ").strip()
    stored_name = "%s_%s_%s" % (token[:8], stamp, secrets.token_hex(3) + "_" + (safe or "doc.pdf"))
    dest_dir = os.path.join(FILES_DIR, token)
    dest = os.path.join(dest_dir, stored_name)

    # Both the directory creation and the (potentially large) file copy are
    # blocking, so run them off the event loop.
    await run_in_threadpool(os.makedirs, dest_dir, exist_ok=True)
    size = await run_in_threadpool(_stream_to_disk, upload.file, dest)

    ctype = upload.content_type or mimetypes.guess_type(upload.filename or "")[0] or "application/octet-stream"
    meta_json = json.dumps(meta)
    received = now_iso()
    await run_db(lambda conn: conn.execute(
        "INSERT INTO files (token, docname, stored_name, size, content_type, meta, received) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (token, docname, stored_name, size, ctype, meta_json, received),
    ))

    return JSONResponse({"ok": True, "docname": docname, "meta": meta, "bytes": size})


# --------------------------------------------------------------------------- #
# Admin API (used by the dashboard)
# --------------------------------------------------------------------------- #
@app.post("/api/links")
async def create_link(request: Request, x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    label = (body.get("label") or "").strip() or "Unnamed link"
    token = secrets.token_urlsafe(18)
    created = now_iso()
    await run_db(lambda conn: conn.execute(
        "INSERT INTO links (token, label, created) VALUES (?, ?, ?)",
        (token, label, created)))
    ingest_url = str(request.base_url).rstrip("/") + "/ingest/" + token
    return {"token": token, "label": label, "ingest_url": ingest_url}


def _list_links(conn):
    rows = conn.execute("SELECT * FROM links ORDER BY created DESC").fetchall()
    out = []
    for row in rows:
        n = conn.execute("SELECT COUNT(*) c FROM files WHERE token = ?",
                         (row["token"],)).fetchone()["c"]
        out.append({
            "token": row["token"], "label": row["label"], "created": row["created"],
            "file_count": n, "ingest_url": "/ingest/" + row["token"],
        })
    return out


@app.get("/api/links")
async def list_links(request: Request, x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    base = str(request.base_url).rstrip("/")
    out = await run_db(_list_links)
    for row in out:
        row["ingest_url"] = base + row["ingest_url"]
    return out


@app.get("/api/links/{token}/files")
async def list_files(token: str, x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    rows = await run_db(lambda conn: conn.execute(
        "SELECT id, docname, stored_name, size, content_type, meta, received "
        "FROM files WHERE token = ? ORDER BY id DESC", (token,)
    ).fetchall())
    return [dict(r) for r in rows]


@app.get("/api/files/{file_id}")
async def download_file(file_id: int, x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    row = await run_db(lambda conn: conn.execute(
        "SELECT token, stored_name, docname, content_type FROM files WHERE id = ?",
        (file_id,)).fetchone())
    if not row:
        raise HTTPException(status_code=404, detail="Not found.")
    path = os.path.join(FILES_DIR, row["token"], row["stored_name"])
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File missing on disk.")
    fname = row["docname"] or row["stored_name"]
    if not fname.lower().endswith(".pdf"):
        fname += ".pdf"
    return FileResponse(path, media_type=row["content_type"] or "application/pdf", filename=fname)


@app.delete("/api/links/{token}")
async def delete_link(token: str, x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    # Remove the link, its file rows, and all stored files on disk.
    await run_db(lambda conn: (
        conn.execute("DELETE FROM files WHERE token = ?", (token,)),
        conn.execute("DELETE FROM links WHERE token = ?", (token,)),
    ))
    await run_in_threadpool(shutil.rmtree, os.path.join(FILES_DIR, token), True)
    return {"ok": True}


@app.delete("/api/files/{file_id}")
async def delete_file(file_id: int, x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    row = await run_db(lambda conn: conn.execute(
        "SELECT token, stored_name FROM files WHERE id = ?", (file_id,)).fetchone())
    if not row:
        raise HTTPException(status_code=404, detail="Not found.")
    await run_db(lambda conn: conn.execute("DELETE FROM files WHERE id = ?", (file_id,)))
    path = os.path.join(FILES_DIR, row["token"], row["stored_name"])
    await run_in_threadpool(lambda: os.path.exists(path) and os.remove(path))
    return {"ok": True}


@app.get("/healthz")
async def healthz():
    return PlainTextResponse("ok")


# --------------------------------------------------------------------------- #
# Dashboard (single self-contained page)
# --------------------------------------------------------------------------- #
DASHBOARD = """<!doctype html>
<html><head><meta charset="utf-8"><title>Virtual Cloud Printer - Receiver Simulator</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--bg:#0f1216;--card:#171c24;--line:#262d38;--fg:#e6edf3;--mut:#9aa7b4;--acc:#4c8dff;--ok:#2ea043}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
 header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center;flex-wrap:wrap}
 h1{font-size:16px;margin:0;font-weight:600} .sub{color:var(--mut);font-size:12px}
 main{padding:24px;max-width:1000px;margin:0 auto} .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
 input,button{font:inherit} input[type=text]{background:#0d1117;border:1px solid var(--line);color:var(--fg);border-radius:7px;padding:8px 10px}
 button{background:var(--acc);color:#fff;border:0;border-radius:7px;padding:8px 14px;cursor:pointer} button.ghost{background:#222b36}
 table{width:100%;border-collapse:collapse;margin-top:8px} th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
 th{color:var(--mut);font-weight:500;font-size:12px} code{background:#0d1117;padding:2px 6px;border-radius:5px;word-break:break-all}
 .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center} .pill{background:#0d1117;border:1px solid var(--line);border-radius:20px;padding:2px 10px;font-size:12px;color:var(--mut)}
 .copy{cursor:pointer;color:var(--acc)} .muted{color:var(--mut)} a{color:var(--acc)} .del{cursor:pointer;color:#ff6b6b}
</style></head><body>
<header>
  <h1>Virtual Cloud Printer</h1><span class="sub">Receiver Simulator</span>
  <div style="flex:1"></div>
  <div class="row"><input id="admin" type="text" placeholder="admin token" size="30"><button class="ghost" onclick="saveTok()">Save</button></div>
</header>
<main>
  <div class="card">
    <div class="row">
      <input id="label" type="text" placeholder="link label (e.g. Invoices)" size="26">
      <button onclick="createLink()">+ Create link</button>
      <span class="muted">The generated URL is what you paste into the printer's config.</span>
    </div>
    <div id="newlink"></div>
  </div>
  <div class="card">
    <div class="row"><b>Links</b><button class="ghost" onclick="refresh()">Refresh</button></div>
    <table id="links"><thead><tr><th>Label</th><th>Ingest URL (paste into printer)</th><th>Files</th><th>Created</th><th></th></tr></thead><tbody></tbody></table>
  </div>
  <div class="card" id="filescard" style="display:none">
    <div class="row"><b>Received documents</b> <span id="fltitle" class="pill"></span></div>
    <table id="files"><thead><tr><th>Document name</th><th>Registration / UUID</th><th>Size</th><th>Type</th><th>Received</th><th></th></tr></thead><tbody></tbody></table>
  </div>
</main>
<script>
 const tok = () => localStorage.getItem('sim_admin') || '';
 function saveTok(){ localStorage.setItem('sim_admin', document.getElementById('admin').value.trim()); refresh(); }
 function hdr(){ return {'X-Admin-Token': tok(), 'Content-Type':'application/json'}; }
 function copy(t){ navigator.clipboard.writeText(t); }
 async function createLink(){
   const label = document.getElementById('label').value;
   const r = await fetch('/api/links',{method:'POST',headers:hdr(),body:JSON.stringify({label})});
   if(!r.ok){ alert('Create failed: '+r.status+' (check admin token)'); return; }
   const j = await r.json();
   document.getElementById('newlink').innerHTML =
     '<p>New link <b>'+j.label+'</b>:<br><code>'+j.ingest_url+'</code> '+
     '<span class="copy" onclick="copy(\\''+j.ingest_url+'\\')">[copy]</span></p>';
   document.getElementById('label').value=''; refresh();
 }
 async function refresh(){
   const r = await fetch('/api/links',{headers:{'X-Admin-Token':tok()}});
   const tb = document.querySelector('#links tbody'); tb.innerHTML='';
   if(!r.ok){ tb.innerHTML='<tr><td colspan=5 class="muted">Enter a valid admin token above.</td></tr>'; return; }
   for(const l of await r.json()){
     const tr=document.createElement('tr');
     tr.innerHTML='<td>'+l.label+'</td><td><code>'+l.ingest_url+'</code> <span class="copy" onclick="copy(\\''+l.ingest_url+'\\')">[copy]</span></td>'+
       '<td><a href="#" onclick="showFiles(\\''+l.token+'\\',\\''+l.label+'\\');return false">'+l.file_count+'</a></td><td class="muted">'+l.created+'</td>'+
       '<td><span class="del" onclick="delLink(\\''+l.token+'\\',\\''+l.label+'\\')">[delete]</span></td>';
     tb.appendChild(tr);
   }
 }
 let curToken=null, curLabel=null;
 async function showFiles(token,label){
   const r = await fetch('/api/links/'+token+'/files',{headers:{'X-Admin-Token':tok()}});
   if(!r.ok){ alert('Load failed'); return; }
   curToken=token; curLabel=label;
   document.getElementById('filescard').style.display='block';
   document.getElementById('fltitle').textContent=label;
   const tb=document.querySelector('#files tbody'); tb.innerHTML='';
   for(const f of await r.json()){
     let reg=''; try{ const m=JSON.parse(f.meta||'{}'); reg=m.registration_number||Object.values(m).join(', ')||''; }catch(e){}
     const tr=document.createElement('tr');
     tr.innerHTML='<td>'+f.docname+'</td><td><code>'+(reg||'&mdash;')+'</code></td><td>'+f.size+'</td><td class="muted">'+f.content_type+'</td><td class="muted">'+f.received+'</td>'+
       '<td><a href="/api/files/'+f.id+'?x_admin_token='+encodeURIComponent(tok())+'" onclick="this.href=\\'/api/files/'+f.id+'\\';dl(event,'+f.id+');return false">download</a> '+
       '<span class="del" onclick="delFile('+f.id+')">[delete]</span></td>';
     tb.appendChild(tr);
   }
 }
 async function delLink(token,label){
   if(!confirm('Delete link "'+label+'" and ALL its received files? This cannot be undone.')) return;
   const r = await fetch('/api/links/'+token,{method:'DELETE',headers:{'X-Admin-Token':tok()}});
   if(!r.ok){ alert('Delete failed: '+r.status+' (check admin token)'); return; }
   if(curToken===token){ document.getElementById('filescard').style.display='none'; curToken=null; }
   refresh();
 }
 async function delFile(id){
   if(!confirm('Delete this document?')) return;
   const r = await fetch('/api/files/'+id,{method:'DELETE',headers:{'X-Admin-Token':tok()}});
   if(!r.ok){ alert('Delete failed: '+r.status); return; }
   if(curToken) showFiles(curToken,curLabel);
   refresh();
 }
 async function dl(e,id){
   e.preventDefault();
   const r = await fetch('/api/files/'+id,{headers:{'X-Admin-Token':tok()}});
   if(!r.ok){ alert('Download failed'); return; }
   const blob = await r.blob(); const u=URL.createObjectURL(blob);
   const a=document.createElement('a'); a.href=u; a.download='document.pdf'; a.click(); URL.revokeObjectURL(u);
 }
 document.getElementById('admin').value = tok(); refresh();
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD)


if __name__ == "__main__":
    import uvicorn
    # Bind to loopback so the printed URL is directly openable in a browser.
    # (0.0.0.0 is a bind-all address, not a connectable one -> ERR_ADDRESS_INVALID.)
    # Override with SIM_HOST=0.0.0.0 to expose on all interfaces.
    host = os.environ.get("SIM_HOST", "127.0.0.1").strip() or "127.0.0.1"
    # 8001 by default so the simulator can never fight the LIMS hub for :8000
    # (both used to default to 8000, which caused confusing bind errors).
    port = int(os.environ.get("SIM_PORT", "8001"))
    print("=" * 70)
    print(" Virtual Cloud Printer - Receiver Simulator  (legacy dev tool)")
    print(" Dashboard : http://localhost:%d/" % port)
    print(" ADMIN TOKEN:", ADMIN_TOKEN)
    print(" (paste that token into the dashboard to create links)")
    print(" NOTE: the LIMS pipeline uses hub\\run.bat, NOT this simulator.")
    print("=" * 70)
    uvicorn.run(app, host=host, port=port)
