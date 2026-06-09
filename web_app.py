"""
Shazam2Spotify — Web Interface
Run with: python web_app.py
Then open: http://127.0.0.1:5000
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
    request, session, url_for
)
import spotipy
from spotipy.oauth2 import SpotifyOAuth

app = Flask(__name__)
app.secret_key = os.urandom(24)

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "library")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ── Global transfer state ─────────────────────────────────────────────────────
transfer_queue = queue.Queue()
transfer_running = False
transfer_thread = None


# ── Config helpers ────────────────────────────────────────────────────────────

def load_config():
    defaults = {
        "client_id": "",
        "client_secret": "",
        "redirect_uri": "http://127.0.0.1:8888/callback",
        "playlist_name": "Shazam2Spotify",
        "open_browser": True,
        "public_playlist": True,
        "skip_duplicates": True,
        "delay_ms": 500,
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                saved = json.load(f)
            defaults.update(saved)
        except Exception:
            pass
    return defaults


def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Spotify auth helpers ──────────────────────────────────────────────────────

def make_sp(cfg):
    return spotipy.Spotify(auth_manager=SpotifyOAuth(
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        redirect_uri=cfg["redirect_uri"],
        scope="playlist-modify-public playlist-modify-private",
        cache_path=os.path.join(os.path.dirname(__file__), ".cache"),
        open_browser=False,
    ))


# ── CSV parser ────────────────────────────────────────────────────────────────

def parse_shazam_csv(file_content):
    """Parse Shazam CSV export. Returns list of (title, artist) tuples."""
    songs = []
    reader = csv.reader(io.StringIO(file_content))
    header_done = False
    for row in reader:
        if not header_done:
            if row and row[0].strip().upper() in ("SHAZAM LIBRARY",):
                continue
            if len(row) >= 4 and row[0].strip().lower() == "index":
                header_done = True
                continue
            header_done = True
            continue
        if len(row) >= 4:
            title = row[2].strip().strip('"')
            artist = row[3].strip().strip('"')
            if title and artist:
                songs.append((title, artist))
    return songs


# ── Transfer worker ───────────────────────────────────────────────────────────

def run_transfer(cfg, songs):
    global transfer_running

    def emit(event, data):
        transfer_queue.put({"event": event, "data": data})

    try:
        emit("status", {"msg": "Connecting to Spotify...", "type": "info"})
        sp = make_sp(cfg)
        user = sp.current_user()
        emit("status", {"msg": f"Logged in as: {user['display_name']} ({user['id']})", "type": "success"})

        playlist_name = cfg.get("playlist_name", "Shazam2Spotify")
        public = cfg.get("public_playlist", True)
        emit("status", {"msg": f"Creating playlist '{playlist_name}'...", "type": "info"})
        playlist = sp.user_playlist_create(
            user=user["id"],
            name=playlist_name,
            public=public,
            description="Created by Shazam2Spotify — github.com/dairyking98/Shazam2Spotify",
        )
        playlist_id = playlist["id"]
        playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"
        emit("status", {"msg": f"Playlist created!", "type": "success"})
        emit("playlist", {"id": playlist_id, "url": playlist_url, "name": playlist_name})

        total = len(songs)
        added_ids = set()
        added = 0
        not_found = []
        duplicates = 0
        delay = cfg.get("delay_ms", 500) / 1000.0
        skip_dupes = cfg.get("skip_duplicates", True)

        for i, (title, artist) in enumerate(songs, 1):
            query = f"track:{title} artist:{artist}"
            try:
                results = sp.search(q=query, type="track", limit=1)
                tracks = results["tracks"]["items"]
                if tracks:
                    track_id = tracks[0]["id"]
                    track_name = tracks[0]["name"]
                    track_artist = tracks[0]["artists"][0]["name"]
                    if skip_dupes and track_id in added_ids:
                        duplicates += 1
                        emit("song", {
                            "i": i, "total": total,
                            "status": "duplicate",
                            "title": title, "artist": artist,
                            "msg": f"Duplicate skipped"
                        })
                    else:
                        sp.playlist_add_items(playlist_id, [track_id])
                        added_ids.add(track_id)
                        added += 1
                        emit("song", {
                            "i": i, "total": total,
                            "status": "added",
                            "title": track_name, "artist": track_artist,
                            "msg": "Added"
                        })
                else:
                    not_found.append(f"{title} — {artist}")
                    emit("song", {
                        "i": i, "total": total,
                        "status": "notfound",
                        "title": title, "artist": artist,
                        "msg": "Not found on Spotify"
                    })
                time.sleep(delay)
            except Exception as e:
                emit("song", {
                    "i": i, "total": total,
                    "status": "error",
                    "title": title, "artist": artist,
                    "msg": str(e)
                })
                time.sleep(1)

        emit("done", {
            "total": total,
            "added": added,
            "duplicates": duplicates,
            "not_found": not_found,
            "playlist_url": playlist_url,
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
    config_exists = os.path.exists(CONFIG_FILE) and bool(cfg.get("client_id"))
    # Check if already authenticated
    sp_authenticated = False
    cache_path = os.path.join(os.path.dirname(__file__), ".cache")
    if os.path.exists(cache_path) and cfg.get("client_id"):
        try:
            sp = make_sp(cfg)
            user = sp.current_user()
            sp_authenticated = bool(user)
        except Exception:
            sp_authenticated = False
    return render_template("index.html",
                           cfg=cfg,
                           config_exists=config_exists,
                           sp_authenticated=sp_authenticated)


@app.route("/save_config", methods=["POST"])
def save_config_route():
    data = request.get_json()
    cfg = load_config()
    cfg.update({
        "client_id": data.get("client_id", "").strip(),
        "client_secret": data.get("client_secret", "").strip(),
        "redirect_uri": data.get("redirect_uri", "http://127.0.0.1:8888/callback").strip(),
        "playlist_name": data.get("playlist_name", "Shazam2Spotify").strip(),
        "open_browser": bool(data.get("open_browser", True)),
        "public_playlist": bool(data.get("public_playlist", True)),
        "skip_duplicates": bool(data.get("skip_duplicates", True)),
        "delay_ms": int(data.get("delay_ms", 500)),
    })
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/spotify_auth")
def spotify_auth():
    cfg = load_config()
    if not cfg.get("client_id"):
        return jsonify({"error": "No credentials configured"}), 400
    auth_manager = SpotifyOAuth(
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        redirect_uri=cfg["redirect_uri"],
        scope="playlist-modify-public playlist-modify-private",
        cache_path=os.path.join(os.path.dirname(__file__), ".cache"),
        open_browser=False,
    )
    auth_url = auth_manager.get_authorize_url()
    return jsonify({"auth_url": auth_url})


@app.route("/callback")
def spotify_callback():
    code = request.args.get("code")
    cfg = load_config()
    auth_manager = SpotifyOAuth(
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        redirect_uri=cfg["redirect_uri"],
        scope="playlist-modify-public playlist-modify-private",
        cache_path=os.path.join(os.path.dirname(__file__), ".cache"),
        open_browser=False,
    )
    auth_manager.get_access_token(code)
    return redirect(url_for("index") + "?auth=success")


@app.route("/check_auth")
def check_auth():
    cfg = load_config()
    cache_path = os.path.join(os.path.dirname(__file__), ".cache")
    if not os.path.exists(cache_path) or not cfg.get("client_id"):
        return jsonify({"authenticated": False})
    try:
        sp = make_sp(cfg)
        user = sp.current_user()
        return jsonify({"authenticated": True, "name": user["display_name"], "id": user["id"]})
    except Exception:
        return jsonify({"authenticated": False})


@app.route("/upload_csv", methods=["POST"])
def upload_csv():
    if "csv_file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["csv_file"]
    if not f.filename.lower().endswith(".csv"):
        return jsonify({"error": "File must be a .csv"}), 400
    content = f.read().decode("utf-8", errors="replace")
    songs = parse_shazam_csv(content)
    # Save to library folder
    save_path = os.path.join(UPLOAD_FOLDER, "shazamlibrary.csv")
    with open(save_path, "w", encoding="utf-8") as out:
        out.write(content)
    return jsonify({"ok": True, "count": len(songs), "songs": songs[:5]})


@app.route("/start_transfer", methods=["POST"])
def start_transfer():
    global transfer_running, transfer_thread, transfer_queue
    if transfer_running:
        return jsonify({"error": "Transfer already running"}), 400

    data = request.get_json()
    songs_raw = data.get("songs", [])
    songs = [(s[0], s[1]) for s in songs_raw if len(s) >= 2]
    if not songs:
        return jsonify({"error": "No songs to transfer"}), 400

    cfg = load_config()
    # Allow per-request overrides
    if data.get("playlist_name"):
        cfg["playlist_name"] = data["playlist_name"]
    if "open_browser" in data:
        cfg["open_browser"] = data["open_browser"]
    if "public_playlist" in data:
        cfg["public_playlist"] = data["public_playlist"]
    if "skip_duplicates" in data:
        cfg["skip_duplicates"] = data["skip_duplicates"]
    if "delay_ms" in data:
        cfg["delay_ms"] = data["delay_ms"]

    transfer_queue = queue.Queue()
    transfer_running = True
    transfer_thread = threading.Thread(target=run_transfer, args=(cfg, songs), daemon=True)
    transfer_thread.start()
    return jsonify({"ok": True})


@app.route("/stream")
def stream():
    def generate():
        while True:
            try:
                item = transfer_queue.get(timeout=30)
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("event") in ("done", "error"):
                    break
            except queue.Empty:
                yield "data: {\"event\": \"ping\"}\n\n"
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/logout")
def logout():
    cache_path = os.path.join(os.path.dirname(__file__), ".cache")
    if os.path.exists(cache_path):
        os.remove(cache_path)
    return redirect(url_for("index"))


if __name__ == "__main__":
    print("\n" + "=" * 55)
    print("  Shazam2Spotify Web Interface")
    print("  Open your browser at: http://127.0.0.1:5000")
    print("=" * 55 + "\n")
    app.run(host="127.0.0.1", port=5000, debug=False)
