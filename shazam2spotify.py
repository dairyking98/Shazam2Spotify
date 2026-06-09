"""
Shazam2Spotify - Fixed & Improved Version
==========================================
Transfers your Shazam library (CSV export) to a new Spotify playlist.

SETUP INSTRUCTIONS
------------------
1. Get your Spotify API credentials:
   - Go to https://developer.spotify.com/dashboard
   - Log in and click "Create App"
   - Fill in any name/description, set Redirect URI to: http://127.0.0.1:8888/callback
     (Note: Spotify no longer allows 'localhost' — use the IP 127.0.0.1 instead)
   - Copy your Client ID and Client Secret

2. Set your credentials below (or use environment variables):
   - SPOTIPY_CLIENT_ID
   - SPOTIPY_CLIENT_SECRET
   - SPOTIPY_REDIRECT_URI

3. Export your Shazam library:
   - Go to https://www.shazam.com/myshazam
   - Log in and click "Download CSV"
   - Place the downloaded file in the library/ folder (or pass its path as argument)

4. Run:
   python shazam2spotify.py
   # or specify a custom CSV path:
   python shazam2spotify.py --csv /path/to/your/shazamlibrary.csv
   # or specify a custom playlist name:
   python shazam2spotify.py --name "My Shazam Songs"
"""

import csv
import os
import sys
import time
import argparse
import webbrowser

try:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
except ImportError:
    print("ERROR: spotipy is not installed. Run: pip install spotipy")
    sys.exit(1)


# ─── CONFIGURATION ────────────────────────────────────────────────────────────
# You can set these directly here, or use environment variables.
# Environment variables take priority over the values set here.

CLIENT_ID     = os.environ.get("SPOTIPY_CLIENT_ID",     "YOUR_CLIENT_ID_HERE")
CLIENT_SECRET = os.environ.get("SPOTIPY_CLIENT_SECRET", "YOUR_CLIENT_SECRET_HERE")
REDIRECT_URI  = os.environ.get("SPOTIPY_REDIRECT_URI",  "http://127.0.0.1:8888/callback")

# ──────────────────────────────────────────────────────────────────────────────


def parse_args():
    parser = argparse.ArgumentParser(
        description="Transfer your Shazam library CSV to a Spotify playlist."
    )
    parser.add_argument(
        "--csv",
        default=os.path.join(os.path.dirname(__file__), "library", "shazamlibrary.csv"),
        help="Path to your Shazam library CSV file (default: library/shazamlibrary.csv)",
    )
    parser.add_argument(
        "--name",
        default="Shazam2Spotify",
        help='Name for the new Spotify playlist (default: "Shazam2Spotify")',
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the playlist in the browser when done",
    )
    return parser.parse_args()


def read_shazam_csv(csv_path):
    """
    Parse the Shazam CSV export.
    The file has a header row 'Shazam Library', then a column row, then data.
    Columns: Index, TagTime, Title, Artist, URL, TrackKey
    """
    if not os.path.exists(csv_path):
        print(f"ERROR: CSV file not found: {csv_path}")
        print("Please export your Shazam library from https://www.shazam.com/myshazam")
        print("and place it in the library/ folder, or pass --csv /path/to/file")
        sys.exit(1)

    songs = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header_skipped = False
        for row in reader:
            # Skip the "Shazam Library" title row and the column-name row
            if not header_skipped:
                if row and row[0].strip().upper() in ("SHAZAM LIBRARY", "INDEX"):
                    continue
                # If the row looks like the column header
                if len(row) >= 4 and row[0].strip().lower() == "index":
                    header_skipped = True
                    continue
                header_skipped = True  # skip first non-empty row regardless

            if len(row) >= 4:
                title  = row[2].strip().strip('"')
                artist = row[3].strip().strip('"')
                if title and artist:
                    songs.append((title, artist))

    return songs


def create_spotify_client(client_id, client_secret, redirect_uri):
    """Authenticate with Spotify and return a client."""
    if client_id == "YOUR_CLIENT_ID_HERE" or not client_id:
        print("\nERROR: Spotify credentials are not configured.")
        print("Please edit shazam2spotify.py and fill in CLIENT_ID and CLIENT_SECRET,")
        print("or set the environment variables SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET.")
        print("\nGet your credentials at: https://developer.spotify.com/dashboard")
        sys.exit(1)

    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope="playlist-modify-public playlist-modify-private",
        open_browser=True,
    )
    return spotipy.Spotify(auth_manager=auth_manager)


def main():
    args = parse_args()

    print("=" * 60)
    print("  Shazam2Spotify — Fixed Version")
    print("=" * 60)

    # ── Step 1: Authenticate ──────────────────────────────────────
    print("\n[1/4] Connecting to Spotify...")
    sp = create_spotify_client(CLIENT_ID, CLIENT_SECRET, REDIRECT_URI)

    user = sp.current_user()
    print(f"      Logged in as: {user['display_name']} ({user['id']})")

    # ── Step 2: Read CSV ──────────────────────────────────────────
    print(f"\n[2/4] Reading Shazam library from: {args.csv}")
    songs = read_shazam_csv(args.csv)
    print(f"      Found {len(songs)} songs.")

    if not songs:
        print("ERROR: No songs found in the CSV. Check the file format.")
        sys.exit(1)

    # ── Step 3: Create playlist ───────────────────────────────────
    print(f"\n[3/4] Creating Spotify playlist: '{args.name}'")
    playlist = sp.user_playlist_create(
        user=user["id"],
        name=args.name,
        public=True,
        description="Playlist created by Shazam2Spotify (github.com/jclosadev/Shazam2Spotify)",
    )
    playlist_id = playlist["id"]
    print(f"      Playlist created with ID: {playlist_id}")

    # ── Step 4: Search and add songs ──────────────────────────────
    print(f"\n[4/4] Searching and adding songs to playlist...")
    print("-" * 60)

    added_ids   = set()
    added_count = 0
    not_found   = []
    duplicates  = 0

    for i, (title, artist) in enumerate(songs, 1):
        query = f"track:{title} artist:{artist}"
        try:
            results = sp.search(q=query, type="track", limit=1)
            tracks  = results["tracks"]["items"]

            if tracks:
                track_id = tracks[0]["id"]
                if track_id not in added_ids:
                    sp.playlist_add_items(playlist_id, [track_id])
                    added_ids.add(track_id)
                    added_count += 1
                    print(f"  [{i}/{len(songs)}] ✓ Added:      {title} — {artist}")
                else:
                    duplicates += 1
                    print(f"  [{i}/{len(songs)}] ~ Duplicate:  {title} — {artist}")
            else:
                not_found.append((title, artist))
                print(f"  [{i}/{len(songs)}] ✗ Not found:  {title} — {artist}")

            # Respect Spotify rate limits
            time.sleep(0.5)

        except Exception as e:
            print(f"  [{i}/{len(songs)}] ! Error ({title} — {artist}): {e}")
            time.sleep(1)

    # ── Summary ───────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  Done!")
    print(f"  Songs processed : {len(songs)}")
    print(f"  Songs added     : {added_count}")
    print(f"  Duplicates      : {duplicates}")
    print(f"  Not found       : {len(not_found)}")

    if not_found:
        print("\n  Songs not found on Spotify:")
        for title, artist in not_found:
            print(f"    - {title} — {artist}")

    playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"
    print(f"\n  Playlist URL: {playlist_url}")

    if not args.no_browser:
        print("  Opening playlist in browser...")
        webbrowser.open(playlist_url)

    print("=" * 60)


if __name__ == "__main__":
    main()
