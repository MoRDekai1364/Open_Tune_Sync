#!/usr/bin/env python3
"""
OpenTune -> YouTube Music Sync  v2.1
Just run it: python3 opentune_sync.py
"""

# Auto-install Flask if missing
import sys, subprocess, importlib

def _ensure(import_name, pip_name=None):
    try:
        importlib.import_module(import_name)
    except ImportError:
        pkg = pip_name or import_name
        print(f"[setup] Installing {pkg}...", flush=True)
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg,
             "--break-system-packages", "--quiet"],
            capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[setup] ERROR: {r.stderr[:300]}")
            sys.exit(1)
        importlib.invalidate_caches()

_ensure("flask")

import os, shutil, zipfile, sqlite3, json, threading, time, uuid, math, csv, io
import urllib.request, urllib.parse, urllib.error, difflib, re
import webbrowser
from pathlib import Path
from flask import Flask, request, jsonify, redirect, Response

APP_DIR  = Path.home() / ".opentune_sync"
WORK_DIR = APP_DIR / "workdir"
JOBS_DIR = APP_DIR / "jobs"
CFG_FILE = APP_DIR / "config.json"
for _d in (APP_DIR, WORK_DIR, JOBS_DIR): _d.mkdir(parents=True, exist_ok=True)

PORT         = 8080
REDIRECT_URI = f"http://127.0.0.1:{PORT}/auth/callback"
YT_SCOPE     = "https://www.googleapis.com/auth/youtube"
TOKEN_URL    = "https://oauth2.googleapis.com/token"
AUTH_URL     = "https://accounts.google.com/o/oauth2/v2/auth"
YT_API       = "https://www.googleapis.com/youtube/v3"

app = Flask(__name__)
G   = {"db": None, "jobs": {}, "active_cred": 0, "yt_meta_cache": {}, "csv_songs": {}}

# ─── Multi-credential Config / Token ─────────────────────────────────────────
# config.json: credentials=[{name,client_id,client_secret},...], active_cred=int
# tokens stored per-cred: tokens_0.json, tokens_1.json, …
# Legacy tokens.json auto-migrated on first cfg_load().

def cfg_load():
    cfg = json.loads(CFG_FILE.read_text()) if CFG_FILE.exists() else {}
    # migrate legacy single-cred
    if "client_id" in cfg and "credentials" not in cfg:
        cfg["credentials"] = [{"name": "Project 1",
                                "client_id":     cfg.pop("client_id"),
                                "client_secret": cfg.pop("client_secret", "")}]
        cfg_save(cfg)
    # migrate legacy tokens.json
    legacy = APP_DIR / "tokens.json"
    if legacy.exists() and not (APP_DIR / "tokens_0.json").exists():
        legacy.rename(APP_DIR / "tokens_0.json")
    cfg.setdefault("credentials", [])
    cfg.setdefault("active_cred", 0)
    return cfg

def cfg_save(c): CFG_FILE.write_text(json.dumps(c, indent=2))
def _tok_path(idx): return APP_DIR / f"tokens_{idx}.json"

def tok_load(idx=None):
    if idx is None: idx = G.get("active_cred", 0)
    p = _tok_path(idx)
    return json.loads(p.read_text()) if p.exists() else None

def tok_save(t, idx=None):
    if idx is None: idx = G.get("active_cred", 0)
    _tok_path(idx).write_text(json.dumps(t, indent=2))

def tok_clear(idx=None):
    if idx is None: idx = G.get("active_cred", 0)
    p = _tok_path(idx)
    if p.exists(): p.unlink()

def credentials_list():
    cfg = cfg_load()
    creds = cfg.get("credentials", [])
    out = []
    for i, c in enumerate(creds):
        out.append({
            "idx":       i,
            "name":      c.get("name", f"Project {i+1}"),
            "client_id": c.get("client_id",""),
            "connected": bool(tok_load(i)),
            "channel":   cfg.get(f"channel_name_{i}",""),
            "active":    (i == cfg.get("active_cred",0)),
        })
    return out

def next_connected_cred(skip_idx):
    """Return index of next cred with a valid token, or None."""
    cfg = cfg_load()
    n = len(cfg.get("credentials", []))
    for offset in range(1, n):
        idx = (skip_idx + offset) % n
        if tok_load(idx):
            return idx
    return None

# ─── Per-job persistence ──────────────────────────────────────────────────────
# File: JOBS_DIR/<jid>.json
# { "meta":{id,mode,status,...}, "cfg":{original request body},
#   "uploaded_ids":[...],  "failed_songs":[...] }
#
# uploaded_ids is checkpointed after EVERY successful add_video call.
# On resume we skip every video_id already in that set (O(1)).
# On startup we mark any "running" job as "paused" so the UI shows Resume.

_job_lock = threading.Lock()

def _jpath(jid): return JOBS_DIR / f"{jid}.json"

def _job_public(job):
    """Strip internal keys before sending to frontend, add has_cfg flag."""
    pub = {k: v for k, v in job.items() if not k.startswith("_")}
    pub["has_cfg"] = bool(job.get("_cfg"))
    return pub

def persist_job(job):
    data = {
        "meta": {k: job.get(k) for k in (
            "id","mode","status","created_at","started_at","finished_at",
            "total","completed","failed","skipped_existing","error","last_song",
            "current_action","rate","eta_str","cancelled",
        )},
        "cfg":          job.get("_cfg", {}),
        "uploaded_ids": list(job.get("_uploaded", set())),
        "failed_songs": job.get("failed_songs", []),
    }
    with _job_lock:
        _jpath(job["id"]).write_text(json.dumps(data, indent=2))

def load_persisted_jobs():
    for p in sorted(JOBS_DIR.glob("*.json")):
        try:
            data  = json.loads(p.read_text())
            meta  = data.get("meta", {})
            jid   = meta.get("id") or p.stem
            job   = dict(meta)
            job["id"]           = jid
            job["_cfg"]         = data.get("cfg", {})
            job["_uploaded"]    = set(data.get("uploaded_ids", []))
            job["failed_songs"] = data.get("failed_songs", [])
            if job.get("status") == "running":
                job["status"] = "paused"
                persist_job(job)
            for k, v in [("eta_str","--:--"),("rate",0),
                          ("last_song",""),("current_action",""),("skipped_existing",0)]:
                job.setdefault(k, v)
            G["jobs"][jid] = job
        except Exception as exc:
            print(f"[warn] Could not load job {p.name}: {exc}")

def resumable_count():
    return sum(1 for j in G["jobs"].values()
               if j.get("status") in ("paused","quota_exceeded","error")
               and j.get("_cfg"))

# ─── Backup ───────────────────────────────────────────────────────────────────
def process_backup(path_str):
    src = Path(path_str.strip())
    if not src.exists():
        raise FileNotFoundError(f"File not found: {path_str}")
    ts = int(time.time())
    work_copy = WORK_DIR / f"backup_{ts}{src.suffix}"
    shutil.copy2(src, work_copy)
    zip_path = work_copy.with_suffix(".zip")
    shutil.copy2(work_copy, zip_path)
    extract_dir = WORK_DIR / f"extracted_{ts}"
    extract_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)
    dbs = list(extract_dir.glob("*.db"))
    if not dbs:
        raise ValueError("No .db file found in backup archive")
    conn = sqlite3.connect(str(dbs[0]), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    G["db"] = conn
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM song"); sc = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM playlist"); pc = c.fetchone()[0]
    return {"zip_path": str(zip_path), "db_path": str(dbs[0]),
            "song_count": sc, "pl_count": pc}

def _db():
    if G["db"] is None: raise RuntimeError("No backup loaded")
    return G["db"]

def backup_playlists():
    c = _db().cursor()
    c.execute("""
        SELECT p.id, p.name,
               COALESCE(p.createdAt,0) AS createdAt,
               COALESCE(p.lastUpdateTime,0) AS lastUpdateTime,
               COUNT(psm.songId) AS song_count
        FROM playlist p
        LEFT JOIN playlist_song_map psm ON p.id=psm.playlistId
        GROUP BY p.id ORDER BY song_count DESC
    """)
    return [dict(r) for r in c.fetchall()]

def backup_songs(playlist_id=None, sort="title", order="asc",
                 search="", page=1, per_page=50):
    c = _db().cursor()
    where, params = [], []
    if playlist_id:
        where.append("psm.playlistId = ?"); params.append(playlist_id)
    if search:
        where.append("(LOWER(s.title) LIKE ? OR LOWER(COALESCE(a.name,'')) LIKE ?)")
        pat = f"%{search.lower()}%"; params += [pat, pat]
    join_psm = ("JOIN playlist_song_map psm ON s.id=psm.songId" if playlist_id
                else "LEFT JOIN playlist_song_map psm ON s.id=psm.songId")
    wc = ("WHERE " + " AND ".join(where)) if where else ""
    scol = {"title":"s.title","artist":"LOWER(COALESCE(a.name,''))",
            "date":"COALESCE(s.year,0)","date_added":"COALESCE(psm.position,999999)"}.get(sort,"s.title")
    od = "DESC" if order == "desc" else "ASC"
    c.execute(f"""
        SELECT COUNT(DISTINCT s.id) FROM song s {join_psm}
        LEFT JOIN song_artist_map sam ON s.id=sam.songId AND sam.position=0
        LEFT JOIN artist a ON sam.artistId=a.id {wc}
    """, params)
    total = c.fetchone()[0]
    c.execute(f"""
        SELECT DISTINCT s.id, s.title, s.albumName, s.year,
               COALESCE(s.date,0) AS date, s.liked,
               COALESCE(a.name,'') AS artist,
               COALESCE(s.thumbnailUrl,'') AS thumbnailUrl,
               COALESCE(psm.position,0) AS position
        FROM song s {join_psm}
        LEFT JOIN song_artist_map sam ON s.id=sam.songId AND sam.position=0
        LEFT JOIN artist a ON sam.artistId=a.id {wc}
        ORDER BY {scol} {od} LIMIT ? OFFSET ?
    """, params + [per_page, (page-1)*per_page])
    return {"songs": [dict(r) for r in c.fetchall()], "total": total,
            "page": page, "pages": max(1, math.ceil(total/per_page)), "per_page": per_page}

# ─── OAuth2 / YouTube (pure stdlib) ──────────────────────────────────────────
def _http(url, *, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try: detail = json.loads(raw)
        except Exception: detail = {"raw": raw.decode(errors="replace")}
        err = detail.get("error", {})
        status = err.get("code", e.code) if isinstance(err, dict) else e.code
        msg = (err.get("message","") if isinstance(err, dict)
               else detail.get("error_description", str(e)))
        raise RuntimeError(f"HTTP {status}: {msg}") from e

def _http_pages(url, token, key):
    items, pt = [], None
    while True:
        data = _http(url + (f"&pageToken={pt}" if pt else ""),
                     headers={"Authorization": f"Bearer {token}"})
        items.extend(data.get(key, []))
        pt = data.get("nextPageToken")
        if not pt: break
    return items

def _cred(idx=None):
    """Return (client_id, client_secret) for credential index."""
    if idx is None: idx = G.get("active_cred", 0)
    cfg = cfg_load()
    creds = cfg.get("credentials", [])
    if not creds: raise RuntimeError("No credentials saved")
    c = creds[min(idx, len(creds)-1)]
    return c["client_id"], c["client_secret"]

def _valid_token(idx=None):
    if idx is None: idx = G.get("active_cred", 0)
    tok = tok_load(idx)
    if not tok: raise RuntimeError("Not authenticated")
    if time.time() < tok.get("expires_at", 0) - 60:
        return tok["access_token"]
    client_id, client_secret = _cred(idx)
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
        "client_id": client_id, "client_secret": client_secret,
    }).encode()
    data = _http(TOKEN_URL, method="POST", body=body,
                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    tok["access_token"] = data["access_token"]
    tok["expires_at"]   = time.time() + data.get("expires_in", 3600)
    tok_save(tok, idx); return tok["access_token"]

def _exchange_code(code, idx=None):
    if idx is None: idx = G.get("active_cred", 0)
    client_id, client_secret = _cred(idx)
    body = urllib.parse.urlencode({
        "code": code, "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code",
    }).encode()
    data = _http(TOKEN_URL, method="POST", body=body,
                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    tok_save({"access_token": data["access_token"],
              "refresh_token": data.get("refresh_token",""),
              "expires_at": time.time() + data.get("expires_in", 3600)}, idx)

def yt_channel_name():
    token = _valid_token()
    data  = _http(f"{YT_API}/channels?part=snippet&mine=true",
                  headers={"Authorization": f"Bearer {token}"})
    items = data.get("items", [])
    return items[0]["snippet"]["title"] if items else "Connected"

def yt_playlists():
    token = _valid_token()
    raw   = _http_pages(f"{YT_API}/playlists?part=snippet,contentDetails&mine=true&maxResults=50", token, "items")
    return [{"id": i["id"], "name": i["snippet"]["title"],
             "song_count": i["contentDetails"]["itemCount"],
             "thumbnail": (i["snippet"].get("thumbnails",{}).get("default",{}).get("url","")),
             "published_at": i["snippet"].get("publishedAt","")} for i in raw]

def yt_create_playlist(title, privacy="private"):
    token = _valid_token()
    body  = json.dumps({"snippet":{"title":title},"status":{"privacyStatus":privacy}}).encode()
    data  = _http(f"{YT_API}/playlists?part=snippet,status", method="POST", body=body,
                  headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    return data["id"]

def yt_add_video(playlist_id, video_id):
    token = _valid_token()
    body  = json.dumps({"snippet":{"playlistId":playlist_id,
                                   "resourceId":{"kind":"youtube#video","videoId":video_id}}}).encode()
    _http(f"{YT_API}/playlistItems?part=snippet", method="POST", body=body,
          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

def yt_playlist_video_ids(playlist_id):
    token = _valid_token()
    items = _http_pages(
        f"{YT_API}/playlistItems?part=contentDetails&playlistId={playlist_id}&maxResults=50",
        token, "items")
    return {i["contentDetails"]["videoId"] for i in items}

def yt_playlist_songs(playlist_id, page_token="", max_results=50):
    token = _valid_token()
    url = (f"{YT_API}/playlistItems?part=snippet&playlistId={playlist_id}"
           f"&maxResults={max_results}")
    if page_token:
        url += f"&pageToken={urllib.parse.quote(page_token)}"
    data = _http(url, headers={"Authorization": f"Bearer {token}"})
    songs = []
    for item in data.get("items", []):
        snip = item.get("snippet", {})
        vid  = snip.get("resourceId", {}).get("videoId", "")
        thumb = (snip.get("thumbnails", {}).get("default", {}).get("url", "") or
                 snip.get("thumbnails", {}).get("medium", {}).get("url", ""))
        songs.append({
            "id":       vid,
            "title":    snip.get("title", ""),
            "channel":  snip.get("videoOwnerChannelTitle", ""),
            "thumbnail":thumb,
            "position": snip.get("position", 0),
        })
    return {
        "songs":           songs,
        "next_page_token": data.get("nextPageToken", ""),
        "prev_page_token": data.get("prevPageToken", ""),
        "total":           data.get("pageInfo", {}).get("totalResults", 0),
    }

def _norm_text(s):
    return "".join(ch.lower() for ch in s if ch.isalnum() or ch.isspace()).split()

def _match_score(query, title):
    q = " ".join(_norm_text(query))
    t = " ".join(_norm_text(title))
    if not q or not t:
        return 0.0
    ratio = difflib.SequenceMatcher(None, q, t).ratio()
    q_tokens = set(q.split())
    t_tokens = set(t.split())
    overlap = len(q_tokens & t_tokens) / max(1, len(q_tokens))
    return round((ratio * 0.5) + (overlap * 0.5), 3)

def _parse_iso_duration(d):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s

def _video_quality_info(video_ids, token):
    if not video_ids:
        return {}
    out = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i+50]
        url = f"{YT_API}/videos?part=contentDetails&id={','.join(batch)}"
        data = _http(url, headers={"Authorization": f"Bearer {token}"})
        for item in data.get("items", []):
            cd = item.get("contentDetails", {})
            out[item["id"]] = {
                "definition": (cd.get("definition") or "sd").upper(),
                "duration_secs": _parse_iso_duration(cd.get("duration", "")),
            }
    return out

def yt_search_videos(query, max_results=5):
    token = _valid_token()
    url = (f"{YT_API}/search?part=snippet&type=video&maxResults={max_results}"
           f"&q={urllib.parse.quote(query)}")
    data = _http(url, headers={"Authorization": f"Bearer {token}"})
    out = []
    for item in data.get("items", []):
        vid = item.get("id", {}).get("videoId", "")
        if not vid:
            continue
        snip  = item.get("snippet", {})
        thumb = (snip.get("thumbnails", {}).get("default", {}).get("url", "") or
                 snip.get("thumbnails", {}).get("medium", {}).get("url", ""))
        title = snip.get("title", "")
        out.append({"id": vid, "title": title,
                     "channel": snip.get("channelTitle", ""), "thumbnail": thumb,
                     "score": _match_score(query, title)})
    quality = _video_quality_info([r["id"] for r in out], token)
    for r in out:
        q = quality.get(r["id"], {})
        r["definition"]     = q.get("definition", "SD")
        r["duration_secs"]  = q.get("duration_secs", 0)
    out.sort(key=lambda r: r["score"], reverse=True)
    return out

def parse_exportify_csv(text):
    reader = csv.DictReader(io.StringIO(text))
    songs = []
    for i, row in enumerate(reader):
        title  = (row.get("Track Name") or row.get("Track") or "").strip()
        artist = (row.get("Artist Name(s)") or row.get("Artist") or "").strip()
        album  = (row.get("Album Name") or row.get("Album") or "").strip()
        if not title:
            continue
        songs.append({"csv_id": f"csv_{i}", "title": title, "artist": artist, "album": album})
    return songs

# ─── Sync engine ──────────────────────────────────────────────────────────────
def _fmt_eta(secs):
    if secs is None: return "--:--"
    secs = int(secs)
    h, r = divmod(secs, 3600); m, s = divmod(r, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

def _build_tasks(cfg_data, job):
    """
    Reconstruct the full task list from cfg_data.
    For create_new, already-created YT playlist IDs survive in
    cfg_data['created_playlists'] so we never double-create on resume.
    """
    mode, tasks = cfg_data["mode"], []

    if mode == "map_existing":
        for m in cfg_data.get("mappings", []):
            for s in backup_songs(playlist_id=m["backup_id"], per_page=99999)["songs"]:
                tasks.append((s["id"], m["yt_id"], s["title"], s["artist"]))

    elif mode == "create_new":
        created = cfg_data.setdefault("created_playlists", {})
        for pid in cfg_data.get("playlist_ids", []):
            if pid not in created:
                c = _db().cursor()
                c.execute("SELECT name FROM playlist WHERE id=?", (pid,))
                row = c.fetchone()
                pl_name = row["name"] if row else pid
                job["current_action"] = f"Creating: {pl_name}"
                persist_job(job)
                created[pid] = yt_create_playlist(pl_name, cfg_data.get("privacy","private"))
                persist_job(job)   # save created_playlists immediately
                time.sleep(0.3)
            for s in backup_songs(playlist_id=pid, per_page=99999)["songs"]:
                tasks.append((s["id"], created[pid], s["title"], s["artist"]))

    elif mode == "custom_songs":
        yt_id = cfg_data.get("yt_playlist_id", "")
        if not yt_id and cfg_data.get("new_playlist_name"):
            created = cfg_data.setdefault("created_playlists", {})
            if "custom" not in created:
                job["current_action"] = f"Creating: {cfg_data['new_playlist_name']}"
                persist_job(job)
                created["custom"] = yt_create_playlist(
                    cfg_data["new_playlist_name"], cfg_data.get("privacy", "private"))
                persist_job(job)
            yt_id = created["custom"]
        meta_cache = G.get("yt_meta_cache", {})
        c = G["db"].cursor() if G["db"] is not None else None
        for vid in cfg_data.get("song_ids", []):
            title, artist = vid, ""
            if vid in meta_cache:
                title  = meta_cache[vid].get("title") or vid
                artist = meta_cache[vid].get("artist") or ""
            elif c is not None:
                c.execute("""SELECT s.title, COALESCE(a.name,'') AS artist FROM song s
                    LEFT JOIN song_artist_map sam ON s.id=sam.songId AND sam.position=0
                    LEFT JOIN artist a ON sam.artistId=a.id WHERE s.id=?""", (vid,))
                row = c.fetchone()
                if row:
                    title, artist = row["title"], row["artist"]
            tasks.append((vid, yt_id, title, artist))
    return tasks

def run_job(job_id, cfg_data):
    """
    Background sync worker with full checkpoint/resume support.

    HOW RESUME WORKS
    ─────────────────
    _uploaded  : set of video IDs already successfully added to YouTube.
                 Loaded from JOBS_DIR/<jid>.json when the job is resumed.
                 Every task whose video_id is in this set is silently skipped.
    Checkpoint : persist_job() is called after EVERY successful yt_add_video,
                 so a crash/kill loses at most one in-flight song.
    Quota hit  : status -> "quota_exceeded", fully persisted to disk.
                 On next app open the user sees Resume button; clicking it
                 calls run_job again with the same cfg_data and the populated
                 _uploaded set, so work continues from exactly where it stopped.
    Crash/kill : load_persisted_jobs() at startup marks any "running" job as
                 "paused" so the Resume button appears immediately.
    """
    job = G["jobs"][job_id]
    job["status"]     = "running"
    job["started_at"] = job.get("started_at") or time.time()
    job["_cfg"]       = cfg_data
    if "_uploaded"        not in job: job["_uploaded"]        = set()
    if "failed_songs"     not in job: job["failed_songs"]     = []
    if "skipped_existing" not in job: job["skipped_existing"] = 0
    job["pause_requested"] = False
    persist_job(job)

    try:
        tasks = _build_tasks(cfg_data, job)
        already_uploaded = len(job["_uploaded"])

        job["total"]     = len(tasks)
        job["completed"] = already_uploaded   # resume starts with credit
        job["failed"]    = job.get("failed", 0)
        job["current_action"] = ""
        persist_job(job)

        # ── Fetch existing YouTube playlist contents ──────────────────────
        unique_yt_ids = {yt_id for _, yt_id, _, _ in tasks}
        job["current_action"] = f"Checking {len(unique_yt_ids)} YouTube playlist(s) for duplicates…"
        persist_job(job)
        yt_existing: dict = {}
        for yt_id in unique_yt_ids:
            try:
                yt_existing[yt_id] = yt_playlist_video_ids(yt_id)
            except Exception:
                yt_existing[yt_id] = set()

        # ── Pre-filter: remove already-done and already-on-YT songs ──────
        tasks_to_run = []
        newly_skipped = 0
        for t in tasks:
            vid2, yt_id2 = t[0], t[1]
            if vid2 in job["_uploaded"]:
                continue
            if vid2 in yt_existing.get(yt_id2, set()):
                newly_skipped += 1
                continue
            tasks_to_run.append(t)

        job["skipped_existing"] = job.get("skipped_existing", 0) + newly_skipped
        job["total"]     = len(tasks_to_run) + job["completed"]
        job["current_action"] = ""
        persist_job(job)

        t0           = time.time()
        session_done = 0

        for vid, yt_id, title, artist in tasks_to_run:

            # ── Pause ─────────────────────────────────────────────────────
            if job.get("pause_requested"):
                job["status"]         = "paused"
                job["finished_at"]    = time.time()
                job["current_action"] = ""
                persist_job(job)
                return

            # ── Cancel ───────────────────────────────────────────────────
            if job.get("cancelled"):
                job["status"]      = "cancelled"
                job["finished_at"] = time.time()
                persist_job(job)
                return

            # ── Upload ───────────────────────────────────────────────────
            try:
                yt_add_video(yt_id, vid)
                job["_uploaded"].add(vid)
                job["completed"] += 1
                session_done     += 1
                job["last_song"]  = (f"{artist} - {title}".strip(" -") or vid)
                persist_job(job)   # ← checkpoint

            except RuntimeError as e:
                msg = str(e)
                if "403" in msg or "429" in msg or "quota" in msg.lower():
                    # ── Try rotating to the next connected credential ─────
                    cur = G.get("active_cred", 0)
                    nxt = next_connected_cred(cur)
                    if nxt is not None:
                        G["active_cred"] = nxt
                        cfg = cfg_load(); cfg["active_cred"] = nxt; cfg_save(cfg)
                        job["current_action"] = (
                            f"Quota hit on project {cur+1} — switching to project {nxt+1}…")
                        persist_job(job)
                        time.sleep(2)
                        continue   # retry this song with new credential
                    # No more credentials — stop and report
                    remaining = job["total"] - job["completed"] - job["failed"]
                    job["status"] = "quota_exceeded"
                    job["error"]  = (
                        f"All {len(cfg_load().get('credentials',[]))} project(s) hit their "
                        f"daily quota after {job['completed']} uploads. "
                        f"{remaining} songs pending. "
                        f"Quota resets at midnight Pacific Time — click Resume tomorrow, "
                        f"or add more Google Cloud projects in Setup."
                    )
                    job["finished_at"] = time.time()
                    persist_job(job)
                    return
                job["failed"] += 1
                job["failed_songs"].append({"id": vid, "title": title, "error": msg[:120]})
                persist_job(job)

            except Exception as exc:
                job["failed"] += 1
                job["failed_songs"].append({"id": vid, "title": title, "error": str(exc)[:120]})
                persist_job(job)

            # ── Rate / ETA ───────────────────────────────────────────────
            time.sleep(1.1)
            elapsed = time.time() - t0
            if session_done > 0 and elapsed > 1:
                rate      = session_done / elapsed
                remaining = job["total"] - job["completed"] - job["failed"]
                job["eta_str"] = _fmt_eta(remaining / rate if rate > 0 else None)
                job["rate"]    = round(rate * 60, 1)

        job["status"]         = "completed"
        job["finished_at"]    = time.time()
        job["current_action"] = ""
        persist_job(job)

    except Exception as exc:
        job["status"]      = "error"
        job["error"]       = str(exc)
        job["finished_at"] = time.time()
        persist_job(job)

# ─── Auto-detect backups ──────────────────────────────────────────────────────
def find_backup_files():
    candidates, seen = [], set()
    for d in [Path.home()/"Downloads", Path.home()/"Desktop",
              Path.home()/"Documents", Path.home(),
              Path("/sdcard/Download"), Path("/storage/emulated/0/Download")]:
        if not d.exists(): continue
        for f in sorted(d.glob("*.backup"), key=lambda x: x.stat().st_mtime, reverse=True):
            if str(f) not in seen:
                seen.add(str(f)); candidates.append(str(f))
    last = cfg_load().get("last_backup","")
    if last and Path(last).exists() and last not in seen:
        candidates.insert(0, last)
    return candidates

# ─── Flask routes ─────────────────────────────────────────────────────────────
@app.after_request
def _cors(r):
    r.headers["Access-Control-Allow-Origin"]  = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    r.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return r

@app.route("/")
def index(): return Response(HTML, mimetype="text/html")

@app.route("/api/auth/start")
def auth_start():
    idx = int(request.args.get("cred", G.get("active_cred", 0)))
    try: _cred(idx)
    except RuntimeError as e: return jsonify({"error": str(e)}), 400
    G["active_cred"] = idx
    client_id, _ = _cred(idx)
    params = urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT_URI,
        "response_type": "code", "scope": YT_SCOPE,
        "access_type": "offline", "prompt": "consent",
        "state": str(idx)})
    return jsonify({"url": f"{AUTH_URL}?{params}"})

@app.route("/auth/callback")
def auth_callback():
    error = request.args.get("error")
    if error: return redirect(f"/?auth=error&msg={urllib.parse.quote(error)}")
    code  = request.args.get("code")
    idx   = int(request.args.get("state", G.get("active_cred", 0)))
    if not code: return redirect("/?auth=error&msg=no_code")
    try:
        _exchange_code(code, idx)
        try:
            G["active_cred"] = idx
            name = yt_channel_name()
            cfg  = cfg_load()
            cfg[f"channel_name_{idx}"] = name
            cfg["active_cred"] = idx
            cfg_save(cfg)
        except Exception: pass
        return redirect(f"/?auth=success&cred={idx}")
    except Exception as e:
        return redirect(f"/?auth=error&msg={urllib.parse.quote(str(e))}")

@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    idx = int((request.json or {}).get("idx", G.get("active_cred", 0)))
    tok_clear(idx)
    cfg = cfg_load(); cfg.pop(f"channel_name_{idx}", None); cfg_save(cfg)
    return jsonify({"ok": True})

@app.route("/api/status")
def api_status():
    cfg   = cfg_load()
    creds = credentials_list()
    any_auth = any(c["connected"] for c in creds)
    active = next((c for c in creds if c["active"]), creds[0] if creds else {})
    return jsonify({
        "has_credentials": bool(creds),
        "authenticated":   any_auth,
        "channel_name":    active.get("channel",""),
        "backup_loaded":   G["db"] is not None,
        "last_backup":     cfg.get("last_backup",""),
        "resumable":       resumable_count(),
        "suggestions":     find_backup_files(),
        "credentials":     creds,
        "active_cred":     cfg.get("active_cred", 0),
        "csv_songs":       list(G.get("csv_songs", {}).values()),
    })

@app.route("/api/credentials", methods=["GET"])
def list_credentials():
    return jsonify(credentials_list())

@app.route("/api/credentials", methods=["POST"])
def save_credentials():
    d   = request.json or {}
    cfg = cfg_load()
    creds = cfg.setdefault("credentials", [])
    idx = d.get("idx")
    if idx is None:
        # Add new credential
        creds.append({
            "name":          d.get("name", f"Project {len(creds)+1}"),
            "client_id":     d.get("client_id","").strip(),
            "client_secret": d.get("client_secret","").strip(),
        })
        new_idx = len(creds) - 1
    else:
        idx = int(idx)
        while len(creds) <= idx: creds.append({})
        if d.get("client_id"):     creds[idx]["client_id"]     = d["client_id"].strip()
        if d.get("client_secret"): creds[idx]["client_secret"] = d["client_secret"].strip()
        if d.get("name"):          creds[idx]["name"]           = d["name"].strip()
        new_idx = idx
    cfg_save(cfg)
    return jsonify({"ok": True, "idx": new_idx})

@app.route("/api/credentials/<int:idx>", methods=["DELETE"])
def delete_credential(idx):
    cfg   = cfg_load()
    creds = cfg.get("credentials", [])
    if 0 <= idx < len(creds):
        creds.pop(idx)
        tok_clear(idx)
        # Re-index remaining token files
        for i in range(idx, len(creds)):
            old_p = _tok_path(i+1)
            new_p = _tok_path(i)
            if old_p.exists(): old_p.rename(new_p)
        cfg["active_cred"] = max(0, min(cfg.get("active_cred",0), len(creds)-1))
        G["active_cred"] = cfg["active_cred"]
        cfg_save(cfg)
    return jsonify({"ok": True})

@app.route("/api/credentials/<int:idx>/activate", methods=["POST"])
def activate_credential(idx):
    cfg = cfg_load()
    cfg["active_cred"] = idx
    G["active_cred"]   = idx
    cfg_save(cfg)
    return jsonify({"ok": True})

@app.route("/api/backup/load", methods=["POST"])
def load_backup():
    path = (request.json or {}).get("path","").strip()
    if not path: return jsonify({"error": "No path provided"}), 400
    try:
        result = process_backup(path)
        cfg = cfg_load(); cfg["last_backup"] = path; cfg_save(cfg)
        return jsonify({"ok": True, **result})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/backup/playlists")
def get_backup_playlists():
    try: return jsonify(backup_playlists())
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/backup/songs")
def get_backup_songs():
    try:
        return jsonify(backup_songs(
            playlist_id=request.args.get("playlist_id") or None,
            sort=request.args.get("sort","title"),
            order=request.args.get("order","asc"),
            search=request.args.get("search",""),
            page=int(request.args.get("page",1)),
            per_page=min(int(request.args.get("per_page",50)),200)))
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/youtube/playlists")
def get_yt_playlists():
    try: return jsonify(yt_playlists())
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/youtube/playlist/<pid>/songs")
def get_yt_playlist_songs(pid):
    try:
        return jsonify(yt_playlist_songs(
            pid,
            page_token=request.args.get("page_token", ""),
            max_results=min(int(request.args.get("max_results", 50)), 50),
        ))
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    def _stop():
        time.sleep(0.3)
        os._exit(0)
    threading.Thread(target=_stop, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/youtube/search")
def api_youtube_search():
    q = request.args.get("q","").strip()
    if not q: return jsonify({"error": "No query"}), 400
    try:
        return jsonify(yt_search_videos(q, max_results=min(int(request.args.get("max_results",5)),10)))
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/library/upload", methods=["POST"])
def api_library_upload():
    f = request.files.get("file")
    if not f: return jsonify({"error": "No file uploaded"}), 400
    ext = Path(f.filename or "").suffix.lower()
    try:
        if ext == ".csv":
            text = f.read().decode("utf-8-sig", errors="replace")
            songs = parse_exportify_csv(text)
            G["csv_songs"] = {s["csv_id"]: s for s in songs}
            return jsonify({"kind": "csv", "songs": songs, "total": len(songs)})
        ts = int(time.time())
        saved = WORK_DIR / f"upload_{ts}{ext or '.backup'}"
        f.save(str(saved))
        result = process_backup(str(saved))
        cfg = cfg_load(); cfg["last_backup"] = str(saved); cfg_save(cfg)
        return jsonify({"kind": "backup", "ok": True, **result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/csv/import", methods=["POST"])
def api_csv_import():
    content = (request.json or {}).get("content","")
    if not content: return jsonify({"error": "No CSV content"}), 400
    try:
        songs = parse_exportify_csv(content)
        G["csv_songs"] = {s["csv_id"]: s for s in songs}
        return jsonify({"songs": songs, "total": len(songs)})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/csv/match", methods=["POST"])
def api_csv_match():
    d = request.json or {}
    vid = d.get("video_id","")
    if not vid: return jsonify({"error": "No video_id"}), 400
    G.setdefault("yt_meta_cache", {})[vid] = {"title": d.get("title",""), "artist": d.get("artist","")}
    return jsonify({"ok": True})

@app.route("/api/sync/start", methods=["POST"])
def start_sync():
    d = request.json or {}; jid = str(uuid.uuid4())[:8]
    job = {"id": jid, "status": "pending", "mode": d.get("mode",""),
           "created_at": time.time(), "total": 0, "completed": 0, "failed": 0,
           "skipped_existing": 0, "eta_str": "--:--", "rate": 0,
           "last_song": "", "current_action": "", "failed_songs": []}
    job["_cfg"]     = d
    job["_uploaded"]= set()
    G["jobs"][jid]  = job
    persist_job(job)  # create file before thread starts
    threading.Thread(target=run_job, args=(jid, d), daemon=True).start()
    return jsonify({"job_id": jid})

@app.route("/api/sync/preview", methods=["POST"])
def preview_sync():
    """Build task list and return up to 100 new songs without actually syncing."""
    d = request.json or {}
    try:
        dummy = {"id": "__preview__", "current_action": "", "status": "running",
                 "_cfg": d, "_uploaded": set(), "failed_songs": []}
        tasks = _build_tasks(d, dummy)
        unique_yt_ids = {yt_id for _, yt_id, _, _ in tasks}
        yt_existing: dict = {}
        for yt_id in unique_yt_ids:
            try:   yt_existing[yt_id] = yt_playlist_video_ids(yt_id)
            except Exception: yt_existing[yt_id] = set()
        new_songs, skipped = [], 0
        for vid, yt_id, title, artist in tasks:
            if vid in yt_existing.get(yt_id, set()):
                skipped += 1
            else:
                new_songs.append({"id": vid, "title": title, "artist": artist})
        return jsonify({"total_new": len(new_songs), "skipped_existing": skipped,
                        "songs": new_songs[:100]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/sync/<jid>")
def sync_status(jid):
    job = G["jobs"].get(jid)
    return jsonify(job) if job else (jsonify({"error": "Not found"}), 404)

@app.route("/api/sync/<jid>/cancel", methods=["POST"])
def cancel_sync(jid):
    job = G["jobs"].get(jid)
    if job: job["cancelled"] = True; persist_job(job)
    return jsonify({"ok": bool(job)})

@app.route("/api/sync/<jid>/pause", methods=["POST"])
def pause_sync(jid):
    job = G["jobs"].get(jid)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job.get("status") != "running":
        return jsonify({"error": "Job is not running"}), 400
    job["pause_requested"] = True
    persist_job(job)
    return jsonify({"ok": True})

@app.route("/api/sync/<jid>/resume", methods=["POST"])
def resume_sync(jid):
    job = G["jobs"].get(jid)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job.get("status") not in ("paused","quota_exceeded","error","cancelled"):
        return jsonify({"error": f"Job is {job.get('status')}, not resumable"}), 400
    cfg = job.get("_cfg")
    if not cfg:
        return jsonify({"error": "No saved config to resume from"}), 400
    # Reset transient fields; keep _uploaded and failed_songs intact
    job["status"]         = "pending"
    job["cancelled"]      = False
    job["error"]          = None
    job["current_action"] = "Resuming..."
    persist_job(job)
    threading.Thread(target=run_job, args=(jid, cfg), daemon=True).start()
    return jsonify({"ok": True, "job_id": jid})

@app.route("/api/jobs")
def list_jobs():
    return jsonify([_job_public(j) for j in G["jobs"].values()])

# ─── Self-test ────────────────────────────────────────────────────────────────
def self_test():
    load_persisted_jobs()
    ok = True
    def chk(label, fn):
        nonlocal ok
        try: fn(); print(f"  [OK] {label}")
        except Exception as e: print(f"  [!!] {label}  ->  {e}"); ok = False

    print("Startup checks:")
    chk("Python >= 3.8",
        lambda: (_ for _ in ()).throw(RuntimeError("Need 3.8+"))
        if sys.version_info < (3,8) else None)
    chk("sqlite3",     lambda: sqlite3.connect(":memory:").close())
    chk("zipfile",     lambda: zipfile.ZipFile.__doc__)
    chk("flask",       lambda: __import__("flask"))
    chk("urllib",      lambda: urllib.parse.urlencode({"a":1}))
    chk("config dir",  lambda: ((APP_DIR/".wt").write_text("ok"), (APP_DIR/".wt").unlink()))

    found = find_backup_files()
    if found:
        print(f"  [OK] auto-detected {len(found)} backup file(s):")
        for f in found[:3]: print(f"       {f}")
    else:
        print("  [--] no backup files auto-detected (enter path manually)")

    if not ok:
        print("\nFix the errors above and retry.\n"); sys.exit(1)
    print()

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpenTune Sync</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg:       #07070f;
  --bg1:      #0d0d1a;
  --bg2:      #12121f;
  --bg3:      #181828;
  --border:   #1e1e32;
  --border2:  #2a2a45;
  --text:     #dde1f0;
  --muted:    #5a5a80;
  --dim:      #3a3a58;
  --green:    #00e87a;
  --green2:   #00b85e;
  --amber:    #ffaa33;
  --red:      #ff4466;
  --purple:   #9b6dff;
  --blue:     #4488ff;
  --radius:   6px;
  --mono:     'IBM Plex Mono', monospace;
  --sans:     'Syne', sans-serif;
}
* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--sans);
  font-size: 14px;
  min-height: 100vh;
  overflow-x: hidden;
}

/* Header */
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  height: 52px;
  background: var(--bg1);
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  z-index: 100;
}
.logo {
  font-family: var(--mono);
  font-size: 13px;
  font-weight: 600;
  color: var(--green);
  letter-spacing: 0.05em;
  display: flex;
  align-items: center;
  gap: 8px;
}
.logo svg { width: 20px; height: 20px; }
nav { display: flex; gap: 2px; }
nav button {
  background: none;
  border: none;
  color: var(--muted);
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  padding: 6px 14px;
  border-radius: var(--radius);
  cursor: pointer;
  transition: all .15s;
}
nav button:hover { color: var(--text); background: var(--bg2); }
nav button.active { color: var(--green); background: rgba(0,232,122,.08); }
.header-right {
  display: flex;
  align-items: center;
  gap: 10px;
  font-family: var(--mono);
  font-size: 11px;
}
.badge {
  padding: 3px 10px;
  border-radius: 100px;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: .05em;
  text-transform: uppercase;
}
.badge-green { background: rgba(0,232,122,.15); color: var(--green); border: 1px solid rgba(0,232,122,.2); }
.badge-amber { background: rgba(255,170,51,.12); color: var(--amber); border: 1px solid rgba(255,170,51,.2); }
.badge-red   { background: rgba(255,68,102,.12);  color: var(--red);   border: 1px solid rgba(255,68,102,.2); }
.badge-dim   { background: var(--bg2); color: var(--muted); border: 1px solid var(--border); }

/* Layout */
main { padding: 24px; max-width: 1400px; margin: 0 auto; }
.view { display: none; }
.view.active { display: block; }

/* Cards */
.card {
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
}
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
  background: var(--bg2);
}
.card-title {
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .1em;
  color: var(--muted);
}
.card-body { padding: 18px; }

/* Buttons */
.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  border-radius: var(--radius);
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  border: none;
  transition: all .15s;
  white-space: nowrap;
}
.btn-primary {
  background: var(--green);
  color: #000;
}
.btn-primary:hover { background: var(--green2); }
.btn-secondary {
  background: var(--bg3);
  color: var(--text);
  border: 1px solid var(--border2);
}
.btn-secondary:hover { border-color: var(--dim); background: var(--bg2); }
.btn-danger { background: rgba(255,68,102,.15); color: var(--red); border: 1px solid rgba(255,68,102,.2); }
.btn-danger:hover { background: rgba(255,68,102,.25); }
.btn-sm { padding: 5px 10px; font-size: 11px; }
.btn:disabled { opacity: .4; cursor: not-allowed; }

/* Inputs */
.input {
  background: var(--bg2);
  border: 1px solid var(--border2);
  border-radius: var(--radius);
  color: var(--text);
  font-family: var(--mono);
  font-size: 12px;
  padding: 8px 12px;
  outline: none;
  transition: border .15s;
  width: 100%;
}
.input:focus { border-color: var(--green); }
.input::placeholder { color: var(--dim); }
input[type=file].input { padding: 6px; color: var(--muted); font-size: 11px; }
input[type=file].input::file-selector-button,
input[type=file].input::-webkit-file-upload-button {
  background: var(--bg3);
  color: var(--text);
  border: 1px solid var(--border2);
  border-radius: var(--radius);
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 500;
  padding: 6px 12px;
  margin-right: 10px;
  cursor: pointer;
  transition: all .15s;
}
input[type=file].input::file-selector-button:hover,
input[type=file].input::-webkit-file-upload-button:hover {
  border-color: var(--dim);
  background: var(--bg2);
}
.label {
  display: block;
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .08em;
  color: var(--muted);
  margin-bottom: 6px;
}
.field { margin-bottom: 16px; }

/* Grid */
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
.grid3 { display: grid; grid-template-columns: repeat(3,1fr); gap: 16px; }
.flex  { display: flex; gap: 10px; align-items: center; }
.flex-between { display: flex; justify-content: space-between; align-items: center; }
.mt8  { margin-top: 8px; }
.mt16 { margin-top: 16px; }
.mt24 { margin-top: 24px; }

/* Stat blocks */
.stat-grid { display: grid; grid-template-columns: repeat(3,1fr); gap: 12px; }
.stat {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px 16px;
}
.stat-val {
  font-family: var(--mono);
  font-size: 24px;
  font-weight: 600;
  color: var(--green);
  line-height: 1;
}
.stat-label {
  font-family: var(--mono);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: .08em;
  color: var(--muted);
  margin-top: 4px;
}

/* Tables */
.tbl-wrap {
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: 8px;
}
table { width: 100%; border-collapse: collapse; }
th {
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .08em;
  color: var(--muted);
  padding: 10px 14px;
  text-align: left;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
  cursor: pointer;
  user-select: none;
}
th:hover { color: var(--text); }
th.sorted { color: var(--green); }
td {
  padding: 9px 14px;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
  vertical-align: middle;
}
tr:last-child td { border-bottom: none; }
tr:hover td { background: rgba(255,255,255,.02); }
.tr-selected td { background: rgba(0,232,122,.05) !important; }
.mono { font-family: var(--mono); font-size: 12px; }
.text-muted { color: var(--muted); }
.text-green  { color: var(--green); }
.text-amber  { color: var(--amber); }
.text-red    { color: var(--red); }
.text-purple { color: var(--purple); }
.thumb {
  width: 32px;
  height: 32px;
  border-radius: 4px;
  object-fit: cover;
  background: var(--bg3);
}
.thumb-placeholder {
  width: 32px; height: 32px;
  border-radius: 4px;
  background: var(--bg3);
  border: 1px solid var(--border2);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--dim);
  font-size: 14px;
  flex-shrink: 0;
}

/* Controls bar */
.controls {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.search-wrap { position: relative; flex: 1; min-width: 200px; }
.search-wrap svg { position: absolute; left: 10px; top: 50%; transform: translateY(-50%); color: var(--dim); }
.search-input { padding-left: 34px !important; }
.select {
  background: var(--bg2);
  border: 1px solid var(--border2);
  border-radius: var(--radius);
  color: var(--text);
  font-family: var(--mono);
  font-size: 12px;
  padding: 8px 12px;
  outline: none;
  cursor: pointer;
}
.sort-btn {
  background: var(--bg2);
  border: 1px solid var(--border2);
  border-radius: var(--radius);
  color: var(--muted);
  font-family: var(--mono);
  font-size: 11px;
  padding: 7px 12px;
  cursor: pointer;
}
.sort-btn.active { color: var(--green); border-color: rgba(0,232,122,.3); background: rgba(0,232,122,.06); }

/* Pagination */
.pagination {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 14px;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--muted);
}
.page-btn {
  padding: 4px 10px;
  border-radius: var(--radius);
  border: 1px solid var(--border2);
  background: var(--bg2);
  color: var(--text);
  font-family: var(--mono);
  font-size: 11px;
  cursor: pointer;
}
.page-btn:disabled { opacity: .35; cursor: default; }
.page-btn.current { background: rgba(0,232,122,.1); border-color: rgba(0,232,122,.3); color: var(--green); }

/* Progress */
.progress-bar-wrap {
  height: 6px;
  background: var(--bg3);
  border-radius: 100px;
  overflow: hidden;
  margin: 10px 0;
}
.progress-bar {
  height: 100%;
  border-radius: 100px;
  background: var(--green);
  transition: width .5s ease;
}
.progress-bar.error { background: var(--red); }
.progress-bar.amber { background: var(--amber); }

/* Job cards */
.job-card {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px 20px;
  margin-bottom: 12px;
}
.job-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px; }
.job-title { font-weight: 600; font-size: 14px; }
.job-meta { font-family: var(--mono); font-size: 11px; color: var(--muted); margin-top: 4px; }
.job-stats { display: flex; gap: 20px; font-family: var(--mono); font-size: 12px; margin-top: 10px; }

/* Sync mode tabs */
.mode-tabs { display: flex; gap: 2px; margin-bottom: 20px; }
.mode-tab {
  flex: 1;
  padding: 10px;
  text-align: center;
  border: 1px solid var(--border2);
  border-radius: var(--radius);
  cursor: pointer;
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .06em;
  color: var(--muted);
  background: var(--bg2);
  transition: all .15s;
}
.mode-tab:hover { color: var(--text); }
.mode-tab.active {
  background: rgba(0,232,122,.08);
  border-color: rgba(0,232,122,.3);
  color: var(--green);
}

/* Mapping rows */
.map-row {
  display: grid;
  grid-template-columns: 1fr 40px 1fr;
  gap: 10px;
  align-items: center;
  margin-bottom: 10px;
}
.map-arrow { text-align: center; color: var(--green); font-size: 18px; }

/* Playlist row */
.pl-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  transition: background .1s;
}
.pl-row:last-child { border-bottom: none; }
.pl-row:hover { background: rgba(255,255,255,.02); }
.pl-row.selected { background: rgba(0,232,122,.06); }
.pl-row .pl-name { flex: 1; font-size: 13px; }
.pl-count { font-family: var(--mono); font-size: 12px; color: var(--muted); }

/* Toast */
#toast {
  position: fixed;
  bottom: 24px;
  right: 24px;
  padding: 12px 20px;
  border-radius: 8px;
  font-family: var(--mono);
  font-size: 12px;
  z-index: 1000;
  opacity: 0;
  transform: translateY(8px);
  transition: all .2s;
  pointer-events: none;
}
#toast.show { opacity: 1; transform: translateY(0); }
#toast.success { background: rgba(0,232,122,.15); border: 1px solid rgba(0,232,122,.3); color: var(--green); }
#toast.error   { background: rgba(255,68,102,.15);  border: 1px solid rgba(255,68,102,.3);  color: var(--red); }
#toast.info    { background: rgba(68,136,255,.15);  border: 1px solid rgba(68,136,255,.3);  color: var(--blue); }

/* Setup view special */
.setup-hero {
  text-align: center;
  padding: 40px 20px;
  border-bottom: 1px solid var(--border);
}
.setup-hero h1 {
  font-family: var(--sans);
  font-size: 32px;
  font-weight: 800;
  background: linear-gradient(135deg, var(--green), #00aaff);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  margin-bottom: 8px;
}
.setup-hero p { color: var(--muted); font-size: 14px; }

/* Selection footer */
#sel-bar {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  background: var(--bg1);
  border-top: 1px solid var(--border2);
  padding: 12px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  z-index: 50;
  transform: translateY(100%);
  transition: transform .2s;
}
#sel-bar.show { transform: translateY(0); }
#preview-bar {
  position: fixed;
  left: 50%;
  bottom: 0;
  transform: translate(-50%, 100%);
  width: min(560px, 92vw);
  background: var(--bg3);
  border: 1px solid var(--border2);
  border-bottom: none;
  border-radius: 10px 10px 0 0;
  padding: 10px 14px;
  display: flex;
  align-items: center;
  gap: 10px;
  z-index: 50;
  transition: transform .2s;
}
#preview-bar.show { transform: translate(-50%, 0); }
#sel-count { font-family: var(--mono); font-size: 12px; color: var(--green); }

/* Resume banner */
#resume-banner {
  display: none;
  background: rgba(255,170,51,.1);
  border: 1px solid rgba(255,170,51,.35);
  border-radius: 8px;
  padding: 14px 20px;
  margin-bottom: 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
#resume-banner.hidden { display: none !important; }
.resume-banner-text { font-size: 13px; }
.resume-banner-text strong { color: var(--amber); }
.resume-banner-btns { display: flex; gap: 8px; flex-shrink: 0; }

/* Scrollbar */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--dim); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--muted); }

/* Checkbox */
input[type=checkbox] { accent-color: var(--green); cursor: pointer; width: 14px; height: 14px; }

/* Liked heart */
.heart { color: var(--red); font-size: 12px; }

/* Credential Wizard */
.wizard-steps {
  display: flex;
  gap: 0;
  margin-bottom: 20px;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid var(--border);
}
.wiz-step {
  flex: 1;
  padding: 10px 6px;
  text-align: center;
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .04em;
  color: var(--muted);
  background: var(--bg2);
  border-right: 1px solid var(--border);
  transition: all .15s;
  cursor: default;
  user-select: none;
}
.wiz-step:last-child, .cwiz-step:last-child { border-right: none; }
.wiz-step.done, .cwiz-step.done     { color: var(--green); background: rgba(0,232,122,.07); }
.wiz-step.active, .cwiz-step.active { color: var(--text);  background: var(--bg3); }
.wiz-panel, .cwiz-panel { display: none; }
.wiz-panel.active, .cwiz-panel.active { display: block; }
.cwiz-step {
  flex: 1;
  padding: 10px 6px;
  text-align: center;
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .04em;
  color: var(--muted);
  background: var(--bg2);
  border-right: 1px solid var(--border);
  transition: all .15s;
  cursor: default;
  user-select: none;
}
.wiz-num {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px; height: 28px;
  border-radius: 50%;
  background: var(--bg3);
  border: 1px solid var(--border2);
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
  margin-right: 10px;
  flex-shrink: 0;
}
.wiz-num.active { background: rgba(0,232,122,.15); border-color: rgba(0,232,122,.4); color: var(--green); }
.wiz-action-row {
  background: var(--bg3);
  border: 1px solid var(--border2);
  border-radius: 8px;
  padding: 14px 16px;
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 12px;
  transition: border-color .15s;
}
.wiz-action-row:hover { border-color: var(--dim); }
.wiz-action-icon { font-size: 22px; flex-shrink: 0; }
.wiz-action-text { flex: 1; }
.wiz-action-title { font-size: 13px; font-weight: 600; }
.wiz-action-sub { font-size: 11px; color: var(--muted); margin-top: 2px; line-height: 1.5; }
.wiz-nav { display: flex; gap: 8px; margin-top: 16px; align-items: center; }
.json-textarea {
  background: var(--bg3);
  border: 1px solid var(--border2);
  border-radius: var(--radius);
  color: var(--text);
  font-family: var(--mono);
  font-size: 11px;
  padding: 10px 12px;
  width: 100%;
  resize: vertical;
  outline: none;
  min-height: 80px;
}
.json-textarea:focus { border-color: var(--green); }

/* YT song browse panel */
.browse-panel {
  display: none;
  border-top: 1px solid var(--border);
}
.browse-panel.open { display: block; }
.browse-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: var(--bg3);
}
.browse-back {
  background: none;
  border: none;
  color: var(--muted);
  font-family: var(--mono);
  font-size: 11px;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: var(--radius);
  display: flex;
  align-items: center;
  gap: 6px;
}
.browse-back:hover { color: var(--text); background: var(--bg2); }
.browse-title { font-size: 13px; font-weight: 600; }

/* Sync preview */
.sync-preview {
  margin-top: 16px;
  background: var(--bg1);
  border: 1px solid var(--border2);
  border-radius: 8px;
  overflow: hidden;
}
.sync-preview-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  font-family: var(--mono);
  font-size: 11px;
}
.sync-preview-body {
  max-height: 320px;
  overflow-y: auto;
}
</style>
</head>
<body>

<header>
  <div class="logo">
    <svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="10" cy="10" r="9" stroke="currentColor" stroke-width="1.5"/>
      <circle cx="10" cy="10" r="3" fill="currentColor"/>
      <path d="M10 1 A9 9 0 0 1 19 10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    </svg>
    OpenTune Sync
  </div>
  <nav id="nav">
    <button class="active" onclick="showView('setup')">Setup</button>
    <button onclick="showView('playlists')">Playlists</button>
    <button onclick="showView('songs')">Songs</button>
    <button onclick="showView('sync')">Sync</button>
    <button onclick="showView('progress')" id="nav-progress">Progress</button>
  </nav>
  <div class="header-right">
    <span id="hdr-backup" class="badge badge-dim">No backup</span>
    <span id="hdr-auth"   class="badge badge-dim">Not connected</span>
    <button class="btn btn-danger btn-sm" onclick="exitApp()">⏻ Exit</button>
  </div>
</header>

<main>

<!-- ═══ SETUP VIEW ═══════════════════════════════════════════════════════════ -->
<div id="view-setup" class="view active">
  <div class="setup-hero">
    <h1>OpenTune → YouTube Music</h1>
    <p>Import your OpenTune backup and sync playlists directly to YouTube Music</p>
  </div>

  <div class="grid2 mt24">

    <!-- Import panel -->
    <div class="card">
      <div class="card-header">
        <span class="card-title">① Import Your Library</span>
        <span id="backup-status" class="badge badge-dim">Not loaded</span>
      </div>
      <div class="card-body">

        <div id="import-path-choice">
          <p style="font-size:13px;color:var(--muted);margin-bottom:14px;line-height:1.6">
            How do you want to bring in your songs?
          </p>
          <div class="wiz-action-row" style="cursor:pointer" onclick="selectImportPath('backup')">
            <div class="wiz-action-icon">💾</div>
            <div class="wiz-action-text">
              <div class="wiz-action-title">OpenTune Backup</div>
              <div class="wiz-action-sub">Import a .backup file straight from the OpenTune app.</div>
            </div>
            <button class="btn btn-secondary btn-sm">Choose →</button>
          </div>
          <div class="wiz-action-row" style="cursor:pointer" onclick="selectImportPath('csv')">
            <div class="wiz-action-icon">📄</div>
            <div class="wiz-action-text">
              <div class="wiz-action-title">Playlist File (CSV)</div>
              <div class="wiz-action-sub">Import a CSV playlist export — from Spotify via Exportify, or any similar export.</div>
            </div>
            <button class="btn btn-secondary btn-sm">Choose →</button>
          </div>
        </div>

        <div id="import-path-backup" style="display:none">
          <button class="btn btn-secondary btn-sm" onclick="showImportChoice()">← Change method</button>
          <div class="field mt16">
            <label class="label">Upload a .backup file</label>
            <input type="file" id="library-file-input" accept=".backup,.zip" class="input" onchange="handleLibraryFile(event)">
          </div>
          <div style="font-size:11px;color:var(--dim);margin:8px 0;text-align:center">— or —</div>
          <div class="field">
            <label class="label">Path to .backup file</label>
            <input class="input" id="backup-path" type="text"
              placeholder="/path/to/OpenTune_backup.backup" value="">
          </div>
          <button class="btn btn-primary" onclick="loadBackup()">Process File</button>
          <div id="backup-stats" class="mt16" style="display:none">
            <div class="stat-grid">
              <div class="stat"><div class="stat-val" id="stat-songs">0</div><div class="stat-label">Songs</div></div>
              <div class="stat"><div class="stat-val" id="stat-playlists">0</div><div class="stat-label">Playlists</div></div>
              <div class="stat"><div class="stat-val text-green">✓</div><div class="stat-label">DB Loaded</div></div>
            </div>
          </div>
          <div class="mt16" style="background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:12px">
            <div class="label">What this does</div>
            <div style="color:var(--muted);font-size:12px;line-height:1.7">
              Copies your .backup file → renames to .zip → extracts the SQLite database → reads playlists &amp; songs.
              Your original file is never modified.
            </div>
          </div>
        </div>

        <div id="import-path-csv" style="display:none">
          <button class="btn btn-secondary btn-sm" onclick="showImportChoice()">← Change method</button>

          <div class="wiz-steps mt16">
            <div class="cwiz-step active" id="cws-0">1 · Export playlist</div>
            <div class="cwiz-step"        id="cws-1">2 · Upload CSV</div>
            <div class="cwiz-step"        id="cws-2">3 · Match &amp; Sync</div>
          </div>

          <div class="cwiz-panel active" id="cwp-0">
            <p style="font-size:13px;margin-bottom:14px;line-height:1.6">
              Export a playlist to a CSV file. Works with any CSV playlist export — for Spotify, Exportify is the easiest option.
            </p>
            <div class="wiz-action-row">
              <div class="wiz-action-icon">🎧</div>
              <div class="wiz-action-text">
                <div class="wiz-action-title">Open Exportify</div>
                <div class="wiz-action-sub">Log in with Spotify, pick a playlist, and download the CSV.</div>
              </div>
              <button class="btn btn-primary btn-sm" onclick="showExportifyEmbed()">Open →</button>
            </div>
            <div id="exportify-embed-wrap" style="display:none;margin-top:12px">
              <div style="border:1px solid var(--border);border-radius:6px;overflow:hidden;background:var(--bg2)">
                <iframe src="https://exportify.net/" title="Exportify" style="width:100%;height:420px;border:0;display:block"></iframe>
              </div>
              <a href="https://exportify.net/" target="_blank" rel="noopener" class="btn btn-secondary btn-sm" style="margin-top:8px">Not loading? Open in new tab ↗</a>
            </div>
            <div class="wiz-nav">
              <button class="btn btn-primary" onclick="cwizGo(1)">I have my CSV →</button>
            </div>
          </div>

          <div class="cwiz-panel" id="cwp-1">
            <div class="field">
              <label class="label">Upload CSV</label>
              <input type="file" id="library-csv-input" accept=".csv" class="input" onchange="handleWizardCsvUpload(event)">
            </div>
            <div class="wiz-nav">
              <button class="btn btn-secondary btn-sm" onclick="cwizGo(0)">← Back</button>
            </div>
          </div>

          <div class="cwiz-panel" id="cwp-2">
            <div class="wiz-action-row">
              <div class="wiz-action-icon">✅</div>
              <div class="wiz-action-text">
                <div class="wiz-action-title" id="cwiz-count">Songs loaded</div>
                <div class="wiz-action-sub">Head to the Songs tab to find YouTube matches and add them to a playlist.</div>
              </div>
            </div>
            <label style="font-size:12px;color:var(--muted);display:flex;align-items:center;gap:6px;margin-bottom:12px">
              <input type="checkbox" id="csv-fast-toggle-setup" checked onchange="syncFastMatchToggle(this.checked)">
              Fast match by default (auto-pick high-confidence results)
            </label>
            <div class="wiz-nav">
              <button class="btn btn-secondary btn-sm" onclick="cwizGo(0)">← Start over</button>
              <button class="btn btn-primary" onclick="showView('songs')">Go match songs →</button>
            </div>
          </div>
        </div>

      </div>
    </div>

    <!-- Auth / Credentials panel -->
    <div class="card">
      <div class="card-header">
        <span class="card-title">② YouTube Projects</span>
        <span id="auth-status-badge" class="badge badge-dim">Not connected</span>
      </div>
      <div class="card-body" style="padding:0">

        <!-- Project list (shown when credentials exist) -->
        <div id="projects-panel" style="display:none">
          <div style="padding:14px 18px 10px">
            <p style="font-size:12px;color:var(--muted);margin-bottom:12px;line-height:1.6">
              Each Google Cloud project has its own <b style="color:var(--amber)">~200 songs/day</b> quota.
              Add more projects to multiply your daily capacity — they rotate automatically when one hits its limit.
            </p>
            <div id="projects-list"></div>
            <button class="btn btn-secondary btn-sm mt8" onclick="showAddProject()" style="width:100%">+ Add Another Project</button>
          </div>

          <!-- Quota math display -->
          <div id="quota-math" style="margin:0 18px 14px;background:rgba(0,232,122,.06);border:1px solid rgba(0,232,122,.15);border-radius:6px;padding:10px 14px;font-family:var(--mono);font-size:11px">
            <span id="quota-math-text" style="color:var(--green)"></span>
          </div>

          <!-- Quota increase guide (collapsed by default) -->
          <details style="margin:0 18px 14px;border:1px solid var(--border);border-radius:6px;overflow:hidden">
            <summary style="padding:10px 14px;background:var(--bg2);cursor:pointer;font-family:var(--mono);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)">
              📈 Request a higher quota from Google (free)
            </summary>
            <div style="padding:14px;font-size:12px;line-height:1.8;color:var(--muted)">
              Google offers free quota increases for personal projects. It takes about 5 minutes:<br><br>
              <b style="color:var(--text)">1.</b> Go to <a href="https://console.cloud.google.com" target="_blank" style="color:var(--blue)">console.cloud.google.com</a> → select your project<br>
              <b style="color:var(--text)">2.</b> Search for <b style="color:var(--amber)">YouTube Data API v3</b> → click it → click <b style="color:var(--text)">Quotas &amp; System Limits</b><br>
              <b style="color:var(--text)">3.</b> Find <b style="color:var(--amber)">Queries per day</b> → click the pencil icon → request an increase<br>
              <b style="color:var(--text)">4.</b> For a personal project, requesting <b style="color:var(--text)">1,000,000 units/day</b> is usually auto-approved<br>
              <b style="color:var(--text)">5.</b> Each playlist insert costs 50 units → that's <b style="color:var(--green)">~20,000 songs/day</b> per project<br><br>
              <span style="color:var(--dim)">Tip: Approvals for personal projects usually come within minutes via email.</span>
            </div>
          </details>
        </div>

        <!-- Add / Edit project form -->
        <div id="add-project-panel" style="padding:14px 18px">
          <div id="cred-wizard">
            <div class="wizard-steps" id="wiz-steps">
              <div class="wiz-step active" id="ws-0">1 · Create project</div>
              <div class="wiz-step"        id="ws-1">2 · Enable API</div>
              <div class="wiz-step"        id="ws-2">3 · Get credentials</div>
              <div class="wiz-step"        id="ws-3">4 · Connect</div>
            </div>

            <!-- Step 0: Create project -->
            <div class="wiz-panel active" id="wp-0">
              <p style="font-size:13px;margin-bottom:14px;line-height:1.6">
                We need a free "API key" from Google so this app can talk to YouTube on your behalf.
                Takes about <b style="color:var(--text)">3 minutes</b> and is completely free.
              </p>
              <div class="wiz-action-row">
                <div class="wiz-action-icon">🌐</div>
                <div class="wiz-action-text">
                  <div class="wiz-action-title">Open Google Cloud Console</div>
                  <div class="wiz-action-sub">Don't worry — you don't need to be a developer!</div>
                </div>
                <a href="https://console.cloud.google.com" target="_blank" class="btn btn-primary btn-sm">Open →</a>
              </div>
              <div class="wiz-action-row">
                <div class="wiz-action-icon">📁</div>
                <div class="wiz-action-text">
                  <div class="wiz-action-title">Create a new project</div>
                  <div class="wiz-action-sub">Click <b style="color:var(--text)">"Select a project"</b> at the top → <b style="color:var(--text)">"New Project"</b> → name it anything (e.g. "OpenTune") → click <b style="color:var(--text)">Create</b></div>
                </div>
              </div>
              <div class="wiz-nav">
                <button class="btn btn-secondary btn-sm" id="wiz-back-btn" onclick="wizBack()" style="display:none">← Back to projects</button>
                <button class="btn btn-primary" onclick="wizGo(1)">I created a project →</button>
              </div>
            </div>

            <!-- Step 1: Enable API -->
            <div class="wiz-panel" id="wp-1">
              <div class="wiz-action-row">
                <div class="wiz-action-icon">🔍</div>
                <div class="wiz-action-text">
                  <div class="wiz-action-title">Search for "YouTube Data API v3"</div>
                  <div class="wiz-action-sub">In the Google Cloud search bar, type <b style="color:var(--amber)">YouTube Data API v3</b> and press Enter</div>
                </div>
              </div>
              <div class="wiz-action-row">
                <div class="wiz-action-icon">✅</div>
                <div class="wiz-action-text">
                  <div class="wiz-action-title">Click Enable</div>
                  <div class="wiz-action-sub">Click the result, then press the blue <b style="color:var(--text)">Enable</b> button.</div>
                </div>
              </div>
              <div class="wiz-nav">
                <button class="btn btn-secondary btn-sm" onclick="wizGo(0)">← Back</button>
                <button class="btn btn-primary" onclick="wizGo(2)">I enabled the API →</button>
              </div>
            </div>

            <!-- Step 2: Credentials -->
            <div class="wiz-panel" id="wp-2">
              <div class="wiz-action-row">
                <div class="wiz-action-icon">🔑</div>
                <div class="wiz-action-text">
                  <div class="wiz-action-title">Create OAuth credentials</div>
                  <div class="wiz-action-sub">
                    Go to <b style="color:var(--text)">APIs &amp; Services → Credentials</b><br>
                    Click <b style="color:var(--text)">+ Create Credentials → OAuth client ID</b><br>
                    If prompted for consent screen: choose <b style="color:var(--text)">External</b>, fill in any app name, save &amp; continue<br>
                    For Application type choose <b style="color:var(--amber)">Desktop app</b> → Create
                  </div>
                </div>
              </div>
              <div class="wiz-action-row" style="border-color:rgba(255,180,0,.35)">
                <div class="wiz-action-icon">👤</div>
                <div class="wiz-action-text">
                  <div class="wiz-action-title">Add every account you'll sync as a Test user</div>
                  <div class="wiz-action-sub">
                    New Google Cloud projects start in <b style="color:var(--text)">Testing</b> mode — only accounts you list here are allowed to log in, everyone else gets an "access blocked" screen.<br>
                    Go to <b style="color:var(--text)">APIs &amp; Services → OAuth consent screen → Audience</b> (or <b style="color:var(--text)">Test users</b> on older layouts) → <b style="color:var(--text)">+ Add users</b> → enter the Gmail address of <b style="color:var(--amber)">every</b> Google/YouTube account you plan to connect below (yours, family, etc.) → Save.<br>
                    You can add up to 100 accounts this way — no need to publish or verify the app for personal use.
                  </div>
                </div>
              </div>
              <div class="wiz-action-row">
                <div class="wiz-action-icon">⬇️</div>
                <div class="wiz-action-text">
                  <div class="wiz-action-title">Download the JSON file &amp; paste below</div>
                  <div class="wiz-action-sub">Click the <b style="color:var(--text)">⬇ Download JSON</b> button. Open it in any text editor and paste everything here:</div>
                </div>
              </div>
              <div class="field mt8">
                <textarea class="json-textarea" id="cred-json" placeholder='{"installed":{"client_id":"...","client_secret":"...",...}}'></textarea>
                <button class="btn btn-secondary btn-sm mt8" onclick="parseCredJson()">📋 Auto-fill from JSON</button>
              </div>
              <div style="font-size:11px;color:var(--muted);margin:10px 0;text-align:center">— or enter manually —</div>
              <div class="field">
                <label class="label">Project nickname (optional)</label>
                <input class="input" id="cred-name" type="text" placeholder="e.g. Project 1, My Account">
              </div>
              <div class="field">
                <label class="label">Client ID</label>
                <input class="input" id="cred-id" type="text" placeholder="xxxxxxx.apps.googleusercontent.com">
              </div>
              <div class="field">
                <label class="label">Client Secret</label>
                <input class="input" id="cred-secret" type="password" placeholder="GOCSPX-...">
              </div>
              <div class="wiz-nav">
                <button class="btn btn-secondary btn-sm" onclick="wizGo(1)">← Back</button>
                <button class="btn btn-primary" onclick="saveCredsAndNext()">Save &amp; Continue →</button>
              </div>
            </div>

            <!-- Step 3: Connect -->
            <div class="wiz-panel" id="wp-3">
              <div style="text-align:center;padding:20px 0">
                <div style="font-size:40px;margin-bottom:12px">🎵</div>
                <div style="font-size:15px;font-weight:700;margin-bottom:8px">Almost there!</div>
                <p style="font-size:13px;color:var(--muted);margin-bottom:20px;line-height:1.6">
                  Click below to sign in with your Google account.<br>
                  You'll see a browser window — click <b style="color:var(--text)">Allow</b>.
                </p>
                <button class="btn btn-primary" id="btn-connect" onclick="connectGoogle()" style="font-size:14px;padding:12px 28px">
                  🔗 Connect my Google Account
                </button>
                <div style="font-size:11px;color:var(--dim);margin-top:14px">
                  This app never sees your password. It only gets permission to manage YouTube playlists.
                </div>
              </div>
              <div class="wiz-nav">
                <button class="btn btn-secondary btn-sm" onclick="wizGo(2)">← Back</button>
              </div>
            </div>
          </div>
        </div>

      </div>
    </div>
  </div>

  
</div>

<!-- ═══ PLAYLISTS VIEW ══════════════════════════════════════════════════════ -->
<div id="view-playlists" class="view">
  <div class="grid2">

    <!-- Backup playlists -->
    <div class="card">
      <div class="card-header">
        <span class="card-title">Backup Playlists</span>
        <button class="btn btn-secondary btn-sm" onclick="refreshBackupPlaylists()">↻ Refresh</button>
      </div>
      <div id="backup-playlists-list"></div>
    </div>

    <!-- YT playlists + songs browse -->
    <div class="card" style="overflow:hidden">
      <div id="yt-pl-panel">
        <div class="card-header">
          <span class="card-title">YouTube Music Playlists</span>
          <button class="btn btn-secondary btn-sm" onclick="refreshYTPlaylists()">↻ Refresh</button>
        </div>
        <div id="yt-playlists-list">
          <div class="card-body" style="color:var(--muted);font-size:12px">
            Connect your Google account to see YouTube playlists.
          </div>
        </div>
      </div>
      <!-- Song browse sub-panel -->
      <div class="browse-panel" id="yt-songs-panel">
        <div class="browse-header">
          <button class="browse-back" onclick="closeYTSongs()">← Back to playlists</button>
          <span class="browse-title" id="yt-songs-title"></span>
          <span id="yt-songs-total" class="badge badge-dim"></span>
        </div>
        <div id="yt-songs-list" style="max-height:420px;overflow-y:auto"></div>
        <div class="pagination" id="yt-songs-pagination" style="padding:10px 16px"></div>
      </div>
    </div>
  </div>
</div>

<!-- ═══ SONGS VIEW ══════════════════════════════════════════════════════════ -->
<div id="view-songs" class="view">
  <div class="controls">
    <div class="search-wrap">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
      </svg>
      <input class="input search-input" id="song-search" placeholder="Search songs, artists..." oninput="onSearch()">
    </div>
    <select class="select" id="playlist-filter" onchange="onPlaylistFilter()">
      <option value="">All Songs</option>
    </select>
    <div class="flex" style="gap:4px">
      <span style="font-family:var(--mono);font-size:10px;color:var(--muted);margin-right:4px">SORT</span>
      <button class="sort-btn active" data-sort="title"      onclick="setSort('title')">Title</button>
      <button class="sort-btn"        data-sort="artist"     onclick="setSort('artist')">Artist</button>
      <button class="sort-btn"        data-sort="date"       onclick="setSort('date')">Year</button>
      <button class="sort-btn"        data-sort="date_added" onclick="setSort('date_added')">Added</button>
      <button class="sort-btn" id="order-btn" onclick="toggleOrder()">↑ ASC</button>
    </div>
    <button class="btn btn-secondary btn-sm" onclick="selectAll()">Select All Page</button>
    <button class="btn btn-secondary btn-sm" onclick="clearSelection()">Clear</button>
  </div>

  <div class="card" style="margin-bottom:16px">
    <div class="card-header">
      <span class="card-title">Playlist File Import</span>
    </div>
    <div class="card-body">
      <p style="color:var(--muted);font-size:12px;margin-bottom:10px">
        Export a playlist to CSV below, then upload the CSV here. Find and confirm a YouTube match for each song, then head to Sync → Custom Songs to upload the ones you matched.
      </p>
      <details style="margin-bottom:10px">
        <summary style="cursor:pointer;font-size:12px;color:var(--muted)">Open Exportify inline</summary>
        <div style="border:1px solid var(--border);border-radius:6px;overflow:hidden;background:var(--bg2);margin-top:8px">
          <iframe src="https://exportify.net/" title="Exportify" style="width:100%;height:360px;border:0;display:block"></iframe>
        </div>
        <a href="https://exportify.net/" target="_blank" rel="noopener" class="btn btn-secondary btn-sm" style="margin-top:8px">Not loading? Open in new tab ↗</a>
      </details>
      <input type="file" id="csv-file-input" accept=".csv" class="input" onchange="handleCsvFile(event)">
      <div class="flex" style="gap:10px;margin-top:10px;align-items:center">
        <label style="font-size:12px;color:var(--muted);display:flex;align-items:center;gap:6px">
          <input type="checkbox" id="csv-fast-toggle" checked onchange="syncFastMatchToggle(this.checked)"> Fast match (auto-pick high-confidence results)
        </label>
        <button class="btn btn-secondary btn-sm" id="csv-matchall-btn" onclick="fastMatchAll()">⚡ Match All</button>
      </div>
      <div id="csv-matchall-status" class="hidden" style="font-size:11px;color:var(--green);font-family:var(--mono);margin-top:8px;display:flex;align-items:center;gap:8px">
        <span id="csv-matchall-spinner">⏳</span><span id="csv-matchall-text"></span>
      </div>
      <div id="csv-results" style="margin-top:12px"></div>
    </div>
  </div>

  <div class="tbl-wrap">
    <table>
      <thead>
        <tr>
          <th style="width:32px"><input type="checkbox" id="check-all" onchange="toggleAll(this)"></th>
          <th style="width:40px"></th>
          <th onclick="setSort('title')"  class="sorted">Title</th>
          <th onclick="setSort('artist')">Artist</th>
          <th onclick="setSort('date')">Year</th>
          <th>♥</th>
          <th style="width:90px;font-family:var(--mono)">Video ID</th>
        </tr>
      </thead>
      <tbody id="songs-tbody"></tbody>
    </table>
  </div>

  <div class="pagination" id="pagination"></div>
  <div style="height:60px"></div>
</div>

<!-- ═══ SYNC VIEW ════════════════════════════════════════════════════════════ -->
<div id="view-sync" class="view">
  <div class="mode-tabs">
    <div class="mode-tab active" onclick="setSyncMode('map')">
      Map Existing<br><small style="font-size:9px;opacity:.6">Link backup → YT playlist</small>
    </div>
    <div class="mode-tab" onclick="setSyncMode('new')">
      Create New<br><small style="font-size:9px;opacity:.6">Upload as new playlists</small>
    </div>
    <div class="mode-tab" onclick="setSyncMode('custom')">
      Custom Songs<br><small style="font-size:9px;opacity:.6">Pick songs + destination</small>
    </div>
  </div>

  <!-- Map Existing -->
  <div id="sync-map" class="sync-panel">
    <div class="card">
      <div class="card-header">
        <span class="card-title">Map Backup Playlists → YouTube Playlists</span>
        <button class="btn btn-secondary btn-sm" onclick="addMapping()">+ Add Mapping</button>
      </div>
      <div class="card-body">
        <p style="color:var(--muted);font-size:12px;margin-bottom:16px">
          Each row maps a backup playlist to an existing YouTube Music playlist. Songs will be added to the YouTube playlist.
        </p>
        <div id="mappings-list"></div>
        <div class="flex mt16" style="gap:10px">
          <button class="btn btn-secondary" onclick="previewSync('map')">🔍 Preview songs to add</button>
          <button class="btn btn-primary" onclick="startSync('map')">▶ Start Sync</button>
        </div>
      </div>
    </div>
    <div id="preview-map" class="sync-preview" style="display:none"></div>
  </div>

  <!-- Create New -->
  <div id="sync-new" class="sync-panel" style="display:none">
    <div class="grid2">
      <div class="card">
        <div class="card-header"><span class="card-title">Select Backup Playlists</span></div>
        <div id="new-backup-list"></div>
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">Options</span></div>
        <div class="card-body">
          <div class="field">
            <label class="label">Privacy</label>
            <select class="select" id="new-privacy" style="width:100%">
              <option value="private">Private</option>
              <option value="unlisted">Unlisted</option>
              <option value="public">Public</option>
            </select>
          </div>
          <div style="background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:12px;font-size:12px;color:var(--muted);line-height:1.7;margin-bottom:16px">
            <b style="color:var(--amber)">⚠ Quota note:</b> YouTube API allows ~200 songs/day on free tier.
            Large playlists will be split across multiple days automatically.
          </div>
          <div class="flex" style="gap:10px">
            <button class="btn btn-secondary" onclick="previewSync('new')">🔍 Preview songs to add</button>
            <button class="btn btn-primary" onclick="startSync('new')">▶ Create & Upload</button>
          </div>
        </div>
      </div>
    </div>
    <div id="preview-new" class="sync-preview" style="display:none"></div>
  </div>

  <!-- Custom Songs -->
  <div id="sync-custom" class="sync-panel" style="display:none">
    <div class="grid2">
      <div class="card">
        <div class="card-header">
          <span class="card-title">Selected Songs</span>
          <span id="custom-sel-count" class="badge badge-dim">0 songs</span>
        </div>
        <div class="card-body">
          <div id="custom-songs-list" style="max-height:400px;overflow-y:auto"></div>
          <p style="color:var(--muted);font-size:12px;margin-top:8px">
            Go to the Songs tab to select songs. They'll appear here.
          </p>
        </div>
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">Destination Playlist</span></div>
        <div class="card-body">
          <div class="flex" style="gap:16px;margin-bottom:12px">
            <label style="font-size:12px;color:var(--muted);display:flex;align-items:center;gap:6px">
              <input type="radio" name="custom-dest-mode" value="existing" checked onchange="setCustomDestMode('existing')"> Existing playlist
            </label>
            <label style="font-size:12px;color:var(--muted);display:flex;align-items:center;gap:6px">
              <input type="radio" name="custom-dest-mode" value="new" onchange="setCustomDestMode('new')"> New playlist
            </label>
          </div>
          <div id="custom-dest-existing" class="field">
            <label class="label">YouTube Music Playlist</label>
            <select class="select" id="custom-yt-playlist" style="width:100%">
              <option value="">— select playlist —</option>
            </select>
          </div>
          <div id="custom-dest-new" class="field" style="display:none">
            <label class="label">New Playlist Name</label>
            <input class="input" id="custom-new-playlist-name" type="text" placeholder="Playlist name" style="width:100%;margin-bottom:10px">
            <label class="label">Privacy</label>
            <select class="select" id="custom-new-privacy" style="width:100%">
              <option value="private">Private</option>
              <option value="unlisted">Unlisted</option>
              <option value="public">Public</option>
            </select>
          </div>
          <div class="flex" style="gap:10px;margin-top:14px">
            <button class="btn btn-primary" onclick="startSync('custom')">▶ Sync Selected Songs</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ═══ PROGRESS VIEW ════════════════════════════════════════════════════════ -->
<div id="view-progress" class="view">
  <div id="resume-banner" class="hidden">
    <div class="resume-banner-text">
      <strong id="resume-banner-title">⚠ Unfinished sync jobs detected</strong><br>
      <span id="resume-banner-msg" style="color:var(--muted);font-size:12px"></span>
    </div>
    <div class="resume-banner-btns">
      <button class="btn btn-secondary btn-sm" onclick="dismissResumeBanner()">Dismiss</button>
      <button class="btn" style="background:var(--amber);color:#000" onclick="resumeAllJobs()">Resume All →</button>
    </div>
  </div>
  <div class="flex-between mt8" style="margin-bottom:16px">
    <div style="font-family:var(--mono);font-size:11px;color:var(--muted)">ACTIVE & RECENT JOBS</div>
    <button class="btn btn-secondary btn-sm" onclick="refreshJobs()">↻ Refresh</button>
  </div>
  <div id="jobs-container">
    <div style="color:var(--muted);font-size:13px;padding:20px 0">No sync jobs yet.</div>
  </div>
</div>

</main>

<!-- Selection footer bar -->
<div id="sel-bar">
  <div>
    <span id="sel-count" class="mono text-green">0 songs selected</span>
    <span style="font-size:11px;color:var(--muted);margin-left:12px">
      Go to <b>Sync → Custom Songs</b> to upload
    </span>
  </div>
  <div class="flex">
    <button class="btn btn-secondary btn-sm" onclick="clearSelection()">Clear</button>
    <button class="btn btn-primary btn-sm" onclick="goToCustomSync()">Sync Selected →</button>
  </div>
</div>

<div id="toast"></div>
<div id="preview-player" style="width:0;height:0;overflow:hidden;position:absolute"></div>
<div id="preview-bar" class="hidden">
  <span id="preview-bar-label" class="mono" style="font-size:11px;color:var(--muted);max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
  <button class="btn btn-secondary btn-sm" onclick="previewJump(0)">⏮ Start</button>
  <button class="btn btn-secondary btn-sm" onclick="previewJump(0.5)">Mid</button>
  <button class="btn btn-secondary btn-sm" onclick="previewJump(0.9)">End ⏭</button>
  <input type="range" id="preview-seek" min="0" max="1000" value="0" style="flex:1;accent-color:var(--green)"
    oninput="previewSeeking=true" onchange="previewSeekCommit(this.value)">
  <span id="preview-time" class="mono" style="font-size:10px;color:var(--dim);white-space:nowrap">0:00 / 0:00</span>
  <button class="btn btn-secondary btn-sm" onclick="stopPreview()">■ Stop</button>
</div>
<script src="https://www.youtube.com/iframe_api"></script>

<script>
// ═══════════════════════════════════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════════════════════════════════
const S = {
  backup:   { loaded: false, playlists: [], songs: 0 },
  yt:       { connected: false, playlists: [], channel: '' },
  songs:    { page: 1, perPage: 50, total: 0, pages: 1,
             items: [], selected: new Set(),
             playlist: '', sort: 'title', order: 'asc', search: '',
             cache: {} },
  sync:     { mode: 'map', mappings: [], selectedNewPlaylists: new Set() },
  jobs:     [],
  settings: { csvFastMatch: true },
};

// ═══════════════════════════════════════════════════════════════════════════
// API
// ═══════════════════════════════════════════════════════════════════════════
async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  return r.json();
}

// ═══════════════════════════════════════════════════════════════════════════
// TOAST
// ═══════════════════════════════════════════════════════════════════════════
let toastTimer;
function toast(msg, type = 'info') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `show ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.className = '', 3500);
}

// ═══════════════════════════════════════════════════════════════════════════
// NAVIGATION
// ═══════════════════════════════════════════════════════════════════════════
function showView(name) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById(`view-${name}`).classList.add('active');
  const navBtns = document.querySelectorAll('nav button');
  const idx = ['setup','playlists','songs','sync','progress'].indexOf(name);
  if (idx >= 0) navBtns[idx].classList.add('active');

  if (name === 'playlists') { refreshBackupPlaylists(); if (S.yt.connected) refreshYTPlaylists(); }
  if (name === 'songs')     { loadSongs(); }
  if (name === 'sync')      { refreshSyncUI(); }
  if (name === 'progress')  { refreshJobs(); }
}

// ═══════════════════════════════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════════════════════════════
async function init() {
  const status = await api('/api/status');

  if (status.backup_loaded) {
    S.backup.loaded = true;
    document.getElementById('backup-stats').style.display = '';
    document.getElementById('backup-status').textContent = 'Loaded';
    document.getElementById('backup-status').className = 'badge badge-green';
    document.getElementById('hdr-backup').textContent = 'Backup ✓';
    document.getElementById('hdr-backup').className = 'badge badge-green';
    if (status.last_backup)
      document.getElementById('backup-path').value = status.last_backup;
    refreshBackupStats();
  }

  // Render multi-project credential UI
  renderProjects(status.credentials || []);

  if (status.csv_songs && status.csv_songs.length) {
    CSV.songs = status.csv_songs;
    renderCsvResults();
    document.getElementById('csv-matchall-status').classList.remove('hidden');
    document.getElementById('csv-matchall-text').textContent =
      `${status.csv_songs.length} songs from a previously loaded CSV`;
    document.getElementById('csv-matchall-spinner').textContent = '📄';
    setTimeout(() => document.getElementById('csv-matchall-status').classList.add('hidden'), 4000);
  }

  if (status.authenticated) {
    S.yt.connected = true;
    S.yt.channel = status.channel_name || 'Connected';
    updateAuthBadge(status.credentials || []);
  }

  // Check for resumable jobs from a previous session
  const jobs = await api('/api/jobs');
  const resumable = jobs.filter(j =>
    ['paused','quota_exceeded','error'].includes(j.status) && j.has_cfg);
  if (resumable.length) {
    showResumeBanner(resumable);
    showView('progress');
  }

  // Check if redirected back from OAuth
  // Check if redirected back from OAuth
  if (location.search.includes('auth=error')) {
    const params = new URLSearchParams(location.search);
    history.replaceState({}, '', '/');
    const rawMsg = params.get('msg') || 'unknown_error';
    if (rawMsg === 'access_denied') {
      toast('Google blocked this account — add it as a Test user in your OAuth consent screen (see Setup → Test users step), or use an account that\'s already listed.', 'error');
    } else if (rawMsg === 'no_code') {
      toast('Google sign-in was cancelled or did not return a code — try connecting again.', 'error');
    } else {
      toast(`Connection failed: ${rawMsg}`, 'error');
    }
  }

  if (location.search.includes('auth=success')) {
    history.replaceState({}, '', '/');
    const s2 = await api('/api/status');
    renderProjects(s2.credentials || []);
    if (s2.authenticated) {
      S.yt.connected = true;
      S.yt.channel   = s2.channel_name || 'Connected';
      updateAuthBadge(s2.credentials || []);
      toast('✓ Connected! Project added successfully.', 'success');
    }
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// SETUP ACTIONS
// ═══════════════════════════════════════════════════════════════════════════
function applyBackupLoaded(r) {
  S.backup = { loaded: true, playlists: r.playlists, songs: r.songs };
  document.getElementById('stat-songs').textContent = r.songs.toLocaleString();
  document.getElementById('stat-playlists').textContent = r.playlists;
  document.getElementById('backup-stats').style.display = '';
  document.getElementById('backup-status').textContent = 'Loaded';
  document.getElementById('backup-status').className = 'badge badge-green';
  document.getElementById('hdr-backup').textContent = 'Backup ✓';
  document.getElementById('hdr-backup').className = 'badge badge-green';
  toast(`Loaded ${r.songs.toLocaleString()} songs across ${r.playlists} playlists`, 'success');
}

async function loadBackup() {
  const path = document.getElementById('backup-path').value.trim();
  if (!path) { toast('Please enter the backup file path', 'error'); return; }
  toast('Processing backup file...', 'info');
  const r = await api('/api/backup/load', { method: 'POST', body: { path } });
  if (r.error) { toast(r.error, 'error'); return; }
  applyBackupLoaded(r);
}

async function handleLibraryFile(event) {
  const file = event.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  toast('Processing file...', 'info');
  let r;
  try { r = await (await fetch('/api/library/upload', { method: 'POST', body: fd })).json(); }
  catch (e) { toast('Upload failed', 'error'); return; }
  if (r.error) { toast(r.error, 'error'); return; }
  if (r.kind === 'csv') {
    CSV.songs = r.songs;
    CSV.matched = {};
    renderCsvResults();
    toast(`Loaded ${r.total} songs from CSV`, 'success');
    showView('songs');
  } else {
    applyBackupLoaded(r);
  }
}

async function refreshBackupStats() {
  const r = await api('/api/backup/playlists');
  if (!r.error) {
    S.backup.playlists = r;
    document.getElementById('stat-playlists').textContent = r.length;
    const total = r.reduce((a,p) => a + p.song_count, 0);
    document.getElementById('stat-songs').textContent = total.toLocaleString();
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// MULTI-PROJECT CREDENTIALS
// ═══════════════════════════════════════════════════════════════════════════
let wizStep = 0;
let _pendingCredIdx = null; // which cred slot we're setting up

function renderProjects(creds) {
  const projectsPanel  = document.getElementById('projects-panel');
  const addPanel       = document.getElementById('add-project-panel');
  if (!creds.length) {
    // No credentials at all — show wizard directly
    projectsPanel.style.display = 'none';
    addPanel.style.display      = '';
    document.getElementById('wiz-back-btn').style.display = 'none';
    return;
  }

  projectsPanel.style.display = '';
  addPanel.style.display      = 'none';

  // Render project cards
  const el = document.getElementById('projects-list');
  el.innerHTML = creds.map(c => `
    <div style="display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--border)">
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;font-weight:600;display:flex;align-items:center;gap:8px">
          ${esc(c.name)}
          ${c.active ? '<span class="badge badge-green" style="font-size:9px">ACTIVE</span>' : ''}
        </div>
        <div style="font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:2px">
          ${c.connected
            ? `<span style="color:var(--green)">✓ Connected${c.channel ? ' · '+esc(c.channel) : ''}</span>`
            : `<span style="color:var(--amber)">⚠ Not connected yet</span>`}
        </div>
      </div>
      <div style="display:flex;gap:6px;flex-shrink:0">
        ${!c.connected
          ? `<button class="btn btn-primary btn-sm" onclick="connectProject(${c.idx})">Connect →</button>`
          : !c.active
            ? `<button class="btn btn-secondary btn-sm" onclick="activateProject(${c.idx})">Set Active</button>`
            : ''}
        <button class="btn btn-danger btn-sm" onclick="deleteProject(${c.idx})" title="Remove">✕</button>
      </div>
    </div>
  `).join('');

  // Quota math
  const connected = creds.filter(c => c.connected).length;
  const daily = connected * 200;
  document.getElementById('quota-math-text').textContent =
    connected === 0 ? 'Connect a project to start syncing'
    : `${connected} project${connected>1?'s':''} × ~200 songs/day = ~${daily.toLocaleString()} songs/day capacity`;

  updateAuthBadge(creds);
}

function updateAuthBadge(creds) {
  const connected = (creds || []).filter(c => c.connected);
  const badge = document.getElementById('auth-status-badge');
  const hdr   = document.getElementById('hdr-auth');
  if (connected.length) {
    badge.textContent = `${connected.length} project${connected.length>1?'s':''} connected`;
    badge.className   = 'badge badge-green';
    hdr.textContent   = `${connected.length} YT project${connected.length>1?'s':''}`;
    hdr.className     = 'badge badge-green';
    S.yt.connected    = true;
  } else {
    badge.textContent = 'Not connected';
    badge.className   = 'badge badge-dim';
    hdr.textContent   = 'Not connected';
    hdr.className     = 'badge badge-dim';
    S.yt.connected    = false;
  }
}

function showAddProject() {
  document.getElementById('projects-panel').style.display = 'none';
  document.getElementById('add-project-panel').style.display = '';
  document.getElementById('wiz-back-btn').style.display = '';
  // Clear form
  ['cred-json','cred-id','cred-secret'].forEach(id => {
    const el = document.getElementById(id); if (el) el.value = '';
  });
  document.getElementById('cred-name').value = `Project ${(S._credCount||1)+1}`;
  _pendingCredIdx = null;
  wizGo(0);
}

async function wizBack() {
  const s = await api('/api/status');
  renderProjects(s.credentials || []);
}

function wizGo(n) {
  const panels = document.querySelectorAll('.wiz-panel');
  const steps  = document.querySelectorAll('.wiz-step');
  panels.forEach((p,i) => p.classList.toggle('active', i === n));
  steps.forEach((s,i) => {
    s.classList.remove('active','done');
    if (i < n)  s.classList.add('done');
    if (i === n) s.classList.add('active');
  });
  wizStep = n;
}

function parseCredJson() {
  const raw = document.getElementById('cred-json').value.trim();
  if (!raw) { toast('Paste the JSON file content first', 'error'); return; }
  try {
    const data = JSON.parse(raw);
    const section = data.installed || data.web || data;
    const id  = section.client_id  || '';
    const sec = section.client_secret || '';
    if (!id || !sec) { toast('Could not find client_id/client_secret in that JSON', 'error'); return; }
    document.getElementById('cred-id').value     = id;
    document.getElementById('cred-secret').value = sec;
    toast('Credentials parsed! Click Save & Continue →', 'success');
  } catch(e) {
    toast('Could not parse JSON — make sure you copied the whole file contents', 'error');
  }
}

async function saveCredsAndNext() {
  const id   = document.getElementById('cred-id').value.trim();
  const sec  = document.getElementById('cred-secret').value.trim();
  const name = document.getElementById('cred-name').value.trim();
  if (!id || !sec) { toast('Enter both Client ID and Secret (or paste the JSON above)', 'error'); return; }
  const body = { client_id: id, client_secret: sec };
  if (name) body.name = name;
  if (_pendingCredIdx !== null) body.idx = _pendingCredIdx;
  const r = await api('/api/credentials', { method: 'POST', body });
  if (r.error) { toast(r.error, 'error'); return; }
  _pendingCredIdx = r.idx;
  toast('Credentials saved!', 'success');
  wizGo(3);
}

async function saveCreds() { await saveCredsAndNext(); }

async function connectProject(idx) {
  _pendingCredIdx = idx;
  const r = await api(`/api/auth/start?cred=${idx}`);
  if (r.error) { toast(r.error, 'error'); return; }
  window.location.href = r.url;
}

async function connectGoogle() {
  const idx = _pendingCredIdx !== null ? _pendingCredIdx : 0;
  const r = await api(`/api/auth/start?cred=${idx}`);
  if (r.error) { toast(r.error, 'error'); return; }
  window.location.href = r.url;
}

async function activateProject(idx) {
  await api(`/api/credentials/${idx}/activate`, { method: 'POST' });
  const s = await api('/api/status');
  renderProjects(s.credentials || []);
  S.yt.playlists = [];
  await refreshYTPlaylists();
  if (document.getElementById('view-playlists').classList.contains('active')) refreshBackupPlaylists();
  if (document.getElementById('view-sync').classList.contains('active')) refreshSyncUI();
  toast('Active project switched', 'success');
}

async function deleteProject(idx) {
  if (!confirm('Remove this project? Its token will be deleted.')) return;
  await api(`/api/credentials/${idx}`, { method: 'DELETE' });
  const s = await api('/api/status');
  renderProjects(s.credentials || []);
  updateAuthBadge(s.credentials || []);
  populateYTSelects(S.yt.playlists);
  toast('Project removed', 'info');
}

async function logout() {
  // Disconnect the active credential
  const s = await api('/api/status');
  await api('/api/auth/logout', { method: 'POST', body: { idx: s.active_cred } });
  const s2 = await api('/api/status');
  renderProjects(s2.credentials || []);
  toast('Disconnected', 'info');
}

// legacy no-op kept for compatibility
function setAuthUI(connected, channel) {}

// ═══════════════════════════════════════════════════════════════════════════
// PLAYLISTS VIEW
// ═══════════════════════════════════════════════════════════════════════════
async function refreshBackupPlaylists() {
  if (!S.backup.loaded) {
    document.getElementById('backup-playlists-list').innerHTML =
      '<div class="card-body" style="color:var(--muted);font-size:12px">Load a backup file first.</div>';
    return;
  }
  const playlists = await api('/api/backup/playlists');
  S.backup.playlists = playlists;
  const el = document.getElementById('backup-playlists-list');
  if (!playlists.length) {
    el.innerHTML = '<div class="card-body" style="color:var(--muted)">No playlists found.</div>';
    return;
  }
  el.innerHTML = playlists.map(p => `
    <div class="pl-row" onclick="filterByPlaylist('${p.id}')">
      <div class="thumb-placeholder">♪</div>
      <div class="pl-name">${esc(p.name)}</div>
      <div class="pl-count mono">${p.song_count.toLocaleString()}</div>
      <div style="color:var(--dim);font-size:18px;margin-left:4px">›</div>
    </div>
  `).join('');
  populatePlaylistFilter(playlists);
}

async function refreshYTPlaylists() {
  if (!S.yt.connected) return;
  const el = document.getElementById('yt-playlists-list');
  el.innerHTML = '<div class="card-body" style="color:var(--muted);font-size:12px">Loading...</div>';
  const playlists = await api('/api/youtube/playlists');
  if (playlists.error) {
    el.innerHTML = `<div class="card-body" style="color:var(--red);font-size:12px">${esc(playlists.error)}</div>`;
    return;
  }
  S.yt.playlists = playlists;
  if (!playlists.length) {
    el.innerHTML = '<div class="card-body" style="color:var(--muted)">No playlists found.</div>';
    return;
  }
  el.innerHTML = playlists.map(p => `
    <div class="pl-row" onclick="openYTSongs('${p.id}', ${JSON.stringify(esc(p.name))}, ${p.song_count||0})" title="Click to browse songs">
      ${p.thumbnail ? `<img class="thumb" src="${p.thumbnail}" alt="">` : '<div class="thumb-placeholder">▶</div>'}
      <div class="pl-name">${esc(p.name)}</div>
      <div class="pl-count mono">${(p.song_count||0).toLocaleString()}</div>
      <div style="color:var(--dim);font-size:16px;margin-left:4px">›</div>
    </div>
  `).join('');
  populateYTSelects(playlists);
}

// ─── YT Playlist Song Browsing ───────────────────────────────────────────────
const YTS = { pid: '', name: '', total: 0, nextToken: '', prevToken: '', tokenHistory: [] };

async function openYTSongs(pid, name, total) {
  YTS.pid = pid; YTS.name = name; YTS.total = total;
  YTS.tokenHistory = ['']; YTS.nextToken = ''; YTS.prevToken = '';
  document.getElementById('yt-songs-title').textContent = name;
  document.getElementById('yt-songs-total').textContent = `${total} songs`;
  document.getElementById('yt-pl-panel').style.display = 'none';
  document.getElementById('yt-songs-panel').classList.add('open');
  await loadYTSongs('');
}

function closeYTSongs() {
  document.getElementById('yt-songs-panel').classList.remove('open');
  document.getElementById('yt-pl-panel').style.display = '';
}

async function loadYTSongs(pageToken) {
  const el = document.getElementById('yt-songs-list');
  el.innerHTML = '<div style="padding:20px;color:var(--muted);font-size:12px;font-family:var(--mono)">Loading…</div>';
  const params = new URLSearchParams({ max_results: 50, ...(pageToken ? { page_token: pageToken } : {}) });
  const data = await api(`/api/youtube/playlist/${YTS.pid}/songs?${params}`);
  if (data.error) {
    el.innerHTML = `<div style="padding:16px;color:var(--red);font-size:12px">${esc(data.error)}</div>`;
    return;
  }
  YTS.nextToken = data.next_page_token || '';
  YTS.prevToken = data.prev_page_token || '';
  if (!data.songs.length) {
    el.innerHTML = '<div style="padding:20px;color:var(--muted);font-size:12px">No songs found.</div>';
    renderYTSongsPagination();
    return;
  }
  el.innerHTML = data.songs.map(s => `
    <div class="pl-row" style="cursor:default">
      ${s.thumbnail ? `<img class="thumb" src="${s.thumbnail}" alt="" onerror="this.style.display='none'">` : '<div class="thumb-placeholder">♪</div>'}
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(s.title)}</div>
        <div style="font-size:11px;color:var(--muted);font-family:var(--mono)">${esc(s.channel||'')}</div>
      </div>
      <div style="font-family:var(--mono);font-size:11px;color:var(--dim)">#${s.position+1}</div>
    </div>
  `).join('');
  renderYTSongsPagination();
}

function renderYTSongsPagination() {
  const el = document.getElementById('yt-songs-pagination');
  const history = YTS.tokenHistory;
  const curIdx  = history.length - 1;
  el.innerHTML = `
    <button class="page-btn" onclick="ytSongsPrev()" ${curIdx <= 0 ? 'disabled' : ''}>‹ Prev</button>
    <span style="color:var(--muted)">Page ${curIdx + 1}</span>
    <button class="page-btn" onclick="ytSongsNext()" ${!YTS.nextToken ? 'disabled' : ''}>Next ›</button>
  `;
}

async function ytSongsNext() {
  if (!YTS.nextToken) return;
  YTS.tokenHistory.push(YTS.nextToken);
  await loadYTSongs(YTS.nextToken);
}

async function ytSongsPrev() {
  if (YTS.tokenHistory.length <= 1) return;
  YTS.tokenHistory.pop();
  const prevToken = YTS.tokenHistory[YTS.tokenHistory.length - 1];
  await loadYTSongs(prevToken);
}

function filterByPlaylist(pid) {
  S.songs.playlist = pid;
  showView('songs');
  document.getElementById('playlist-filter').value = pid;
  loadSongs();
}

function populatePlaylistFilter(playlists) {
  const sel = document.getElementById('playlist-filter');
  sel.innerHTML = '<option value="">All Songs</option>' +
    playlists.map(p => `<option value="${p.id}">${esc(p.name)} (${p.song_count})</option>`).join('');
  if (S.songs.playlist) sel.value = S.songs.playlist;
}

function populateYTSelects(playlists) {
  const opts = playlists.map(p =>
    `<option value="${p.id}">${esc(p.name)} (${p.song_count})</option>`).join('');
  document.getElementById('custom-yt-playlist').innerHTML =
    '<option value="">— select playlist —</option>' + opts;
}

// ═══════════════════════════════════════════════════════════════════════════
// SONGS VIEW
// ═══════════════════════════════════════════════════════════════════════════
let searchDebounce;
function onSearch() {
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => {
    S.songs.search = document.getElementById('song-search').value;
    S.songs.page = 1;
    loadSongs();
  }, 300);
}

function onPlaylistFilter() {
  S.songs.playlist = document.getElementById('playlist-filter').value;
  S.songs.page = 1;
  loadSongs();
}

function setSort(col) {
  if (S.songs.sort === col) {
    S.songs.order = S.songs.order === 'asc' ? 'desc' : 'asc';
  } else {
    S.songs.sort = col;
    S.songs.order = 'asc';
  }
  S.songs.page = 1;
  updateSortBtns();
  loadSongs();
}

function toggleOrder() {
  S.songs.order = S.songs.order === 'asc' ? 'desc' : 'asc';
  document.getElementById('order-btn').textContent = S.songs.order === 'asc' ? '↑ ASC' : '↓ DESC';
  loadSongs();
}

function updateSortBtns() {
  document.querySelectorAll('.sort-btn[data-sort]').forEach(b => {
    b.classList.toggle('active', b.dataset.sort === S.songs.sort);
  });
}

async function loadSongs() {
  if (!S.backup.loaded) return;
  const params = new URLSearchParams({
    page: S.songs.page, per_page: S.songs.perPage,
    sort: S.songs.sort, order: S.songs.order,
    search: S.songs.search,
    ...(S.songs.playlist ? { playlist_id: S.songs.playlist } : {}),
  });
  const data = await api('/api/backup/songs?' + params);
  if (data.error) { toast(data.error, 'error'); return; }
  S.songs.items  = data.songs;
  S.songs.total  = data.total;
  S.songs.pages  = data.pages;
  data.songs.forEach(s => { S.songs.cache[s.id] = s; });
  renderSongsTable(data.songs);
  renderPagination();
}

function renderSongsTable(songs) {
  const tbody = document.getElementById('songs-tbody');
  if (!songs.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="color:var(--muted);text-align:center;padding:24px">No songs found</td></tr>';
    return;
  }
  tbody.innerHTML = songs.map(s => {
    const sel = S.songs.selected.has(s.id);
    return `
    <tr class="${sel ? 'tr-selected' : ''}" onclick="toggleSong('${s.id}',this)">
      <td onclick="event.stopPropagation()">
        <input type="checkbox" ${sel ? 'checked' : ''} onchange="toggleSong('${s.id}',this.closest('tr'))">
      </td>
      <td>${s.thumbnailUrl
        ? `<img class="thumb" src="${s.thumbnailUrl}" alt="" onerror="this.style.display='none'">`
        : '<div class="thumb-placeholder">♪</div>'}</td>
      <td>${esc(s.title || '')}</td>
      <td class="text-muted">${esc(s.artist || '')}</td>
      <td class="mono text-muted">${s.year || ''}</td>
      <td>${s.liked ? '<span class="heart">♥</span>' : ''}</td>
      <td class="mono" style="font-size:11px;color:var(--dim)">${s.id}</td>
    </tr>`;
  }).join('');
}

function toggleSong(id, row) {
  if (S.songs.selected.has(id)) {
    S.songs.selected.delete(id);
    row.classList.remove('tr-selected');
    row.querySelector('input[type=checkbox]').checked = false;
  } else {
    S.songs.selected.add(id);
    row.classList.add('tr-selected');
    row.querySelector('input[type=checkbox]').checked = true;
  }
  updateSelBar();
}

function toggleAll(chk) {
  S.songs.items.forEach(s => {
    if (chk.checked) S.songs.selected.add(s.id);
    else S.songs.selected.delete(s.id);
  });
  renderSongsTable(S.songs.items);
  updateSelBar();
}

function selectAll() {
  S.songs.items.forEach(s => S.songs.selected.add(s.id));
  renderSongsTable(S.songs.items);
  updateSelBar();
}

function clearSelection() {
  S.songs.selected.clear();
  renderSongsTable(S.songs.items);
  updateSelBar();
}

function removeSel(id) {
  S.songs.selected.delete(id);
  renderSongsTable(S.songs.items);
  updateSelBar();
  updateCustomSongsPanel();
}

function updateSelBar() {
  const n = S.songs.selected.size;
  document.getElementById('sel-count').textContent = `${n} song${n !== 1 ? 's' : ''} selected`;
  document.getElementById('sel-bar').classList.toggle('show', n > 0);
}

const CSV = { songs: [], matched: {}, lastResults: {}, lastLowConf: {} };

function handleCsvFile(event) {
  const file = event.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = async () => {
    const r = await api('/api/csv/import', { method: 'POST', body: { content: reader.result } });
    if (r.error) { toast(r.error, 'error'); return; }
    CSV.songs = r.songs;
    CSV.matched = {};
    renderCsvResults();
    toast(`Loaded ${r.total} songs from CSV`, 'success');
  };
  reader.readAsText(file);
}

function renderCsvResults() {
  const el = document.getElementById('csv-results');
  if (!CSV.songs.length) { el.innerHTML = ''; return; }
  el.innerHTML = CSV.songs.map(s => {
    const m = CSV.matched[s.csv_id];
    const pending = CSV.lastResults[s.csv_id];
    let body;
    if (m) {
      body = `<span style="font-size:11px;color:var(--green)">✓ matched: ${esc(m.title)}</span>
             <button class="btn btn-secondary btn-sm" onclick="searchCsvSong('${s.csv_id}')">Change</button>`;
    } else if (pending) {
      body = renderCsvMatchOptions(s.csv_id, pending, { lowConfidence: !!CSV.lastLowConf[s.csv_id] });
    } else {
      body = `<button class="btn btn-secondary btn-sm" onclick="searchCsvSong('${s.csv_id}')">🔍 Find on YouTube</button>`;
    }
    return `<div style="padding:8px 0;border-bottom:1px solid var(--border)">
      <div style="font-size:12px"><b>${esc(s.title)}</b> <span style="color:var(--muted)">· ${esc(s.artist)}</span></div>
      <div id="csv-match-${s.csv_id}" style="margin-top:4px">${body}</div>
    </div>`;
  }).join('');
}

function renderCsvMatchOptions(csvId, results, opts = {}) {
  if (!results.length) return `<span style="font-size:11px;color:var(--muted)">No results</span>`;
  return `${opts.lowConfidence ? `<div style="font-size:10px;color:var(--amber);margin-bottom:4px">Low confidence — pick manually</div>` : ''}
     ${results.map((r, i) => `
    <div style="display:flex;align-items:center;gap:8px;padding:4px 0">
      <img src="${esc(r.thumbnail)}" style="width:28px;height:28px;border-radius:3px;object-fit:cover" onerror="this.style.display='none'">
      <span style="flex:1;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
        ${esc(r.title)} <span style="color:var(--muted)">· ${esc(r.channel)}</span>
        <span class="mono" style="color:var(--dim);font-size:10px"> · ${fmtTime(r.duration_secs)} · </span><span class="badge ${r.definition==='HD' ? 'badge-green' : 'badge-dim'}" style="font-size:9px;padding:1px 5px">${r.definition}</span>
      </span>
      <button class="btn btn-secondary btn-sm preview-btn" data-vid="${r.id}" onclick="togglePreview('${r.id}', this)">▶ Preview</button>
      <button class="btn btn-primary btn-sm" onclick="pickCsvMatch('${csvId}',${i})">Use</button>
    </div>`).join('')}`;
}

const CSV_CONFIDENCE_THRESHOLD = 0.72;

async function exitApp() {
  if (!confirm('Stop the server and close this tab?')) return;
  try { await api('/api/shutdown', { method: 'POST' }); } catch (e) {}
  window.close();
  setTimeout(() => {
    document.body.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:center;height:100vh;font-family:var(--mono);color:var(--muted);font-size:13px">Server stopped — you can close this tab now.</div>';
  }, 400);
}

function selectImportPath(path) {
  document.getElementById('import-path-choice').style.display = 'none';
  document.getElementById('import-path-backup').style.display = path === 'backup' ? '' : 'none';
  document.getElementById('import-path-csv').style.display    = path === 'csv'    ? '' : 'none';
}

function showImportChoice() {
  document.getElementById('import-path-choice').style.display = '';
  document.getElementById('import-path-backup').style.display = 'none';
  document.getElementById('import-path-csv').style.display    = 'none';
}

function showExportifyEmbed() {
  document.getElementById('exportify-embed-wrap').style.display = '';
}

function cwizGo(n) {
  document.querySelectorAll('.cwiz-panel').forEach((p,i) => p.classList.toggle('active', i === n));
  document.querySelectorAll('.cwiz-step').forEach((s,i) => {
    s.classList.remove('active','done');
    if (i < n)  s.classList.add('done');
    if (i === n) s.classList.add('active');
  });
}

async function handleWizardCsvUpload(event) {
  const file = event.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  toast('Processing CSV...', 'info');
  let r;
  try { r = await (await fetch('/api/library/upload', { method: 'POST', body: fd })).json(); }
  catch (e) { toast('Upload failed', 'error'); return; }
  if (r.error) { toast(r.error, 'error'); return; }
  if (r.kind !== 'csv') { toast('That looked like a backup file, not a CSV — use the Backup File section above', 'error'); return; }
  CSV.songs = r.songs;
  CSV.matched = {};
  document.getElementById('cwiz-count').textContent = `${r.total} songs loaded`;
  cwizGo(2);
  toast(`Loaded ${r.total} songs from CSV`, 'success');
}

let ytPlayer = null, ytReady = false, previewSeeking = false, previewTimer = null;

function onYouTubeIframeAPIReady() {
  ytPlayer = new YT.Player('preview-player', {
    height: '0', width: '0', playerVars: { controls: 0 },
    events: { onReady: () => { ytReady = true; }, onStateChange: onPreviewStateChange },
  });
}

function onPreviewStateChange(e) {
  if (e.data === YT.PlayerState.ENDED) stopPreview();
}

function fmtTime(secs) {
  secs = Math.max(0, Math.floor(secs || 0));
  const m = Math.floor(secs / 60), s = secs % 60;
  return `${m}:${String(s).padStart(2,'0')}`;
}

function previewTick() {
  if (!ytPlayer || !ytPlayer.getDuration) return;
  const dur = ytPlayer.getDuration() || 0;
  const cur = ytPlayer.getCurrentTime() || 0;
  document.getElementById('preview-time').textContent = `${fmtTime(cur)} / ${fmtTime(dur)}`;
  if (!previewSeeking && dur > 0) {
    document.getElementById('preview-seek').value = Math.round((cur / dur) * 1000);
  }
}

function previewSeekCommit(val) {
  previewSeeking = false;
  if (!ytPlayer || !ytPlayer.getDuration) return;
  const dur = ytPlayer.getDuration() || 0;
  ytPlayer.seekTo((val / 1000) * dur, true);
}

function previewJump(fraction) {
  if (!ytPlayer || !ytPlayer.getDuration) return;
  const dur = ytPlayer.getDuration() || 0;
  ytPlayer.seekTo(dur * fraction, true);
}

function stopPreview() {
  if (ytPlayer && ytPlayer.stopVideo) ytPlayer.stopVideo();
  clearInterval(previewTimer);
  document.querySelectorAll('.preview-btn').forEach(b => b.textContent = '▶ Preview');
  document.getElementById('preview-bar').classList.remove('show');
  CSV.previewing = null;
}

function togglePreview(videoId, btnEl) {
  document.querySelectorAll('.preview-btn').forEach(b => { if (b !== btnEl) b.textContent = '▶ Preview'; });
  if (CSV.previewing === videoId) { stopPreview(); return; }
  if (!ytReady) { toast('Player still loading, try again in a second', 'info'); return; }
  ytPlayer.loadVideoById(videoId);
  CSV.previewing = videoId;
  btnEl.textContent = '■ Stop';
  document.getElementById('preview-bar-label').textContent = btnEl.closest('div').querySelector('span').textContent;
  document.getElementById('preview-bar').classList.add('show');
  document.getElementById('preview-seek').value = 0;
  clearInterval(previewTimer);
  previewTimer = setInterval(previewTick, 400);
}

function syncFastMatchToggle(checked) {
  S.settings.csvFastMatch = checked;
  const a = document.getElementById('csv-fast-toggle');
  const b = document.getElementById('csv-fast-toggle-setup');
  if (a) a.checked = checked;
  if (b) b.checked = checked;
}

async function searchCsvSong(csvId, opts = {}) {
  const song = CSV.songs.find(s => s.csv_id === csvId);
  if (!song) return;
  const box = document.getElementById(`csv-match-${csvId}`);
  if (box) box.innerHTML = `<span style="font-size:11px;color:var(--muted)">Searching…</span>`;
  const q = `${song.title} ${song.artist}`;
  const results = await api(`/api/youtube/search?q=${encodeURIComponent(q)}`);
  if (results.error) { if (box) box.innerHTML = `<span style="font-size:11px;color:var(--red)">${esc(results.error)}</span>`; return 'error'; }
  CSV.lastResults[csvId] = results;
  const fast = opts.fast ?? S.settings.csvFastMatch;
  const top = results[0];
  if (fast && top && top.score >= CSV_CONFIDENCE_THRESHOLD) {
    await pickCsvMatch(csvId, 0, { silent: opts.silent });
    return 'auto';
  }
  CSV.lastLowConf[csvId] = !!fast;
  if (!box) return 'low_confidence';
  box.innerHTML = renderCsvMatchOptions(csvId, results, { lowConfidence: fast });
  return 'low_confidence';
}

async function fastMatchAll() {
  const pending = CSV.songs.filter(s => !CSV.matched[s.csv_id]);
  if (!pending.length) { toast('Nothing to match', 'info'); return; }
  const btn    = document.getElementById('csv-matchall-btn');
  const status = document.getElementById('csv-matchall-status');
  const text   = document.getElementById('csv-matchall-text');
  btn.disabled = true;
  btn.textContent = 'Matching…';
  status.classList.remove('hidden');
  let auto = 0, low = 0, done = 0;
  for (const s of pending) {
    done++;
    text.textContent = `Auto-matching in background — ${done} of ${pending.length} (${s.title})`;
    const outcome = await searchCsvSong(s.csv_id, { fast: true, silent: true });
    if (outcome === 'auto') auto++;
    else if (outcome === 'low_confidence') low++;
    renderCsvResults();
  }
  btn.disabled = false;
  btn.textContent = '⚡ Match All';
  status.classList.add('hidden');
  updateSelBar();
  updateCustomSongsPanel();
  toast(`Auto-matched ${auto} · ${low} need manual review`, 'info');
}

async function pickCsvMatch(csvId, idx, opts = {}) {
  const song   = CSV.songs.find(s => s.csv_id === csvId);
  const result = (CSV.lastResults[csvId] || [])[idx];
  if (!song || !result) return;
  CSV.matched[csvId] = { id: result.id, title: result.title };
  await api('/api/csv/match', { method: 'POST', body: { video_id: result.id, title: song.title, artist: song.artist } });
  S.songs.cache[result.id] = { id: result.id, title: song.title, artist: song.artist, thumbnailUrl: result.thumbnail || '' };
  S.songs.selected.add(result.id);
  if (!opts.silent) {
    renderCsvResults();
    updateSelBar();
    updateCustomSongsPanel();
    toast('Matched — added to selection', 'success');
  }
}

function renderPagination() {
  const { page, pages, total, perPage } = S.songs;
  const start = (page - 1) * perPage + 1;
  const end   = Math.min(page * perPage, total);
  const el = document.getElementById('pagination');
  let html = `<span>${start}–${end} of ${total.toLocaleString()}</span>`;
  html += `<button class="page-btn" onclick="goPage(${page-1})" ${page<=1?'disabled':''}>‹ Prev</button>`;
  // Show pages around current
  const lo = Math.max(1, page - 2), hi = Math.min(pages, page + 2);
  if (lo > 1) html += `<button class="page-btn" onclick="goPage(1)">1</button>${lo>2?'<span>…</span>':''}`;
  for (let i = lo; i <= hi; i++)
    html += `<button class="page-btn ${i===page?'current':''}" onclick="goPage(${i})">${i}</button>`;
  if (hi < pages) html += `${hi<pages-1?'<span>…</span>':''}<button class="page-btn" onclick="goPage(${pages})">${pages}</button>`;
  html += `<button class="page-btn" onclick="goPage(${page+1})" ${page>=pages?'disabled':''}>Next ›</button>`;
  el.innerHTML = html;
}

function goPage(n) {
  S.songs.page = n;
  loadSongs();
  window.scrollTo(0, 0);
}

// ═══════════════════════════════════════════════════════════════════════════
// SYNC VIEW
// ═══════════════════════════════════════════════════════════════════════════
function setSyncMode(mode) {
  S.sync.mode = mode;
  document.querySelectorAll('.mode-tab').forEach((t,i) => {
    t.classList.toggle('active', ['map','new','custom'][i] === mode);
  });
  document.querySelectorAll('.sync-panel').forEach((p,i) => {
    p.style.display = ['map','new','custom'][i] === mode ? '' : 'none';
  });
  if (mode === 'custom') updateCustomSongsPanel();
}

function refreshSyncUI() {
  refreshMappingsUI();
  refreshNewPlaylistsUI();
  if (S.sync.mode === 'custom') updateCustomSongsPanel();
}

function refreshMappingsUI() {
  if (!S.sync.mappings.length && S.backup.playlists.length) {
    addMapping();
  }
  renderMappings();
}

function addMapping() {
  S.sync.mappings.push({ backup_id: '', yt_id: '' });
  renderMappings();
}

function removeMapping(i) {
  S.sync.mappings.splice(i, 1);
  renderMappings();
}

function renderMappings() {
  const el = document.getElementById('mappings-list');
  if (!S.backup.playlists.length) {
    el.innerHTML = '<p style="color:var(--muted);font-size:12px">Load backup first.</p>';
    return;
  }
  const backupOpts = S.backup.playlists.map(p =>
    `<option value="${p.id}">${esc(p.name)} (${p.song_count})</option>`).join('');
  const ytOpts = S.yt.playlists.map(p =>
    `<option value="${p.id}">${esc(p.name)} (${p.song_count})</option>`).join('');

  el.innerHTML = S.sync.mappings.map((m, i) => `
    <div class="map-row">
      <select class="select" style="width:100%" onchange="S.sync.mappings[${i}].backup_id=this.value">
        <option value="">— backup playlist —</option>${backupOpts}
      </select>
      <div class="map-arrow">→</div>
      <div class="flex" style="gap:6px;flex:1">
        <select class="select" style="flex:1" onchange="S.sync.mappings[${i}].yt_id=this.value">
          <option value="">— youtube playlist —</option>${ytOpts}
        </select>
        <button class="btn btn-danger btn-sm" onclick="removeMapping(${i})">✕</button>
      </div>
    </div>
  `).join('');

  // Restore values
  const rows = el.querySelectorAll('.map-row');
  S.sync.mappings.forEach((m, i) => {
    if (m.backup_id) rows[i].querySelectorAll('select')[0].value = m.backup_id;
    if (m.yt_id)     rows[i].querySelectorAll('select')[1].value = m.yt_id;
  });
}

function refreshNewPlaylistsUI() {
  const el = document.getElementById('new-backup-list');
  if (!S.backup.playlists.length) {
    el.innerHTML = '<div class="card-body" style="color:var(--muted)">Load backup first.</div>';
    return;
  }
  el.innerHTML = S.backup.playlists.map(p => `
    <div class="pl-row ${S.sync.selectedNewPlaylists.has(p.id) ? 'selected' : ''}"
         onclick="toggleNewPlaylist('${p.id}',this)">
      <input type="checkbox" ${S.sync.selectedNewPlaylists.has(p.id) ? 'checked' : ''}
             onclick="event.stopPropagation();toggleNewPlaylist('${p.id}',this.closest('.pl-row'))">
      <div class="pl-name">${esc(p.name)}</div>
      <div class="pl-count mono">${p.song_count.toLocaleString()}</div>
    </div>
  `).join('');
}

function toggleNewPlaylist(id, row) {
  if (S.sync.selectedNewPlaylists.has(id)) {
    S.sync.selectedNewPlaylists.delete(id);
    row.classList.remove('selected');
  } else {
    S.sync.selectedNewPlaylists.add(id);
    row.classList.add('selected');
  }
  row.querySelector('input[type=checkbox]').checked = S.sync.selectedNewPlaylists.has(id);
}

function updateCustomSongsPanel() {
  const ids = [...S.songs.selected];
  document.getElementById('custom-sel-count').textContent = `${ids.length} song${ids.length!==1?'s':''}`;
  const el = document.getElementById('custom-songs-list');
  if (!ids.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:12px">No songs selected yet.</div>';
    return;
  }
  el.innerHTML = ids.slice(0, 150).map(id => {
    const s = S.songs.cache[id];
    const label = s ? `<b>${esc(s.title)}</b>${s.artist ? ' <span style="color:var(--muted)">· '+esc(s.artist)+'</span>' : ''}` : `<span style="color:var(--dim)">${id}</span>`;
    return `<div style="font-size:12px;padding:5px 0;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px">
      ${s && s.thumbnailUrl ? `<img style="width:28px;height:28px;border-radius:3px;object-fit:cover;flex-shrink:0" src="${esc(s.thumbnailUrl)}" onerror="this.style.display='none'">` : '<div style="width:28px;height:28px;border-radius:3px;background:var(--bg3);flex-shrink:0"></div>'}
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${label}</span>
      <button style="background:none;border:none;color:var(--dim);cursor:pointer;font-size:14px;padding:0 4px;flex-shrink:0" onclick="removeSel('${id}')" title="Remove">×</button>
    </div>`;
  }).join('') + (ids.length > 150 ? `<div style="color:var(--muted);font-size:11px;padding:6px 0">… and ${ids.length-150} more</div>` : '');
}

function setCustomDestMode(mode) {
  document.getElementById('custom-dest-existing').style.display = mode === 'existing' ? '' : 'none';
  document.getElementById('custom-dest-new').style.display = mode === 'new' ? '' : 'none';
}

function goToCustomSync() {
  showView('sync');
  setSyncMode('custom');
}

async function startSync(mode) {
  let body;

  if (mode === 'map') {
    const validMappings = S.sync.mappings.filter(m => m.backup_id && m.yt_id);
    if (!validMappings.length) { toast('Add at least one complete mapping', 'error'); return; }
    body = { mode: 'map_existing', mappings: validMappings.map(m => ({ backup_id: m.backup_id, yt_id: m.yt_id })) };
  } else if (mode === 'new') {
    const ids = [...S.sync.selectedNewPlaylists];
    if (!ids.length) { toast('Select at least one playlist', 'error'); return; }
    body = { mode: 'create_new', playlist_ids: ids,
             privacy: document.getElementById('new-privacy').value };
  } else if (mode === 'custom') {
    const ids = [...S.songs.selected];
    if (!ids.length) { toast('No songs selected', 'error'); return; }
    const destMode = document.querySelector('input[name="custom-dest-mode"]:checked').value;
    if (destMode === 'new') {
      const name = document.getElementById('custom-new-playlist-name').value.trim();
      if (!name) { toast('Enter a name for the new playlist', 'error'); return; }
      body = { mode: 'custom_songs', song_ids: ids,
               new_playlist_name: name,
               privacy: document.getElementById('custom-new-privacy').value };
    } else {
      const ytId = document.getElementById('custom-yt-playlist').value;
      if (!ytId) { toast('Select a YouTube playlist', 'error'); return; }
      body = { mode: 'custom_songs', song_ids: ids, yt_playlist_id: ytId };
    }
  }

  if (!S.yt.connected) { toast('Connect your YouTube account first', 'error'); return; }

  const r = await api('/api/sync/start', { method: 'POST', body });
  if (r.error) { toast(r.error, 'error'); return; }
  toast(`Sync job started (ID: ${r.job_id})`, 'success');
  showView('progress');
  refreshJobs();
  pollJobs();
}

// ═══════════════════════════════════════════════════════════════════════════
// PROGRESS VIEW
// ═══════════════════════════════════════════════════════════════════════════
let pollTimer;
async function refreshJobs() {
  const jobs = await api('/api/jobs');
  S.jobs = jobs;
  renderJobs(jobs);
}

function renderJobs(jobs) {
  const el = document.getElementById('jobs-container');
  if (!jobs.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:13px;padding:20px 0">No sync jobs yet.</div>';
    return;
  }
  // Newest first
  el.innerHTML = [...jobs].reverse().map(j => {
    const pct = j.total > 0 ? Math.round(((j.completed + j.failed) / j.total) * 100) : 0;
    const modeLabel = { map_existing:'Map Existing', create_new:'Create New', custom_songs:'Custom Songs' }[j.mode] || j.mode;
    const statusColor = {
      running: 'badge-green', completed: 'badge-green', error: 'badge-red',
      cancelled: 'badge-dim', quota_exceeded: 'badge-amber', paused: 'badge-amber', pending: 'badge-dim'
    }[j.status] || 'badge-dim';
    const barColor = j.status === 'error' ? 'error' : j.status === 'quota_exceeded' ? 'amber' : '';

    return `
    <div class="job-card">
      <div class="job-header">
        <div>
          <div class="job-title">${modeLabel}</div>
          <div class="job-meta">
            Job ${j.id} · Started ${j.started_at ? new Date(j.started_at*1000).toLocaleTimeString() : '—'}
          </div>
        </div>
        <div class="flex" style="gap:8px">
          <span class="badge ${statusColor}">${j.status.replace('_',' ')}</span>
          ${j.status === 'running'
            ? `<button class="btn btn-sm" style="background:rgba(255,170,51,.15);color:var(--amber);border:1px solid rgba(255,170,51,.3)" onclick="pauseJob('${j.id}')">⏸ Pause</button>
               <button class="btn btn-danger btn-sm" onclick="cancelJob('${j.id}')">✕ Cancel</button>`
            : ''}
          ${['paused','quota_exceeded','error','cancelled'].includes(j.status) && j.has_cfg
            ? `<button class="btn btn-sm" style="background:var(--amber);color:#000" onclick="resumeJob('${j.id}')">▶ Resume</button>`
            : ''}
        </div>
      </div>

      <div class="progress-bar-wrap">
        <div class="progress-bar ${barColor}" style="width:${pct}%"></div>
      </div>

      <div class="job-stats">
        <span class="text-green">✓ ${j.completed.toLocaleString()} added</span>
        ${(j.skipped_existing||0) > 0 ? `<span class="text-muted">⏭ ${j.skipped_existing.toLocaleString()} already there</span>` : ''}
        ${j.failed > 0 ? `<span class="text-red">✗ ${j.failed} failed</span>` : ''}
        <span class="text-muted">/ ${j.total.toLocaleString()} total</span>
        ${j.status === 'running'
          ? `<span class="text-amber">ETA ${j.eta_str || '--:--'}</span>
             <span class="text-muted">${j.rate || 0} songs/min</span>`
          : ''}
        <span style="margin-left:auto;font-size:11px;color:var(--dim)">${pct}%</span>
      </div>

      ${j.last_song && j.status === 'running'
        ? `<div style="font-size:11px;color:var(--muted);margin-top:8px;font-style:italic">
             ▶ ${esc(j.last_song)}
           </div>`
        : ''}

      ${j.current_action
        ? `<div style="font-size:11px;color:var(--blue);margin-top:4px">${esc(j.current_action)}</div>`
        : ''}

      ${j.error
        ? `<div style="font-size:12px;color:var(--red);margin-top:8px;padding:8px;background:rgba(255,68,102,.08);border-radius:4px">
             ${esc(j.error)}
           </div>`
        : ''}

      ${j.failed_songs && j.failed_songs.length
        ? `<details style="margin-top:10px">
             <summary style="font-family:var(--mono);font-size:11px;color:var(--muted);cursor:pointer">
               ${j.failed_songs.length} failed items
             </summary>
             <div style="margin-top:8px;max-height:120px;overflow-y:auto">
               ${j.failed_songs.slice(0,20).map(f =>
                 `<div style="font-size:11px;font-family:var(--mono);color:var(--dim);padding:2px 0">
                   ${esc(f.title||f.id)} – ${esc(f.error||'')}
                 </div>`).join('')}
             </div>
           </details>`
        : ''}
    </div>`;
  }).join('');
}

function showResumeBanner(jobs) {
  const el = document.getElementById('resume-banner');
  if (!jobs.length) { el.classList.add('hidden'); return; }
  el.classList.remove('hidden');
  const quota = jobs.filter(j => j.status === 'quota_exceeded');
  const paused = jobs.filter(j => j.status === 'paused');
  let msg = [];
  if (quota.length)  msg.push(`${quota.length} job(s) hit YouTube's daily quota`);
  if (paused.length) msg.push(`${paused.length} job(s) were interrupted`);
  document.getElementById('resume-banner-msg').textContent =
    msg.join(' · ') + ' — click Resume to continue from where they stopped.';
  // Store IDs for resume-all
  el.dataset.jobIds = JSON.stringify(jobs.map(j => j.id));
}

function dismissResumeBanner() {
  document.getElementById('resume-banner').classList.add('hidden');
}

async function resumeJob(id) {
  const r = await api(`/api/sync/${id}/resume`, { method: 'POST' });
  if (r.error) { toast(r.error, 'error'); return; }
  toast('Resumed!', 'success');
  refreshJobs();
  pollJobs();
}

async function resumeAllJobs() {
  const el  = document.getElementById('resume-banner');
  const ids = JSON.parse(el.dataset.jobIds || '[]');
  for (const id of ids) {
    await api(`/api/sync/${id}/resume`, { method: 'POST' });
    await new Promise(r => setTimeout(r, 300));
  }
  toast(`Resumed ${ids.length} job(s)`, 'success');
  dismissResumeBanner();
  refreshJobs();
  pollJobs();
}

async function pauseJob(id) {
  const r = await api(`/api/sync/${id}/pause`, { method: 'POST' });
  if (r.error) { toast(r.error, 'error'); return; }
  toast('Pausing after current song…', 'info');
  setTimeout(refreshJobs, 1500);
}

async function cancelJob(id) {
  await api(`/api/sync/${id}/cancel`, { method: 'POST' });
  toast('Cancellation requested', 'info');
  setTimeout(refreshJobs, 1000);
}

async function pauseJob(id) {
  const r = await api(`/api/sync/${id}/pause`, { method: 'POST' });
  if (r.error) { toast(r.error, 'error'); return; }
  toast('Pausing after current song…', 'info');
  setTimeout(refreshJobs, 1500);
}

// ═══════════════════════════════════════════════════════════════════════════
// SYNC PREVIEW
// ═══════════════════════════════════════════════════════════════════════════
async function previewSync(mode) {
  let body;
  if (mode === 'map') {
    const validMappings = S.sync.mappings.filter(m => m.backup_id && m.yt_id);
    if (!validMappings.length) { toast('Add at least one complete mapping first', 'error'); return; }
    body = { mode: 'map_existing', mappings: validMappings.map(m => ({ backup_id: m.backup_id, yt_id: m.yt_id })) };
  } else if (mode === 'new') {
    const ids = [...S.sync.selectedNewPlaylists];
    if (!ids.length) { toast('Select at least one playlist first', 'error'); return; }
    body = { mode: 'create_new', playlist_ids: ids, privacy: 'private' };
  } else if (mode === 'custom') {
    const ids = [...S.songs.selected];
    const ytId = document.getElementById('custom-yt-playlist').value;
    if (!ids.length) { toast('No songs selected', 'error'); return; }
    if (!ytId)       { toast('Select a YouTube playlist first', 'error'); return; }
    body = { mode: 'custom_songs', song_ids: ids, yt_playlist_id: ytId };
  }

  const el = document.getElementById(`preview-${mode}`);
  el.style.display = '';
  el.innerHTML = `
    <div class="sync-preview">
      <div class="sync-preview-header">
        <span style="color:var(--muted)">PREVIEW — fetching songs from YouTube to find new ones…</span>
        <span style="color:var(--dim)">This may take a few seconds</span>
      </div>
      <div style="padding:20px;color:var(--muted);font-size:12px;font-family:var(--mono)">Loading…</div>
    </div>`;

  const r = await api('/api/sync/preview', { method: 'POST', body });

  if (r.error) {
    el.innerHTML = `
      <div class="sync-preview">
        <div class="sync-preview-header" style="color:var(--red)">Preview failed: ${esc(r.error)}</div>
      </div>`;
    return;
  }

  const skipped = r.skipped_existing || 0;
  const songs   = r.songs || [];
  const total   = r.total_new || songs.length;

  el.innerHTML = `
    <div class="sync-preview">
      <div class="sync-preview-header">
        <span>
          <span class="text-green">▶ ${total.toLocaleString()} new songs</span> will be added
          ${skipped > 0 ? `&nbsp;·&nbsp;<span class="text-muted">⏭ ${skipped.toLocaleString()} already on YouTube (will be skipped)</span>` : ''}
        </span>
        <button onclick="this.closest('.sync-preview').remove(); document.getElementById('preview-${mode}').style.display='none'" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:16px">✕</button>
      </div>
      <div class="sync-preview-body">
        ${songs.length === 0
          ? '<div style="padding:16px;color:var(--muted);font-size:12px">All songs are already on YouTube — nothing new to add!</div>'
          : songs.map(s => `
          <div class="pl-row" style="cursor:default">
            <div style="flex:1;min-width:0">
              <div style="font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(s.title||s.id)}</div>
              ${s.artist ? `<div style="font-size:11px;color:var(--muted);font-family:var(--mono)">${esc(s.artist)}</div>` : ''}
            </div>
          </div>`).join('')}
        ${total > songs.length ? `<div style="padding:10px 14px;font-size:11px;color:var(--dim);font-family:var(--mono)">… and ${(total - songs.length).toLocaleString()} more</div>` : ''}
      </div>
    </div>`;
}

function pollJobs() {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const jobs = await api('/api/jobs');
    const anyRunning = jobs.some(j => j.status === 'running' || j.status === 'pending');
    S.jobs = jobs;
    // Only re-render if on progress tab
    if (document.getElementById('view-progress').classList.contains('active')) {
      renderJobs(jobs);
    }
    // Update nav badge
    const runCount = jobs.filter(j => j.status === 'running').length;
    document.getElementById('nav-progress').textContent =
      runCount > 0 ? `Progress (${runCount})` : 'Progress';
    if (!anyRunning) clearInterval(pollTimer);
  }, 2000);
}

// ═══════════════════════════════════════════════════════════════════════════
// UTILS
// ═══════════════════════════════════════════════════════════════════════════
function esc(s) {
  return String(s||'')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ═══════════════════════════════════════════════════════════════════════════
// BOOT
// ═══════════════════════════════════════════════════════════════════════════
init();
</script>
</body>
</html>"""

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"""
╔══════════════════════════════════════════╗
║       OpenTune Sync  v1.0                ║
╚══════════════════════════════════════════╝

  Server:  http://localhost:{port}
  Config:  {APP_DIR}

  Opening browser...
""")
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # Allow http for localhost
    webbrowser.open(f"http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)