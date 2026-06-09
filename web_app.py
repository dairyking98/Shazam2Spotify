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
    request, url_for
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
    return spotipy.Spotify(auth_manager=make_auth_manager(cfg))


def get_all_playlist_track_ids(sp, playlist_id):
    # Use direct API call — sp.playlist_items with fields= can 403 on some app configs
    ids = set()
    offset = 0
    while True:
        results = sp._get(
            f"playlists/{playlist_id}/tracks",
            limit=100, offset=offset
        )
        for item in results.get("items", []):
            if item and item.get("track") and item["track"].get("id"):
                ids.add(item["track"]["id"])
        if results.get("next"):
            offset += 100
        else:
            break
    return ids


def find_existing_playlist(sp, user_id, name):
    # Direct call to /v1/me/playlists — works on all spotipy versions
    offset = 0
    while True:
        results = sp._get("me/playlists", limit=50, offset=offset)
        for pl in results["items"]:
            if pl["owner"]["id"] == user_id and pl["name"] == name:
                return pl
        if results.get("next"):
            offset += 50
        else:
            break
    return None


def remove_playlist_duplicates(sp, playlist_id):
    items = []
    offset = 0
    while True:
        results = sp._get(f"playlists/{playlist_id}/tracks", limit=100, offset=offset)
        for item in results.get("items", []):
            if item and item.get("track") and item["track"].get("id"):
                items.append({"id": item["track"]["id"], "uri": item["track"]["uri"]})
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
            sp._delete(
                f"playlists/{playlist_id}/tracks",
                payload={"tracks": [{"uri": uri, "positions": [pos]}]}
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

    playlist_url = ""
    try:
        emit("status", {"msg": "Connecting to Spotify...", "type": "info"})
        sp   = make_sp(cfg)
        user = sp.current_user()
        emit("status", {"msg": f"Logged in as {user['display_name']}", "type": "success"})

        playlist_name = cfg.get("playlist_name", "Shazam2Spotify") or "Shazam2Spotify"
        public        = cfg.get("public_playlist", True)
        sync_mode     = cfg.get("sync_mode", True)
        remove_dupes  = cfg.get("remove_duplicates", False)
        skip_dupes    = cfg.get("skip_duplicates", True)
        delay         = max(0.1, cfg.get("delay_ms", 500) / 1000.0)

        # Find or create playlist
        existing = find_existing_playlist(sp, user["id"], playlist_name) if sync_mode else None
        if existing:
            playlist_id  = existing["id"]
            playlist_url = existing["external_urls"]["spotify"]
            emit("status", {"msg": f"Found '{playlist_name}' — syncing new songs only", "type": "info"})
        else:
            # Use direct API call to /v1/me/playlists — works on all spotipy versions
            # (current_user_playlist_create only exists in newer spotipy builds)
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
            emit("status", {"msg": f"Created new playlist '{playlist_name}'", "type": "success"})
        emit("playlist", {"url": playlist_url})

        # Fetch existing tracks
        emit("status", {"msg": "Checking existing playlist tracks...", "type": "info"})
        existing_ids = get_all_playlist_track_ids(sp, playlist_id)
        emit("status", {"msg": f"{len(existing_ids)} tracks already in playlist", "type": "info"})

        total       = len(songs)
        session_ids = set()
        added = skipped = csv_dupes = 0
        not_found = []

        for i, (title, artist) in enumerate(songs, 1):
            if shutdown_event.is_set():
                break
            try:
                results = sp.search(q=f"track:{title} artist:{artist}", type="track", limit=1)
                tracks  = results["tracks"]["items"]
                if tracks:
                    tid     = tracks[0]["id"]
                    tname   = tracks[0]["name"]
                    tartist = tracks[0]["artists"][0]["name"]
                    if tid in existing_ids:
                        skipped += 1
                        emit("song", {"i": i, "total": total, "status": "skipped",
                                      "title": tname, "artist": tartist, "msg": "Already in playlist"})
                    elif skip_dupes and tid in session_ids:
                        csv_dupes += 1
                        emit("song", {"i": i, "total": total, "status": "duplicate",
                                      "title": tname, "artist": tartist, "msg": "Duplicate in CSV"})
                    else:
                        sp._post(f"playlists/{playlist_id}/tracks", payload={"uris": [f"spotify:track:{tid}"]})
                        session_ids.add(tid)
                        existing_ids.add(tid)
                        added += 1
                        emit("song", {"i": i, "total": total, "status": "added",
                                      "title": tname, "artist": tartist, "msg": "Added"})
                else:
                    not_found.append(f"{title} — {artist}")
                    emit("song", {"i": i, "total": total, "status": "notfound",
                                  "title": title, "artist": artist, "msg": "Not found on Spotify"})
                time.sleep(delay)
            except Exception as e:
                emit("song", {"i": i, "total": total, "status": "error",
                              "title": title, "artist": artist, "msg": str(e)})
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

        emit("done", {
            "total": total, "added": added, "skipped": skipped,
            "csv_dupes": csv_dupes, "dupes_removed": dupes_removed,
            "not_found": not_found, "playlist_url": playlist_url,
            "open_browser": cfg.get("open_browser", True),
        })

    except Exception as e:
        emit("error", {"msg": str(e)})
    finally:
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
                "skip_duplicates", "remove_duplicates", "sync_mode", "delay_ms"):
        if key in data:
            cfg[key] = data[key]
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
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/logout")
def logout():
    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)
    return redirect(url_for("index"))


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

    # Use waitress (production WSGI server) — handles Ctrl+C cleanly
    try:
        from waitress import serve
        serve(app, host="127.0.0.1", port=5000, threads=8)
    except ImportError:
        # Fallback to Flask dev server if waitress not installed
        try:
            app.run(host="127.0.0.1", port=5000, debug=False,
                    use_reloader=False, threaded=True)
        except KeyboardInterrupt:
            pass
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_event.set()
        print("\nShutting down... Bye!")
        os._exit(0)
