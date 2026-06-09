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
for _env in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET",
             "SPOTIPY_REDIRECT_URI", "SPOTIPY_CLIENT_USERNAME"):
    os.environ.pop(_env, None)

app = Flask(__name__)
app.secret_key = "shazam2spotify-static-key-2024"

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
                cfg.update(json.load(f))
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
        scope=(
            "playlist-read-private playlist-read-collaborative "
            "playlist-modify-public playlist-modify-private"
        ),
        cache_path=CACHE_FILE,
        open_browser=False,
    )


def make_sp(cfg):
    return spotipy.Spotify(auth_manager=make_auth_manager(cfg))


def make_sp_with_token(token):
    """Create a Spotify client using a raw access token and a fresh requests.Session.
    This avoids the Windows urllib3 connection-pool deadlock that occurs when
    spotipy's SpotifyOAuth session (created in the main thread) is reused in a
    worker thread.  A brand-new session is created here, inside the worker."""
    import requests as _requests
    sess = _requests.Session()
    return spotipy.Spotify(auth=token, requests_session=sess)


def get_playlist_track_ids(sp, playlist_id):
    """Fetch all track IDs currently in a playlist (handles pagination)."""
    ids = set()
    offset = 0
    while True:
        # /playlists/{id}/items replaces the deprecated /playlists/{id}/tracks
        results = sp._get(f"playlists/{playlist_id}/items", limit=100, offset=offset)
        for item in (results.get("items") or []):
            if not item:
                continue
            # The track object may be under "track" (old) or "item" (Feb 2026 rename)
            track = item.get("track") or item.get("item")
            if track and track.get("id"):
                ids.add(track["id"])
        if results.get("next"):
            offset += 100
        else:
            break
    return ids


def find_playlist_by_name(sp, user_id, name):
    """Return the first playlist owned by user_id with the given name, or None."""
    offset = 0
    while True:
        results = sp._get("me/playlists", limit=50, offset=offset)
        for pl in (results.get("items") or []):
            if pl and pl.get("owner", {}).get("id") == user_id and pl.get("name") == name:
                return pl
        if results.get("next"):
            offset += 50
        else:
            break
    return None


def create_playlist(sp, user_id, name, public):
    """Create a new playlist and return it."""
    return sp._post(
        "me/playlists",
        payload={
            "name": name,
            "public": public,
            "description": "Created by Shazam2Spotify — github.com/dairyking98/Shazam2Spotify",
        }
    )


def remove_duplicates_from_playlist(sp, playlist_id):
    """Remove duplicate tracks from a playlist. Returns count removed."""
    items = []
    offset = 0
    while True:
        results = sp._get(f"playlists/{playlist_id}/items", limit=100, offset=offset)
        for item in (results.get("items") or []):
            if not item:
                continue
            track = item.get("track") or item.get("item")
            if track and track.get("id"):
                items.append({"id": track["id"], "uri": track["uri"]})
        if results.get("next"):
            offset += 100
        else:
            break

    seen = set()
    to_remove = {}  # uri -> list of positions
    for pos, item in enumerate(items):
        tid = item["id"]
        if tid in seen:
            to_remove.setdefault(item["uri"], []).append(pos)
        else:
            seen.add(tid)

    removed = 0
    for uri, positions in to_remove.items():
        for pos in sorted(positions, reverse=True):
            sp._delete(
                f"playlists/{playlist_id}/items",
                payload={"items": [{"uri": uri, "positions": [pos]}]}
            )
            removed += 1
            time.sleep(0.2)
    return removed


# ── CSV parser ────────────────────────────────────────────────────────────────

def parse_shazam_csv(content):
    songs = []
    reader = csv.reader(io.StringIO(content))
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
        print(f"[S2S EMIT] event={event} data={data}", flush=True)
        transfer_queue.put({"event": event, "data": data})

    print(f"[S2S] run_transfer START — {len(songs)} songs", flush=True)
    print(f"[S2S] selected_playlist_id={cfg.get('selected_playlist_id')!r}", flush=True)
    print(f"[S2S] _access_token present: {bool(cfg.get('_access_token'))}", flush=True)
    playlist_url = ""
    try:
        emit("status", {"msg": "Connecting to Spotify...", "type": "info"})

        # Use pre-fetched token + fresh session to avoid Windows urllib3 deadlock
        access_token = cfg.get("_access_token")
        if not access_token:
            raise RuntimeError("No access token — please re-authenticate with Spotify")
        print("[S2S] creating sp with fresh session...", flush=True)
        sp = make_sp_with_token(access_token)
        print("[S2S] sp created", flush=True)

        # Use pre-fetched user info (no network call needed in worker)
        user_id      = cfg.get("_user_id", "")
        display_name = cfg.get("_display_name", user_id)
        print(f"[S2S] user: {display_name} ({user_id})", flush=True)
        emit("status", {"msg": f"Logged in as {display_name}", "type": "success"})

        # Settings
        playlist_name    = cfg.get("playlist_name", "Shazam2Spotify") or "Shazam2Spotify"
        selected_pl_id   = cfg.get("selected_playlist_id", "")
        selected_pl_name = cfg.get("selected_playlist_name", "")
        public           = bool(cfg.get("public_playlist", True))
        sync_mode        = bool(cfg.get("sync_mode", True))
        remove_dupes     = bool(cfg.get("remove_duplicates", False))
        skip_dupes       = bool(cfg.get("skip_duplicates", True))
        delay            = max(0.1, int(cfg.get("delay_ms", 500)) / 1000.0)
        is_new_playlist  = False

        # Resolve destination playlist
        print(f"[S2S] resolving playlist: selected_pl_id={selected_pl_id!r} playlist_name={playlist_name!r} sync_mode={sync_mode}", flush=True)
        if selected_pl_id:
            # User picked an existing playlist from the dropdown
            playlist_id  = selected_pl_id
            playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"
            print(f"[S2S] using selected playlist id={playlist_id}", flush=True)
            emit("status", {
                "msg": f"Using playlist '{selected_pl_name or playlist_id}'",
                "type": "info"
            })
        else:
            # Find or create by name
            print(f"[S2S] searching for existing playlist '{playlist_name}'...", flush=True)
            existing = find_playlist_by_name(sp, user_id, playlist_name) if sync_mode else None
            if existing:
                playlist_id  = existing["id"]
                playlist_url = existing["external_urls"]["spotify"]
                print(f"[S2S] found existing playlist id={playlist_id}", flush=True)
                emit("status", {
                    "msg": f"Found '{playlist_name}' — syncing new songs only",
                    "type": "info"
                })
            else:
                print(f"[S2S] creating new playlist '{playlist_name}'...", flush=True)
                pl = create_playlist(sp, user_id, playlist_name, public)
                playlist_id     = pl["id"]
                playlist_url    = f"https://open.spotify.com/playlist/{playlist_id}"
                is_new_playlist = True
                print(f"[S2S] created playlist id={playlist_id}", flush=True)
                emit("status", {
                    "msg": f"Created new playlist '{playlist_name}'",
                    "type": "success"
                })

        emit("playlist", {"url": playlist_url})

        # Fetch existing track IDs (skip for brand-new playlists)
        if is_new_playlist:
            existing_ids = set()
            print("[S2S] new playlist — skipping duplicate check", flush=True)
            emit("status", {"msg": "New playlist — no duplicate check needed", "type": "info"})
        else:
            print(f"[S2S] fetching existing track IDs for playlist {playlist_id}...", flush=True)
            emit("status", {"msg": "Checking existing playlist tracks...", "type": "info"})
            existing_ids = get_playlist_track_ids(sp, playlist_id)
            print(f"[S2S] got {len(existing_ids)} existing track IDs", flush=True)
            emit("status", {
                "msg": f"{len(existing_ids)} track(s) already in playlist — will skip these",
                "type": "info"
            })

        total       = len(songs)
        session_ids = set()
        added = skipped = csv_dupes = 0
        not_found = []

        print(f"[S2S] starting song loop: {total} songs, delay={delay}s", flush=True)
        for i, (title, artist) in enumerate(songs, 1):
            if shutdown_event.is_set():
                print("[S2S] shutdown_event set — stopping loop", flush=True)
                break
            try:
                print(f"[S2S] song {i}/{total}: searching '{title}' by '{artist}'...", flush=True)
                results = sp.search(
                    q=f"track:{title} artist:{artist}",
                    type="track",
                    limit=1
                )
                tracks = results["tracks"]["items"]
                if tracks:
                    tid     = tracks[0]["id"]
                    tname   = tracks[0]["name"]
                    tartist = tracks[0]["artists"][0]["name"]
                    print(f"[S2S]   found: {tname} by {tartist} (id={tid})", flush=True)

                    if tid in existing_ids:
                        skipped += 1
                        emit("song", {
                            "i": i, "total": total, "status": "skipped",
                            "title": tname, "artist": tartist,
                            "msg": "Already in playlist"
                        })
                    elif skip_dupes and tid in session_ids:
                        csv_dupes += 1
                        emit("song", {
                            "i": i, "total": total, "status": "duplicate",
                            "title": tname, "artist": tartist,
                            "msg": "Duplicate in CSV"
                        })
                    else:
                        sp._post(
                            f"playlists/{playlist_id}/items",
                            payload={"uris": [f"spotify:track:{tid}"]}
                        )
                        session_ids.add(tid)
                        existing_ids.add(tid)
                        added += 1
                        emit("song", {
                            "i": i, "total": total, "status": "added",
                            "title": tname, "artist": tartist,
                            "msg": "Added"
                        })
                else:
                    not_found.append(f"{title} — {artist}")
                    emit("song", {
                        "i": i, "total": total, "status": "notfound",
                        "title": title, "artist": artist,
                        "msg": "Not found on Spotify"
                    })
                time.sleep(delay)
            except Exception as e:
                emit("song", {
                    "i": i, "total": total, "status": "error",
                    "title": title, "artist": artist,
                    "msg": str(e)
                })
                time.sleep(1)

        # Optional: remove duplicates pass
        dupes_removed = 0
        if remove_dupes and not shutdown_event.is_set():
            emit("status", {"msg": "Scanning for duplicates to remove...", "type": "info"})
            try:
                dupes_removed = remove_duplicates_from_playlist(sp, playlist_id)
                emit("status", {
                    "msg": f"Removed {dupes_removed} duplicate(s)",
                    "type": "success"
                })
            except Exception as e:
                emit("status", {"msg": f"Duplicate removal error: {e}", "type": "error"})

        emit("done", {
            "total": total,
            "added": added,
            "skipped": skipped,
            "csv_dupes": csv_dupes,
            "dupes_removed": dupes_removed,
            "not_found": not_found,
            "playlist_url": playlist_url,
            "open_browser": bool(cfg.get("open_browser", True)),
        })

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[S2S] EXCEPTION in run_transfer:\n{tb}", flush=True)
        emit("error", {"msg": str(e)})
    finally:
        print("[S2S] run_transfer DONE, setting transfer_running=False", flush=True)
        transfer_running = False


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    cfg = load_config()
    sp_authenticated = False
    if os.path.exists(CACHE_FILE) and cfg.get("client_id"):
        try:
            sp = make_sp(cfg)
            sp_authenticated = bool(sp.current_user())
        except Exception:
            sp_authenticated = False
    return render_template("index.html", cfg=cfg, sp_authenticated=sp_authenticated)


@app.route("/save_config", methods=["POST"])
def save_config_route():
    data    = request.get_json() or {}
    old_cfg = load_config()
    new_id  = data.get("client_id", old_cfg["client_id"]).strip()
    new_uri = data.get("redirect_uri", old_cfg["redirect_uri"]).strip()
    # Wipe stale cache if credentials changed
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
        user = make_sp(cfg).current_user()
        return jsonify({"authenticated": True, "name": user.get("display_name", "Unknown")})
    except Exception:
        return jsonify({"authenticated": False})


@app.route("/get_playlists")
def get_playlists():
    """Return all user playlists with correct track counts for the dropdown."""
    cfg = load_config()
    try:
        sp = make_sp(cfg)
        playlists = []
        offset = 0
        while True:
            results = sp._get("me/playlists", limit=50, offset=offset)
            for pl in (results.get("items") or []):
                if not pl:
                    continue
                # The playlist list endpoint returns tracks.total (not items.total).
                # This is a different endpoint from the Feb 2026 /items change —
                # the list endpoint still uses the "tracks" key for the count object.
                total = 0
                if pl.get("tracks") and isinstance(pl["tracks"], dict):
                    total = pl["tracks"].get("total", 0) or 0
                playlists.append({
                    "id":    pl["id"],
                    "name":  pl["name"],
                    "total": total,
                })
            if results.get("next"):
                offset += 50
            else:
                break
        return jsonify({"ok": True, "playlists": playlists})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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
    """Clear any stuck transfer state (called on page load and New Transfer)."""
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
    for key in ("playlist_name", "open_browser", "public_playlist",
                "skip_duplicates", "remove_duplicates", "sync_mode", "delay_ms",
                "selected_playlist_id", "selected_playlist_name"):
        if key in data:
            cfg[key] = data[key]

    # Pre-fetch the access token and user info in the main Flask thread.
    # This avoids the Windows urllib3 connection-pool deadlock: spotipy's
    # SpotifyOAuth holds a requests.Session tied to the main thread's SSL
    # context; reusing it in a daemon thread causes sp.search() to hang.
    # By fetching the token here and passing it to the worker, the worker
    # creates its own fresh session and never touches SpotifyOAuth.
    try:
        auth_manager = make_auth_manager(cfg)
        token_info   = auth_manager.get_cached_token()
        if not token_info:
            return jsonify({"error": "Not authenticated with Spotify"}), 401
        if auth_manager.is_token_expired(token_info):
            token_info = auth_manager.refresh_access_token(token_info["refresh_token"])
        access_token = token_info["access_token"]
        # Also fetch user info in the main thread so the worker never needs to.
        sp_main = make_sp(cfg)
        user     = sp_main.current_user()
        cfg["_access_token"]    = access_token
        cfg["_user_id"]         = user["id"]
        cfg["_display_name"]    = user.get("display_name") or user["id"]
    except Exception as e:
        return jsonify({"error": f"Spotify auth error: {e}"}), 500

    transfer_queue   = queue.Queue()
    transfer_running = True
    transfer_thread  = threading.Thread(target=run_transfer, args=(cfg, songs), daemon=True)
    transfer_thread.start()
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
    if not os.path.exists(CONFIG_FILE):
        write_config(dict(DEFAULTS))
        print("  Created config.json — fill in your Client ID and Secret.")

    print("\n" + "=" * 55)
    print("  Shazam2Spotify Web Interface")
    print("  Open your browser at: http://127.0.0.1:5000")
    print("  Press Ctrl+C to stop")
    print("=" * 55 + "\n")

    try:
        from waitress import serve
        serve(app, host="127.0.0.1", port=5000, threads=8)
    except ImportError:
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
