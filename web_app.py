"""
Shazam2Spotify — Web Interface
Run with: python web_app.py
Then open: http://127.0.0.1:5000
Press Ctrl+C to stop.
"""

import csv
import io
import json
import os
import queue
import threading
import time
import webbrowser

from flask import (
    Flask, Response, jsonify, redirect, render_template,
    request, stream_with_context, url_for
)
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# Clear any spotipy environment variables that could override config.json.
# spotipy falls back to these env vars when the passed value is empty/None,
# which caused the wrong client_id and redirect_uri to be used.
for _env in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI",
             "SPOTIPY_CLIENT_USERNAME"):
    os.environ.pop(_env, None)

app = Flask(__name__)
app.secret_key = "shazam2spotify-static-key-2024"   # static so sessions survive restarts

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE   = os.path.join(BASE_DIR, "config.json")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "library")
CACHE_FILE    = os.path.join(BASE_DIR, ".cache")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ── Global transfer state ─────────────────────────────────────────────────────
transfer_queue   = queue.Queue()
transfer_running = False
transfer_thread  = None
shutdown_event   = threading.Event()


# ── Config ────────────────────────────────────────────────────────────────────

DEFAULTS = {
    "client_id":         "",
    "client_secret":     "",
    "redirect_uri":      "http://127.0.0.1:5000/callback",
    "playlist_name":     "Shazam2Spotify",
    "open_browser":      True,
    "public_playlist":   True,
    "skip_duplicates":   True,
    "remove_duplicates": False,
    "sync_mode":         True,
    "delay_ms":          500,
}


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            cfg.update(saved)
        except Exception:
            pass
    return cfg


def write_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ── Spotify helpers ───────────────────────────────────────────────────────────

def make_auth_manager(cfg):
    return SpotifyOAuth(
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        redirect_uri=cfg["redirect_uri"],
        scope="playlist-read-private playlist-read-collaborative playlist-modify-public playlist-modify-private",
        cache_path=CACHE_FILE,
        open_browser=False,
    )


def make_sp(cfg):
    # If a pre-fetched access token was passed (set by /start_transfer in the main thread),
    # use it directly so the worker thread never touches SpotifyOAuth or the .cache file.
    # IMPORTANT: pass requests_session=False so spotipy creates a brand-new requests.Session
    # inside the worker thread rather than inheriting a connection pool from the main thread.
    # Inheriting the main thread's connection pool causes urllib3 to deadlock on SSL.
    if cfg.get("_access_token"):
        return spotipy.Spotify(auth=cfg["_access_token"], requests_timeout=10, retries=3,
                               requests_session=False)
    return spotipy.Spotify(auth_manager=make_auth_manager(cfg), requests_timeout=10, retries=3,
                           requests_session=False)


def get_all_playlist_track_ids(sp, playlist_id):
    # Use /items endpoint (replaces deprecated /tracks — Spotify Feb 2026 API change)
    ids = set()
    offset = 0
    while True:
        results = sp._get(
            f"playlists/{playlist_id}/items",
            limit=100, offset=offset
        )
        for item in results.get("items", []):
            # Feb 2026: field renamed from 'track' to 'item'
            track = item.get("track") or item.get("item") if item else None
            if track and track.get("id"):
                ids.add(track["id"])
        if results.get("next"):
            offset += 100
        else:
            break
    return ids


def find_existing_playlist(sp, user_id, name):
    # Direct call to /v1/me/playlists — works on all spotipy versions
    # When multiple playlists share the same name (from failed runs), pick the one
    # with the most tracks so we always reuse the populated playlist.
    matches = []
    offset = 0
    while True:
        results = sp._get("me/playlists", limit=50, offset=offset)
        for pl in (results.get("items") or []):
            if pl and pl.get("name") == name:
                matches.append(pl)
        if results.get("next"):
            offset += 50
        else:
            break
    if not matches:
        return None
    # Pick the playlist with the highest track count
    # Feb 2026: 'tracks' renamed to 'items' in playlist objects
    def _track_count(p):
        obj = p.get("items") or p.get("tracks") or {}
        return obj.get("total", 0)
    return max(matches, key=_track_count)


def remove_playlist_duplicates(sp, playlist_id):
    # Use /items endpoint (replaces deprecated /tracks — Spotify Feb 2026 API change)
    items = []
    offset = 0
    while True:
        results = sp._get(f"playlists/{playlist_id}/items", limit=100, offset=offset)
        for item in results.get("items", []):
            # Feb 2026: field renamed from 'track' to 'item'
            track = item.get("track") or item.get("item") if item else None
            if track and track.get("id"):
                items.append({"id": track["id"], "uri": track["uri"]})
        if results.get("next"):
            offset += 100
        else:
            break

    seen = set()
    uri_positions = {}
    for pos, item in enumerate(items):
        tid = item["id"]
        if tid in seen:
            uri_positions.setdefault(item["uri"], []).append(pos)
        else:
            seen.add(tid)

    removed = 0
    for uri, positions in uri_positions.items():
        for pos in sorted(positions, reverse=True):
            # Use /items endpoint for DELETE too
            sp._delete(
                f"playlists/{playlist_id}/items",
                payload={"items": [{"uri": uri, "positions": [pos]}]}
            )
            removed += 1
            time.sleep(0.2)
    return removed


# ── CSV parser ────────────────────────────────────────────────────────────────

def parse_shazam_csv(file_content):
    songs = []
    reader = csv.reader(io.StringIO(file_content))
    header_done = False
    for row in reader:
        if not header_done:
            if row and row[0].strip().upper() == "SHAZAM LIBRARY":
                continue
            if len(row) >= 4 and row[0].strip().lower() == "index":
                header_done = True
                continue
            header_done = True
            continue
        if len(row) >= 4:
            title  = row[2].strip().strip('"')
            artist = row[3].strip().strip('"')
            if title and artist:
                songs.append((title, artist))
    return songs


# ── Transfer worker ───────────────────────────────────────────────────────────

def run_transfer(cfg, songs):
    global transfer_running

    def emit(event, data):
        transfer_queue.put({"event": event, "data": data})

    print(f"[S2S] run_transfer started: {len(songs)} songs", flush=True)
    print(f"[S2S] _access_token present: {bool(cfg.get('_access_token'))}", flush=True)
    print(f"[S2S] _user_id: {cfg.get('_user_id')}", flush=True)
    playlist_url = ""
    try:
        # Use pre-fetched token and user info from the main thread (no blocking calls here)
        print("[S2S] calling emit status...", flush=True)
        emit("status", {"msg": "Connecting to Spotify...", "type": "info"})
        print("[S2S] emit done, calling make_sp...", flush=True)
        sp = make_sp(cfg)
        print("[S2S] make_sp done", flush=True)
        user_id      = cfg.get("_user_id", "")
        display_name = cfg.get("_user_display_name", "Spotify User")
        emit("status", {"msg": f"Logged in as {display_name}", "type": "success"})
        print(f"[S2S] logged in as {display_name}, user_id={user_id}", flush=True)

        playlist_name    = cfg.get("playlist_name", "Shazam2Spotify") or "Shazam2Spotify"
        selected_pl_id   = cfg.get("selected_playlist_id", "")   # set when user picks from dropdown
        selected_pl_name = cfg.get("selected_playlist_name", "") # display name for the chosen playlist
        public           = cfg.get("public_playlist", True)
        sync_mode        = cfg.get("sync_mode", True)
        remove_dupes     = cfg.get("remove_duplicates", False)
        skip_dupes       = cfg.get("skip_duplicates", True)
        delay            = max(0.1, cfg.get("delay_ms", 500) / 1000.0)

        is_new_playlist = False

        if selected_pl_id:
            # User picked an existing playlist from the dropdown
            playlist_id  = selected_pl_id
            display_name = selected_pl_name or playlist_id
            playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"
            emit("status", {"msg": f"Using selected playlist '{display_name}'", "type": "info"})
        else:
            # User typed a new playlist name — search for it first to avoid duplicates
            emit("status", {"msg": f"Searching for existing playlist '{playlist_name}'...", "type": "info"})
            existing = find_existing_playlist(sp, user_id, playlist_name)
            if existing:
                playlist_id  = existing["id"]
                playlist_url = existing["external_urls"]["spotify"]
                emit("status", {"msg": f"Found '{playlist_name}' — will add songs to it", "type": "info"})
            else:
                playlist = sp._post(
                    "me/playlists",
                    payload={
                        "name": playlist_name,
                        "public": public,
                        "description": "Created by Shazam2Spotify — github.com/dairyking98/Shazam2Spotify",
                    }
                )
                playlist_id  = playlist["id"]
                playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"
                is_new_playlist = True
                emit("status", {"msg": f"Created new playlist '{playlist_name}'", "type": "success"})
        emit("playlist", {"url": playlist_url})

        # Fetch existing tracks to skip already-added songs.
        print(f"[S2S] is_new_playlist={is_new_playlist} sync_mode={sync_mode} playlist_id={playlist_id}", flush=True)
        if is_new_playlist:
            existing_ids = set()
            emit("status", {"msg": "New playlist — no duplicate check needed", "type": "info"})
        elif sync_mode:
            emit("status", {"msg": "Checking existing playlist tracks...", "type": "info"})
            print("[S2S] calling get_all_playlist_track_ids...", flush=True)
            existing_ids = get_all_playlist_track_ids(sp, playlist_id)
            print(f"[S2S] existing_ids count: {len(existing_ids)}", flush=True)
            emit("status", {"msg": f"{len(existing_ids)} tracks already in playlist — will skip these", "type": "info"})
        else:
            existing_ids = set()

        total       = len(songs)
        session_ids = set()
        added = skipped = csv_dupes = 0
        not_found = []
        all_results = []  # full log: (original_title, original_artist, matched_title, matched_artist, status)
        print(f"[S2S] starting song loop: {total} songs", flush=True)

        for i, (title, artist) in enumerate(songs, 1):
            if shutdown_event.is_set():
                break
            try:
                results = sp.search(q=f"track:{title} artist:{artist}", type="track", limit=1)  # noqa: limit=1 is within the new max of 10
                tracks  = results["tracks"]["items"]
                if tracks:
                    tid     = tracks[0]["id"]
                    tname   = tracks[0]["name"]
                    tartist = tracks[0]["artists"][0]["name"]
                    if tid in existing_ids:
                        skipped += 1
                        all_results.append((title, artist, tname, tartist, "Already in playlist"))
                        emit("song", {"i": i, "total": total, "status": "skipped",
                                      "title": tname, "artist": tartist, "msg": "Already in playlist"})
                        # No delay needed for skipped songs — no API write call was made
                        continue
                    elif skip_dupes and tid in session_ids:
                        csv_dupes += 1
                        all_results.append((title, artist, tname, tartist, "Duplicate in CSV"))
                        emit("song", {"i": i, "total": total, "status": "duplicate",
                                      "title": tname, "artist": tartist, "msg": "Duplicate in CSV"})
                        continue
                    else:
                        # Use /items endpoint (replaces deprecated /tracks — Spotify Feb 2026 API change)
                        sp._post(f"playlists/{playlist_id}/items", payload={"uris": [f"spotify:track:{tid}"]})
                        session_ids.add(tid)
                        existing_ids.add(tid)
                        added += 1
                        all_results.append((title, artist, tname, tartist, "Added"))
                        emit("song", {"i": i, "total": total, "status": "added",
                                      "title": tname, "artist": tartist, "msg": "Added"})
                else:
                    not_found.append(f"{title} — {artist}")
                    all_results.append((title, artist, "", "", "Not found on Spotify"))
                    emit("song", {"i": i, "total": total, "status": "notfound",
                                  "title": title, "artist": artist, "msg": "Not found on Spotify"})
                time.sleep(delay)
            except Exception as e:
                err_msg = str(e)
                all_results.append((title, artist, "", "", f"Error: {err_msg}"))
                emit("song", {"i": i, "total": total, "status": "error",
                              "title": title, "artist": artist, "msg": err_msg})
                # Back off longer on rate limit errors (429)
                if "429" in err_msg or "rate" in err_msg.lower():
                    time.sleep(5)
                else:
                    time.sleep(1)

        # Remove duplicates pass
        dupes_removed = 0
        if remove_dupes and not shutdown_event.is_set():
            emit("status", {"msg": "Scanning for duplicates to remove...", "type": "info"})
            try:
                dupes_removed = remove_playlist_duplicates(sp, playlist_id)
                emit("status", {"msg": f"Removed {dupes_removed} duplicate(s)", "type": "success"})
            except Exception as e:
                emit("status", {"msg": f"Duplicate removal error: {e}", "type": "error"})

        # Write full results CSV
        import csv as csv_mod
        from datetime import datetime
        report_name = f"transfer_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        report_path = os.path.join(BASE_DIR, "library", report_name)
        try:
            with open(report_path, "w", newline="", encoding="utf-8") as f:
                writer = csv_mod.writer(f)
                writer.writerow(["Shazam Title", "Shazam Artist", "Spotify Title", "Spotify Artist", "Status"])
                for row in all_results:
                    writer.writerow(row)
            emit("status", {"msg": f"Report saved: {report_name}", "type": "success"})
        except Exception as e:
            report_name = ""
            emit("status", {"msg": f"Could not save report: {e}", "type": "error"})

        emit("done", {
            "total": total, "added": added, "skipped": skipped,
            "csv_dupes": csv_dupes, "dupes_removed": dupes_removed,
            "not_found": not_found, "playlist_url": playlist_url,
            "open_browser": cfg.get("open_browser", True),
            "report_file": report_name,
        })

    except Exception as e:
        import traceback
        print(f"[S2S] TRANSFER ERROR: {e}", flush=True)
        traceback.print_exc()
        emit("error", {"msg": str(e)})
    finally:
        print("[S2S] run_transfer finished", flush=True)
        transfer_running = False


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    cfg = load_config()
    sp_authenticated = False
    if os.path.exists(CACHE_FILE) and cfg.get("client_id"):
        try:
            sp = make_sp(cfg)
            user = sp.current_user()
            sp_authenticated = bool(user)
        except Exception:
            sp_authenticated = False
    return render_template("index.html", cfg=cfg, sp_authenticated=sp_authenticated)


@app.route("/save_config", methods=["POST"])
def save_config_route():
    data    = request.get_json() or {}
    old_cfg = load_config()
    new_id  = data.get("client_id", old_cfg["client_id"]).strip()
    new_uri = data.get("redirect_uri", old_cfg["redirect_uri"]).strip()

    # If credentials changed, wipe the stale .cache so spotipy doesn't reuse old tokens
    if new_id != old_cfg["client_id"] or new_uri != old_cfg["redirect_uri"]:
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)

    old_cfg.update({
        "client_id":         new_id,
        "client_secret":     data.get("client_secret", old_cfg["client_secret"]).strip(),
        "redirect_uri":      new_uri,
        "playlist_name":     data.get("playlist_name", old_cfg["playlist_name"]).strip() or "Shazam2Spotify",
        "open_browser":      bool(data.get("open_browser", old_cfg["open_browser"])),
        "public_playlist":   bool(data.get("public_playlist", old_cfg["public_playlist"])),
        "skip_duplicates":   bool(data.get("skip_duplicates", old_cfg["skip_duplicates"])),
        "remove_duplicates": bool(data.get("remove_duplicates", old_cfg["remove_duplicates"])),
        "sync_mode":         bool(data.get("sync_mode", old_cfg["sync_mode"])),
        "delay_ms":          int(data.get("delay_ms", old_cfg["delay_ms"])),
    })
    write_config(old_cfg)
    return jsonify({"ok": True})


@app.route("/spotify_auth")
def spotify_auth():
    cfg = load_config()
    if not cfg.get("client_id") or not cfg.get("client_secret"):
        return jsonify({"error": "Fill in Client ID and Client Secret first, then click Save."}), 400
    try:
        auth_url = make_auth_manager(cfg).get_authorize_url()
        return jsonify({"auth_url": auth_url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/callback")
def spotify_callback():
    code  = request.args.get("code")
    error = request.args.get("error")
    if error:
        return f"<h2>Spotify auth error: {error}</h2><p><a href='/'>Go back</a></p>"
    if not code:
        return "<h2>No code received.</h2><p><a href='/'>Go back</a></p>"
    cfg = load_config()
    try:
        make_auth_manager(cfg).get_access_token(code)
    except Exception as e:
        return f"<h2>Auth failed: {e}</h2><p><a href='/'>Go back</a></p>"
    return redirect(url_for("index") + "?auth=success")


@app.route("/check_auth")
def check_auth():
    cfg = load_config()
    if not os.path.exists(CACHE_FILE) or not cfg.get("client_id"):
        return jsonify({"authenticated": False})
    try:
        sp   = make_sp(cfg)
        user = sp.current_user()
        return jsonify({"authenticated": True, "name": user.get("display_name", "Unknown")})
    except Exception:
        return jsonify({"authenticated": False})


@app.route("/test_add_track")
def test_add_track():
    """Debug: try adding a known track to the first playlist found, return full Spotify response."""
    import requests as req
    cfg = load_config()
    try:
        sp   = make_sp(cfg)
        auth = make_auth_manager(cfg)
        token = auth.get_cached_token()
        access_token = token["access_token"]

        # Get first playlist
        playlists = sp._get("me/playlists", limit=1)
        if not playlists["items"]:
            return jsonify({"error": "No playlists found"})
        pl_id = playlists["items"][0]["id"]
        pl_name = playlists["items"][0]["name"]

        # Try adding a well-known track (Never Gonna Give You Up)
        # Use /items endpoint (replaces deprecated /tracks — Spotify Feb 2026 API change)
        test_uri = "spotify:track:4cOdK2wGLETKBW3PvgPWqT"
        url = f"https://api.spotify.com/v1/playlists/{pl_id}/items"
        resp = req.post(
            url,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"uris": [test_uri]}
        )
        return jsonify({
            "playlist": pl_name,
            "playlist_id": pl_id,
            "status_code": resp.status_code,
            "response": resp.json() if resp.content else "(empty)"
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/token_info")
def token_info():
    """Debug route: shows what scopes the current cached token has."""
    cfg = load_config()
    if not os.path.exists(CACHE_FILE):
        return jsonify({"error": "No .cache file found — not authenticated yet"})
    try:
        auth = make_auth_manager(cfg)
        token = auth.get_cached_token()
        if not token:
            return jsonify({"error": "No cached token"})
        return jsonify({
            "scope": token.get("scope", "(none)"),
            "expires_at": token.get("expires_at"),
            "token_type": token.get("token_type"),
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/upload_csv", methods=["POST"])
def upload_csv():
    if "csv_file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["csv_file"]
    if not f.filename.lower().endswith(".csv"):
        return jsonify({"error": "File must be a .csv"}), 400
    try:
        content = f.read().decode("utf-8", errors="replace")
        songs   = parse_shazam_csv(content)
        save_path = os.path.join(UPLOAD_FOLDER, "shazamlibrary.csv")
        with open(save_path, "w", encoding="utf-8") as out:
            out.write(content)
        return jsonify({"ok": True, "count": len(songs)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/reset_transfer", methods=["POST"])
def reset_transfer():
    """Force-reset the transfer state so a new transfer can start.
    Called automatically by the UI when the page loads or 'New Transfer' is clicked."""
    global transfer_running, transfer_queue
    transfer_running = False
    transfer_queue   = queue.Queue()
    return jsonify({"ok": True})


@app.route("/start_transfer", methods=["POST"])
def start_transfer():
    global transfer_running, transfer_thread, transfer_queue
    if transfer_running:
        return jsonify({"error": "Transfer already running"}), 400
    data      = request.get_json() or {}
    songs_raw = data.get("songs", [])
    songs     = [(s[0], s[1]) for s in songs_raw if len(s) >= 2]
    if not songs:
        return jsonify({"error": "No songs to transfer"}), 400
    cfg = load_config()
    # Override with values sent from UI
    for key in ("playlist_name", "open_browser", "public_playlist",
                "skip_duplicates", "remove_duplicates", "sync_mode", "delay_ms",
                "selected_playlist_id", "selected_playlist_name"):
        if key in data:
            cfg[key] = data[key]
    # Pre-authenticate AND pre-fetch user info in the main (request) thread.
    # The worker thread must not make any SpotifyOAuth or blocking Spotify calls
    # at startup — they can hang indefinitely due to thread/network issues.
    try:
        auth_manager = make_auth_manager(cfg)
        token_info   = auth_manager.get_cached_token()
        if not token_info:
            return jsonify({"error": "Not authenticated with Spotify — please connect first"}), 401
        if auth_manager.is_token_expired(token_info):
            token_info = auth_manager.refresh_access_token(token_info["refresh_token"])
        cfg["_access_token"] = token_info["access_token"]
        # Pre-fetch user info so the worker never calls sp.current_user()
        sp_main = make_sp(cfg)
        user_info = sp_main.current_user()
        cfg["_user_id"]           = user_info["id"]
        cfg["_user_display_name"] = user_info.get("display_name", "Unknown")
    except Exception as e:
        return jsonify({"error": f"Spotify auth failed: {e}"}), 401
    transfer_queue   = queue.Queue()
    transfer_running = True
    t = threading.Thread(target=run_transfer, args=(cfg, songs), daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/stream")
def stream():
    def generate():
        while not shutdown_event.is_set():
            try:
                item = transfer_queue.get(timeout=2)
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("event") in ("done", "error"):
                    break
            except queue.Empty:
                yield 'data: {"event":"ping"}\n\n'
    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/get_playlists")
def get_playlists():
    """Return all user playlists for the dropdown."""
    cfg = load_config()
    try:
        sp = make_sp(cfg)
        playlists = []
        offset = 0
        while True:
            results = sp._get("me/playlists", limit=50, offset=offset)
            for pl in (results.get("items") or []):
                if pl:
                    # Feb 2026 API change: 'tracks' renamed to 'items' in playlist objects.
                    # Fall back to 'tracks' for older API versions / cached responses.
                    items_obj = pl.get("items") or pl.get("tracks") or {}
                    playlists.append({
                        "id":     pl.get("id"),
                        "name":   pl.get("name"),
                        "tracks": items_obj.get("total", 0),
                        "url":    pl.get("external_urls", {}).get("spotify", ""),
                    })
            if results.get("next"):
                offset += 50
            else:
                break
        return jsonify({"playlists": playlists})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/logout")
def logout():
    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)
    return redirect(url_for("index"))


@app.route("/debug_playlist")
def debug_playlist():
    """Debug: list all playlists and show raw items structure for the one with most tracks."""
    cfg = load_config()
    try:
        sp = make_sp(cfg)
        all_pls = []
        offset = 0
        while True:
            results = sp._get("me/playlists", limit=50, offset=offset)
            for pl in (results.get("items") or []):
                if pl:
                    all_pls.append({"name": pl.get("name"), "id": pl.get("id"), "tracks": pl.get("tracks", {}).get("total", 0)})
            if results.get("next"):
                offset += 50
            else:
                break
        # Find the playlist with the most tracks named Shazam2Spotify
        s2s = [p for p in all_pls if p["name"] == "Shazam2Spotify"]
        best = max(s2s, key=lambda p: p["tracks"]) if s2s else None
        items_debug = {}
        if best:
            raw = sp._get(f"playlists/{best['id']}/items", limit=3, offset=0)
            raw_items = raw.get("items", [])
            items_debug = {
                "first_item_keys": list(raw_items[0].keys()) if raw_items else [],
                "first_track_keys": list((raw_items[0].get("track") or raw_items[0].get("item") or {}).keys()) if raw_items else [],
                "first_item_raw": raw_items[0] if raw_items else None,
            }
        return jsonify({
            "all_playlists": all_pls,
            "shazam2spotify_playlists": s2s,
            "best_match": best,
            "items_debug": items_debug,
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/download_report/<filename>")
def download_report(filename):
    """Download a transfer report CSV from the library folder."""
    from flask import send_from_directory
    # Security: only allow filenames that match the expected pattern
    import re
    if not re.match(r'^transfer_report_\d{8}_\d{6}\.csv$', filename):
        return "Invalid filename", 400
    return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=True)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Ensure config.json exists on disk before starting
    if not os.path.exists(CONFIG_FILE):
        write_config(dict(DEFAULTS))
        print(f"  Created config.json — fill in your Client ID and Secret.")

    print("\n" + "=" * 55)
    print("  Shazam2Spotify Web Interface")
    print("  Open your browser at: http://127.0.0.1:5000")
    print("  Press Ctrl+C to stop")
    print("=" * 55 + "\n")

    # Waitress does NOT support streaming responses (SSE) — it buffers the
    # entire response before sending, so the progress stream never reaches
    # the browser. Use Flask's built-in Werkzeug dev server with threading,
    # which does support streaming. Ctrl+C is handled via the KeyboardInterrupt
    # catch below.
    try:
        app.run(host="127.0.0.1", port=5000, debug=False,
                use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_event.set()
        print("\nShutting down... Bye!")
        os._exit(0)
