"""
Shazam2Spotify — Web Interface
Run with: python web_app.py
Then open: http://127.0.0.1:5000
Press Ctrl+C to stop.
"""

import csv
import difflib
import io
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import unicodedata
import webbrowser
from datetime import datetime, timezone

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

DEBUG = "--debug" in sys.argv

app = Flask(__name__)
app.secret_key = "shazam2spotify-static-key-2024"   # static so sessions survive restarts

if DEBUG:
    @app.before_request
    def _log_request():
        print(f"[DEBUG] {request.method} {request.path}", flush=True)
        if request.is_json and request.data:
            try:
                print(f"[DEBUG] body: {request.get_json()}", flush=True)
            except Exception:
                pass

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE     = os.path.join(BASE_DIR, "config.json")
UPLOAD_FOLDER   = os.path.join(BASE_DIR, "library")
CACHE_FILE      = os.path.join(BASE_DIR, ".cache")
SONG_CACHE_FILE = os.path.join(BASE_DIR, "song_cache.json")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ── Global transfer state ─────────────────────────────────────────────────────
transfer_queue   = queue.Queue()
transfer_running = False
transfer_thread  = None
shutdown_event   = threading.Event()


# ── Config ────────────────────────────────────────────────────────────────────

FUZZY_THRESHOLD      = 0.85   # minimum similarity to pre-screen without an API call
NOT_FOUND_RETRY_DAYS = 30    # retry "not found" songs after this many days

DEFAULTS = {
    "client_id":     "",
    "client_secret": "",
    "redirect_uri":  "http://127.0.0.1:5000/callback",
    "playlist_name": "Shazam2Spotify",
    "public_playlist": True,
    "dupes_mode":    "skip",   # "skip" | "remove" | "none"
    "delay_ms":      500,
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
    # retries=0 disables spotipy's built-in Retry-After sleep (which can be
    # 84000+ seconds). We handle 429s ourselves with a capped delay.
    return spotipy.Spotify(auth_manager=make_auth_manager(cfg), retries=0)


# ── Search helpers ────────────────────────────────────────────────────────────

def _norm(s):
    return re.sub(r'[^a-z0-9]', '', s.lower())

# Matches "(feat. X)", "(ft. X)", "(featuring X)" including brackets
_FEAT_RE = re.compile(
    r'\s*[\(\[]\s*f(?:ea)?t\.?\s+([^\)\]]+)[\)\]]',
    re.IGNORECASE,
)
# Matches version/mix/remix qualifiers in parens or brackets, including
# named-person mixes like "(Adam K & Soha Vocal Mix)" or "[Nia Archives Remix]"
_VERSION_RE = re.compile(
    r'\s*[\(\[]\s*(?:'
    r'[^)\]]*\b(?:mix|edit|remix|version|rework|flip|mashup|dub)\b[^)\]]*'
    r'|extended(?:\s+mix)?|club\s+mix|radio\s+edit|remaster(?:ed)?(?:\s+\d+)?'
    r'|original\s+mix|instrumental|acoustic|live(?:\s+version)?|vip(?:\s+mix)?'
    r'|reprise|bonus(?:\s+track)?|deluxe|mixed|clean(?:\s+version)?'
    r')\s*[\)\]]',
    re.IGNORECASE,
)
# Split compound artists on " & ", " vs ", " x " but NOT on "/" (risks "AC/DC")
_ARTIST_SPLIT_RE = re.compile(r'\s+&\s+|\s+vs\.?\s+|\s+x\s+|,\s+', re.IGNORECASE)


def _fuzzy_prescreen(norm_title, norm_artist, existing_by_name):
    """
    Scan existing_by_name for a near-match above FUZZY_THRESHOLD on both title and artist.
    Returns (track_tuple, score) or (None, 0). Used when exact _norm lookup misses due to
    accent differences, minor punctuation variation, etc.
    """
    best_score, best_val = 0.0, None
    for (et, ea), val in existing_by_name.items():
        t_score = difflib.SequenceMatcher(None, norm_title, et).ratio()
        if t_score < FUZZY_THRESHOLD:
            continue
        a_score = difflib.SequenceMatcher(None, norm_artist, ea).ratio()
        if a_score < FUZZY_THRESHOLD:
            continue
        score = (t_score + a_score) / 2
        if score > best_score:
            best_score, best_val = score, val
    return best_val, best_score


def _cache_key(norm_title, norm_artist):
    return f"{norm_title}|{norm_artist}"


def load_song_cache():
    if not os.path.exists(SONG_CACHE_FILE):
        return {}
    try:
        with open(SONG_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("songs", {})
    except Exception:
        return {}


def save_song_cache(songs):
    try:
        with open(SONG_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "songs": songs}, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] Could not save song cache: {e}", flush=True)


def fix_mojibake(s):
    """Recover UTF-8 text that was incorrectly decoded as Latin-1 (common in Shazam exports)."""
    try:
        return s.encode('latin-1').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        return s


def _extract_featured(title):
    """Strip (feat. X) from title, return (clean_title, [featured_names])."""
    featured = []
    for m in _FEAT_RE.finditer(title):
        featured.extend(p.strip() for p in re.split(r'\s*[&,]\s*', m.group(1)) if p.strip())
    return _FEAT_RE.sub('', title).strip(), featured


def _strip_version(title):
    """Remove version/mix/remix qualifiers from title."""
    return _VERSION_RE.sub('', title).strip()


def _split_artists(artist):
    """Split 'DRS & LSB' → ['DRS', 'LSB']. Conservative: ignores '/' to protect 'AC/DC'."""
    parts = _ARTIST_SPLIT_RE.split(artist)
    return [p.strip() for p in parts if p.strip()] or [artist]


def _clean_text(s):
    """Lowercase, remove accents, strip punctuation — for last-resort normalization."""
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[''`]", '', s.lower())
    s = re.sub(r'[^\w\s]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _plausible(csv_title, track):
    """Sanity-check a broad search result: at least one significant word must appear in the track name."""
    sp_name = _clean_text(track.get('name', ''))
    words = [w for w in _clean_text(csv_title).split() if len(w) > 3]
    return not words or any(w in sp_name for w in words)


AUTO_RETRY_MAX = 120   # sleep-and-retry only if Retry-After ≤ this; otherwise stop

def _parse_retry_after(e):
    """Return the Retry-After seconds from a 429 SpotifyException header."""
    try:
        if hasattr(e, 'headers') and e.headers:
            return int(e.headers.get('Retry-After', 10))
    except Exception:
        pass
    return 10

def _fmt_wait(seconds):
    """Format a wait duration as '5s', '3m 20s', or '23h 28m'."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def search_track(sp, title, artist, inter_stage_delay=0.0, call_counter=None, emit_fn=None):
    """
    Progressive 5-stage Spotify search pipeline.
    Returns (track_dict, stage_int) on success, or (None, 'multi'|None) on failure.

    Stage 1 — strict field operators: track:{title} artist:{artist}
    Stage 2 — free-text: {title} {artist}  (Spotify handles fuzzy ranking)
    Stage 3 — strip (feat. X) from title, try each primary/featured artist
    Stage 4 — strip version/mix/remix qualifiers, try each artist
    Stage 5 — full unicode normalization + accent removal

    inter_stage_delay: seconds to sleep before each retry call (not before the first).
    call_counter: optional [int] list; incremented once per API call made.
    emit_fn: optional emit(event, data) callable; used to surface 429 warnings.
    """
    if re.search(r' / ', title):
        return None, 'multi'

    title  = fix_mojibake(title)
    artist = fix_mojibake(artist)
    first_call = True

    def _q(q):
        nonlocal first_call
        if not first_call and inter_stage_delay:
            time.sleep(inter_stage_delay)
        first_call = False
        if call_counter is not None:
            call_counter[0] += 1
        for attempt in range(3):
            try:
                r = sp.search(q=q, type='track', limit=1)
                items = r['tracks']['items']
                return items[0] if items else None
            except spotipy.SpotifyException as e:
                if e.http_status == 429:
                    raw_after = _parse_retry_after(e)
                    if raw_after > AUTO_RETRY_MAX or attempt == 2:
                        if emit_fn:
                            emit_fn("status", {
                                "msg": f"Rate limited — Spotify requests {_fmt_wait(raw_after)} wait. Cache saved — re-run in {_fmt_wait(raw_after)} to resume.",
                                "type": "error",
                            })
                        raise  # propagate so song isn't cached and transfer stops
                    if emit_fn:
                        emit_fn("status", {
                            "msg": f"Rate limited — waiting {_fmt_wait(raw_after + 2)} (Spotify requested {_fmt_wait(raw_after)}, attempt {attempt + 1}/3)",
                            "type": "error",
                        })
                    time.sleep(raw_after + 2)
                    continue
                raise
        return None

    # Stage 1 — strict
    if t := _q(f'track:{title} artist:{artist}'):
        return t, 1

    # Stage 2 — free-text
    if (t := _q(f'{title} {artist}')) and _plausible(title, t):
        return t, 2

    # Stage 3 — remove (feat. X) from title, try each artist variant
    clean_title, featured = _extract_featured(title)
    # Only try artists not already queried as `artist` to avoid duplicate API calls
    extra_artists = [a for a in (_split_artists(artist) + featured) if _norm(a) != _norm(artist)]
    if clean_title != title:
        if (t := _q(f'{clean_title} {artist}')) and _plausible(clean_title, t):
            return t, 3
        for a in extra_artists:
            if (t := _q(f'{clean_title} {a}')) and _plausible(clean_title, t):
                return t, 3

    # Stage 4 — also strip version/mix/remix qualifiers
    base = _strip_version(clean_title or title)
    if base and base != (clean_title or title):
        if (t := _q(f'{base} {artist}')) and _plausible(base, t):
            return t, 4
        for a in extra_artists:
            if (t := _q(f'{base} {a}')) and _plausible(base, t):
                return t, 4

    # Stage 5 — full text normalization (accents, punctuation)
    norm_t = _clean_text(base or clean_title or title)
    norm_a = _clean_text(artist)
    if norm_t and (t := _q(f'{norm_t} {norm_a}')) and _plausible(norm_t, t):
        return t, 5

    return None, None


def _playlist_track_keys(tname, all_artists):
    """
    Return all (norm_title, norm_artist) key variants for a playlist track so that
    Shazam CSV entries with different feat/version suffixes or compound artists
    still match without an API call.

    Covers: full title × all artists, stripped title × all artists.
    Stripped = feat. removed then version/remix/edit/remaster removed.
    """
    clean, _ = _extract_featured(tname)
    stripped  = _strip_version(clean)

    titles  = [tname]
    if stripped and stripped.lower() != tname.lower():
        titles.append(stripped)

    keys = []
    for t in titles:
        nt = _norm(t)
        for art in all_artists:
            keys.append((nt, _norm(art)))
    return keys


def get_all_playlist_track_ids(sp, playlist_id, call_counter=None):
    # Use /items endpoint (replaces deprecated /tracks — Spotify Feb 2026 API change)
    ids     = set()
    by_name = {}   # {(norm_title, norm_artist): (track_id, spotify_name, spotify_artist)}
    offset  = 0
    while True:
        if call_counter is not None:
            call_counter[0] += 1
        results = sp._get(f"playlists/{playlist_id}/items", limit=100, offset=offset)
        for item in results.get("items", []):
            track = item.get("track") or item.get("item") if item else None
            if track and track.get("id"):
                tid        = track["id"]
                tname      = track.get("name", "")
                all_arts   = [a["name"] for a in track.get("artists", []) if a.get("name")] or [""]
                primary    = all_arts[0]
                ids.add(tid)
                val = (tid, tname, primary)
                # Index under full and stripped title × all artists so Shazam CSVs
                # with missing feat./version suffixes still get an O(1) match.
                for key in _playlist_track_keys(tname, all_arts):
                    by_name.setdefault(key, val)
                # Always ensure the exact key wins (don't let stripped overwrite it)
                by_name[(_norm(tname), _norm(primary))] = val
        if results.get("next"):
            offset += 100
        else:
            break
    return ids, by_name


def find_existing_playlist(sp, user_id, name, call_counter=None):
    # Direct call to /v1/me/playlists — works on all spotipy versions
    offset = 0
    while True:
        if call_counter is not None:
            call_counter[0] += 1
        results = sp._get("me/playlists", limit=50, offset=offset)
        for pl in results["items"]:
            if pl["owner"]["id"] == user_id and pl["name"] == name:
                return pl
        if results.get("next"):
            offset += 50
        else:
            break
    return None


def remove_playlist_duplicates(sp, playlist_id, delay=0.5, call_counter=None):
    # Use /items endpoint (replaces deprecated /tracks — Spotify Feb 2026 API change)
    items = []
    offset = 0
    while True:
        if call_counter is not None:
            call_counter[0] += 1
        results = sp._get(f"playlists/{playlist_id}/items", limit=100, offset=offset)
        for item in results.get("items", []):
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
            if call_counter is not None:
                call_counter[0] += 1
            for attempt in range(3):
                try:
                    sp._delete(
                        f"playlists/{playlist_id}/items",
                        payload={"items": [{"uri": uri, "positions": [pos]}]}
                    )
                    break
                except spotipy.SpotifyException as e:
                    if e.http_status == 429:
                        raw_after = _parse_retry_after(e)
                        if raw_after > AUTO_RETRY_MAX or attempt == 2:
                            raise
                        time.sleep(raw_after + 2)
                        continue
                    raise
            removed += 1
            time.sleep(delay)
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

    api_calls    = [0]   # mutable so nested functions can increment

    def emit(event, data):
        if event == "song":
            data = {**data, "api_calls": api_calls[0]}
        transfer_queue.put({"event": event, "data": data})

    playlist_url = ""
    start_time   = time.time()
    try:
        emit("status", {"msg": "Connecting to Spotify...", "type": "info"})
        sp   = make_sp(cfg)
        api_calls[0] += 1
        user = sp.current_user()
        emit("status", {"msg": f"Logged in as {user['display_name']}", "type": "success"})

        playlist_name   = cfg.get("playlist_name", "Shazam2Spotify") or "Shazam2Spotify"
        playlist_id_cfg = cfg.get("playlist_id") or None
        public          = cfg.get("public_playlist", True)
        dupes_mode      = cfg.get("dupes_mode", "skip")   # "skip" | "remove" | "none"
        skip_dupes      = dupes_mode in ("skip", "remove")
        remove_dupes    = dupes_mode == "remove"
        delay           = max(0.1, cfg.get("delay_ms", 500) / 1000.0)

        # Find or create playlist
        is_new_playlist = False
        if playlist_id_cfg:
            playlist_id = playlist_id_cfg
            try:
                api_calls[0] += 1
                pl_info      = sp._get(f"playlists/{playlist_id}")
                playlist_url  = pl_info["external_urls"]["spotify"]
                playlist_name = pl_info["name"]
            except Exception:
                playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"
            emit("status", {"msg": f"Using existing playlist '{playlist_name}'", "type": "info"})
        else:
            existing = find_existing_playlist(sp, user["id"], playlist_name, api_calls)
            if existing:
                playlist_id  = existing["id"]
                playlist_url = existing["external_urls"]["spotify"]
                emit("status", {"msg": f"Found '{playlist_name}' — syncing new songs only", "type": "info"})
            else:
                # Use direct API call to /v1/me/playlists — works on all spotipy versions
                api_calls[0] += 1
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

        # Reverse so oldest Shazam entries (bottom of CSV) are added first;
        # new entries (top of CSV) then append to the playlist end on re-runs
        songs = list(reversed(songs))

        # Load song match cache
        song_cache = load_song_cache()
        cache_hits = 0
        if song_cache:
            emit("status", {"msg": f"Song cache loaded — {len(song_cache)} entries", "type": "info"})

        # Fetch existing tracks only if syncing to an existing playlist (skip for new ones)
        if is_new_playlist:
            existing_ids, existing_by_name = set(), {}
            emit("status", {"msg": "New playlist — skipping duplicate check", "type": "info"})
        else:
            emit("status", {"msg": "Fetching existing playlist tracks...", "type": "info"})
            existing_ids, existing_by_name = get_all_playlist_track_ids(sp, playlist_id, api_calls)
            emit("status", {"msg": f"{len(existing_ids)} tracks already in playlist", "type": "info"})
            if existing_by_name:
                def _would_prescreen(t, a):
                    nt, na = _norm(t), _norm(a)
                    if (nt, na) in existing_by_name:
                        return True
                    clean, _ = _extract_featured(t)
                    nts = _norm(_strip_version(clean))
                    if nts != nt and (nts, na) in existing_by_name:
                        return True
                    for sp_art in _split_artists(a):
                        na_s = _norm(sp_art)
                        if na_s != na and ((nt, na_s) in existing_by_name or (nts != nt and (nts, na_s) in existing_by_name)):
                            return True
                    return False
                prescreened = sum(1 for t, a in songs if _would_prescreen(t, a))
                if prescreened:
                    emit("status", {"msg": f"{prescreened}/{len(songs)} songs matched in playlist by name — skipping Spotify search for those", "type": "info"})

        total       = len(songs)
        session_ids = set()
        added = skipped = csv_dupes = 0
        not_found = []

        # Batch pending adds — flushed every ADD_BATCH tracks or at end of loop.
        # Spotify allows up to 100 URIs per POST; batching cuts add calls by ~50-100x.
        ADD_BATCH = 50
        to_add    = []  # list of {uri, i, csv_idx, tname, tartist, stage}

        def flush_adds():
            nonlocal added
            if not to_add:
                return
            uris = [item['uri'] for item in to_add]
            api_calls[0] += 1
            for attempt in range(3):
                try:
                    sp._post(f"playlists/{playlist_id}/items", payload={"uris": uris})
                    break
                except spotipy.SpotifyException as e:
                    if e.http_status == 429:
                        raw_after = _parse_retry_after(e)
                        if raw_after > AUTO_RETRY_MAX:
                            emit("status", {"msg": f"Rate limited — Spotify requests {_fmt_wait(raw_after)} wait. Re-run in {_fmt_wait(raw_after)} to resume.", "type": "error"})
                            raise
                        emit("status", {"msg": f"Rate limited — waiting {_fmt_wait(raw_after + 2)} before retrying add...", "type": "info"})
                        time.sleep(raw_after + 2)
                        continue
                    for item in to_add:
                        emit("song", {"i": item['i'], "total": total, "csv_idx": item['csv_idx'],
                                      "status": "error", "title": item['tname'], "artist": item['tartist'],
                                      "msg": f"Add failed: {e}"})
                    to_add.clear()
                    return
                except Exception as e:
                    for item in to_add:
                        emit("song", {"i": item['i'], "total": total, "csv_idx": item['csv_idx'],
                                      "status": "error", "title": item['tname'], "artist": item['tartist'],
                                      "msg": f"Add failed: {e}"})
                    to_add.clear()
                    return
            for item in to_add:
                add_msg = "Added" if item['stage'] == 1 else f"Added (matched via stage {item['stage']})"
                emit("song", {"i": item['i'], "total": total, "csv_idx": item['csv_idx'],
                              "status": "added", "title": item['tname'], "artist": item['tartist'],
                              "msg": add_msg})
                added += 1
            to_add.clear()

        now_iso = lambda: datetime.now(timezone.utc).isoformat()

        for i, (title, artist) in enumerate(songs, 1):
            if shutdown_event.is_set():
                save_song_cache(song_cache)
                break
            csv_idx = total - i
            try:
                nt, na = _norm(title), _norm(artist)
                ck     = _cache_key(nt, na)

                # ── Exact / variant pre-screen (no API call) ─────────────────
                # Try: (full title, artist), (stripped title, artist),
                #      and each split sub-artist for compound CSV entries.
                clean_csv, _ = _extract_featured(title)
                nts = _norm(_strip_version(clean_csv))  # stripped CSV title

                pre = existing_by_name.get((nt, na))
                if not pre and nts != nt:
                    pre = existing_by_name.get((nts, na))
                if not pre:
                    for split_art in _split_artists(artist):
                        na_s = _norm(split_art)
                        if na_s == na:
                            continue
                        pre = existing_by_name.get((nt, na_s)) or (existing_by_name.get((nts, na_s)) if nts != nt else None)
                        if pre:
                            break
                if pre:
                    m_tid, m_name, m_art = pre
                    if ck not in song_cache:
                        song_cache[ck] = {"track_id": m_tid, "spotify_title": m_name,
                                          "spotify_artist": m_art, "stage": 0,
                                          "searched_at": now_iso(), "status": "found"}
                    exact = (m_name.lower() == title.lower() and m_art.lower() == artist.lower())
                    pre_label = "Already in playlist (pre-screened)" if exact else f"Already in playlist (matched: {m_name} — {m_art})"
                    if skip_dupes and m_tid in session_ids:
                        csv_dupes += 1
                        emit("song", {"i": i, "total": total, "csv_idx": csv_idx, "status": "duplicate",
                                      "title": m_name, "artist": m_art, "msg": "Duplicate in CSV"})
                    else:
                        skipped += 1
                        emit("song", {"i": i, "total": total, "csv_idx": csv_idx, "status": "skipped",
                                      "title": m_name, "artist": m_art, "msg": pre_label})
                    continue

                # ── Fuzzy pre-screen (no API call) ──────────────────────────
                if existing_by_name:
                    fval, fscore = _fuzzy_prescreen(nt, na, existing_by_name)
                    if fval:
                        m_tid, m_name, m_art = fval
                        if ck not in song_cache:
                            song_cache[ck] = {"track_id": m_tid, "spotify_title": m_name,
                                              "spotify_artist": m_art, "stage": 0,
                                              "searched_at": now_iso(), "status": "found"}
                        if skip_dupes and m_tid in session_ids:
                            csv_dupes += 1
                            emit("song", {"i": i, "total": total, "csv_idx": csv_idx, "status": "duplicate",
                                          "title": m_name, "artist": m_art,
                                          "msg": f"Duplicate in CSV (fuzzy {fscore:.0%})"})
                        else:
                            skipped += 1
                            emit("song", {"i": i, "total": total, "csv_idx": csv_idx, "status": "skipped",
                                          "title": m_name, "artist": m_art,
                                          "msg": f"Already in playlist (fuzzy {fscore:.0%})"})
                        continue

                # ── Song match cache (no API call) ──────────────────────────
                cached = song_cache.get(ck)
                if cached:
                    if cached["status"] == "found":
                        cache_hits += 1
                        tid     = cached["track_id"]
                        tname   = cached["spotify_title"]
                        tartist = cached["spotify_artist"]
                        if tid in existing_ids:
                            skipped += 1
                            emit("song", {"i": i, "total": total, "csv_idx": csv_idx, "status": "skipped",
                                          "title": tname, "artist": tartist, "msg": "Already in playlist (cached)"})
                        elif skip_dupes and tid in session_ids:
                            csv_dupes += 1
                            emit("song", {"i": i, "total": total, "csv_idx": csv_idx, "status": "duplicate",
                                          "title": tname, "artist": tartist, "msg": "Duplicate in CSV (cached)"})
                        else:
                            session_ids.add(tid)
                            existing_ids.add(tid)
                            to_add.append({
                                'uri': f"spotify:track:{tid}", 'i': i, 'csv_idx': csv_idx,
                                'tname': tname, 'tartist': tartist, 'stage': cached["stage"],
                            })
                            if len(to_add) >= ADD_BATCH:
                                flush_adds()
                        continue
                    elif cached["status"] == "not_found":
                        searched_at = datetime.fromisoformat(cached["searched_at"])
                        days_ago    = (datetime.now(timezone.utc) - searched_at).days
                        if days_ago < NOT_FOUND_RETRY_DAYS:
                            not_found.append(f"{title} — {artist}")
                            emit("song", {"i": i, "total": total, "csv_idx": csv_idx, "status": "notfound",
                                          "title": title, "artist": artist,
                                          "msg": f"Not found on Spotify (retry in {NOT_FOUND_RETRY_DAYS - days_ago}d)"})
                            continue
                        emit("status", {"msg": f"Retrying: '{title}' — not found {days_ago}d ago", "type": "info"})

                # ── Spotify API search ──────────────────────────────────────
                emit("status", {"msg": f"Searching: {title} — {artist}", "type": "info"})
                track, stage = search_track(sp, title, artist, delay, api_calls, emit)
                if track:
                    tid     = track["id"]
                    tname   = track["name"]
                    tartist = track["artists"][0]["name"]
                    song_cache[ck] = {"track_id": tid, "spotify_title": tname,
                                      "spotify_artist": tartist, "stage": stage,
                                      "searched_at": now_iso(), "status": "found"}
                    if tid in existing_ids:
                        skipped += 1
                        emit("song", {"i": i, "total": total, "csv_idx": csv_idx, "status": "skipped",
                                      "title": tname, "artist": tartist, "msg": "Already in playlist (API search)"})
                    elif skip_dupes and tid in session_ids:
                        csv_dupes += 1
                        emit("song", {"i": i, "total": total, "csv_idx": csv_idx, "status": "duplicate",
                                      "title": tname, "artist": tartist, "msg": "Duplicate in CSV"})
                    else:
                        session_ids.add(tid)
                        existing_ids.add(tid)
                        to_add.append({
                            'uri': f"spotify:track:{tid}", 'i': i, 'csv_idx': csv_idx,
                            'tname': tname, 'tartist': tartist, 'stage': stage,
                        })
                        if len(to_add) >= ADD_BATCH:
                            flush_adds()
                else:
                    if stage != 'multi':
                        song_cache[ck] = {"track_id": None, "spotify_title": None,
                                          "spotify_artist": None, "stage": None,
                                          "searched_at": now_iso(), "status": "not_found"}
                    reason = ("Multi-track / medley — skipped" if stage == 'multi'
                              else "Not found on Spotify")
                    not_found.append(f"{title} — {artist}")
                    emit("song", {"i": i, "total": total, "csv_idx": csv_idx, "status": "notfound",
                                  "title": title, "artist": artist, "msg": reason})
                time.sleep(delay)
            except spotipy.SpotifyException as e:
                if e.http_status == 429:
                    save_song_cache(song_cache)
                    emit("song", {"i": i, "total": total, "csv_idx": csv_idx, "status": "error",
                                  "title": title, "artist": artist,
                                  "msg": "Rate limited — not cached, will retry on re-run"})
                    emit("status", {
                        "msg": "Spotify rate limit reached. Cache saved — re-run transfer to resume (cached songs will be skipped instantly).",
                        "type": "error",
                    })
                else:
                    emit("song", {"i": i, "total": total, "csv_idx": csv_idx, "status": "error",
                                  "title": title, "artist": artist, "msg": str(e)})
                time.sleep(delay)
            except Exception as e:
                emit("song", {"i": i, "total": total, "csv_idx": csv_idx, "status": "error",
                              "title": title, "artist": artist, "msg": str(e)})
                time.sleep(delay)

            if i % 50 == 0:
                save_song_cache(song_cache)

        flush_adds()
        save_song_cache(song_cache)

        # Remove duplicates pass
        dupes_removed = 0
        if remove_dupes and not shutdown_event.is_set():
            emit("status", {"msg": "Scanning for duplicates to remove...", "type": "info"})
            try:
                dupes_removed = remove_playlist_duplicates(sp, playlist_id, delay, api_calls)
                emit("status", {"msg": f"Removed {dupes_removed} duplicate(s)", "type": "success"})
            except Exception as e:
                emit("status", {"msg": f"Duplicate removal error: {e}", "type": "error"})

        emit("done", {
            "total": total, "added": added, "skipped": skipped,
            "csv_dupes": csv_dupes, "dupes_removed": dupes_removed,
            "not_found": not_found, "playlist_url": playlist_url,
            "api_calls": api_calls[0], "elapsed_s": round(time.time() - start_time, 1),
            "cache_hits": cache_hits,
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
        "client_id":     new_id,
        "client_secret": data.get("client_secret", old_cfg.get("client_secret", "")).strip(),
        "redirect_uri":  new_uri,
        "playlist_name": data.get("playlist_name", old_cfg.get("playlist_name", "Shazam2Spotify")).strip() or "Shazam2Spotify",
        "public_playlist": bool(data.get("public_playlist", old_cfg.get("public_playlist", True))),
        "dupes_mode":    data.get("dupes_mode", old_cfg.get("dupes_mode", "skip")),
        "delay_ms":      int(data.get("delay_ms", old_cfg.get("delay_ms", 500))),
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
        make_auth_manager(cfg).get_access_token(code, as_dict=False)
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


@app.route("/get_playlists")
def get_playlists():
    cfg = load_config()
    if DEBUG:
        print(f"[DEBUG] get_playlists: cache={os.path.exists(CACHE_FILE)} client_id={bool(cfg.get('client_id'))}", flush=True)
    if not os.path.exists(CACHE_FILE) or not cfg.get("client_id"):
        return jsonify({"error": "Not authenticated"}), 401
    try:
        sp   = make_sp(cfg)
        user = sp.current_user()
        uid  = user["id"]
        if DEBUG:
            print(f"[DEBUG] get_playlists: logged in as {user.get('display_name')} ({uid})", flush=True)
        playlists = []
        offset = 0
        while True:
            results = sp._get("me/playlists", limit=50, offset=offset)
            for pl in results.get("items", []) or []:
                playlists.append({
                    "id":     pl["id"],
                    "name":   pl["name"],
                    "tracks": (pl.get("items") or pl.get("tracks") or {}).get("total", 0),
                    "owned":  pl["owner"]["id"] == uid,
                })
            if results.get("next"):
                offset += 50
            else:
                break
        if DEBUG:
            print(f"[DEBUG] get_playlists: returning {len(playlists)} playlists", flush=True)
        return jsonify({"playlists": playlists})
    except Exception as e:
        if DEBUG:
            traceback.print_exc()
        print(f"[ERROR] get_playlists: {e}", flush=True)
        return jsonify({"error": str(e)}), 500


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


@app.route("/preflight", methods=["POST"])
def preflight():
    """Estimate Spotify API calls for a transfer without starting it."""
    data      = request.get_json() or {}
    songs_raw = data.get("songs", [])
    songs     = [(s[0], s[1]) for s in songs_raw if len(s) >= 2]
    if not songs:
        return jsonify({"error": "No songs"}), 400

    cfg = load_config()
    for key in ("playlist_name", "playlist_id", "public_playlist", "dupes_mode", "delay_ms"):
        if key in data:
            cfg[key] = data[key]

    try:
        sp   = make_sp(cfg)
        user = sp.current_user()

        playlist_id_cfg = cfg.get("playlist_id") or None
        playlist_name   = cfg.get("playlist_name", "Shazam2Spotify") or "Shazam2Spotify"
        is_new_playlist = False
        existing_total  = 0

        if playlist_id_cfg:
            try:
                pl_info       = sp._get(f"playlists/{playlist_id_cfg}")
                existing_total = (pl_info.get("items") or pl_info.get("tracks") or {}).get("total", 0)
                playlist_name  = pl_info.get("name", playlist_name)
            except Exception:
                pass
        else:
            existing = find_existing_playlist(sp, user["id"], playlist_name)
            if existing:
                existing_total = (existing.get("items") or existing.get("tracks") or {}).get("total", 0)
            else:
                is_new_playlist = True

        total       = len(songs)
        fetch_calls = 0 if is_new_playlist else (existing_total + 99) // 100
        search_max  = total * 5
        add_max     = (total + 49) // 50

        return jsonify({
            "total_songs":     total,
            "existing_tracks": existing_total,
            "is_new_playlist": is_new_playlist,
            "playlist_name":   playlist_name,
            "estimates": {
                "fetch_calls": fetch_calls,
                "search_min":  0,
                "search_max":  search_max,
                "add_max":     add_max,
                "total_min":   fetch_calls,
                "total_max":   fetch_calls + search_max + add_max,
            },
        })
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
    for key in ("playlist_name", "playlist_id", "public_playlist", "dupes_mode", "delay_ms"):
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

    debug_tag = "  *** DEBUG MODE — requests and errors logged ***\n" if DEBUG else ""
    print("\n" + "=" * 55)
    print("  Shazam2Spotify Web Interface")
    print("  Open your browser at: http://127.0.0.1:5000")
    print("  Press Ctrl+C to stop")
    if DEBUG:
        print("  Debug: python web_app.py --debug")
    print("=" * 55)
    if debug_tag:
        print(debug_tag, end="")
    print()

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
