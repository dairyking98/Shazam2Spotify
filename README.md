# Shazam2Spotify — Fixed Version

A tool that automatically transfers your entire Shazam library to a new Spotify playlist. This is a fixed and improved version of the original [jclosadev/Shazam2Spotify](https://github.com/jclosadev/Shazam2Spotify) repository.

---

## What Was Fixed

The original `app.py` was obfuscated under 50 layers of encoding and contained several issues that made it broken out-of-the-box:

| Issue | Original | Fixed |
|---|---|---|
| Hardcoded file path | `C:\Users\jclos\Desktop\...` (developer's own machine) | Relative path + `--csv` argument |
| Hardcoded API credentials | Developer's own (likely revoked) keys | User provides their own credentials |
| No error messages | Silent failures | Clear error messages with setup instructions |
| Windows-only path | Backslash paths | Cross-platform (`os.path`) |
| No CLI arguments | None | `--csv`, `--name`, `--no-browser` flags |
| Duplicate handling | Basic | Proper deduplication with feedback |

---

## Installation

**1. Clone or download this repository**

```bash
git clone https://github.com/jclosadev/Shazam2Spotify.git
cd Shazam2Spotify
```

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

---

## Setup

### Step 1: Create a Spotify App

1. Go to [https://developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
2. Log in with your Spotify account
3. Click **"Create App"**
4. Fill in any name and description
5. Set the **Redirect URI** to exactly: `http://127.0.0.1:8888/callback`
   > ⚠️ **Important:** Spotify no longer accepts `localhost` as of April 2025. Use the explicit IP `127.0.0.1` instead — it works the same way but is accepted by the dashboard.
6. Click **Save**, then open your app and copy the **Client ID** and **Client Secret**

### Step 2: Configure Your Credentials

Open `shazam2spotify.py` and fill in your credentials near the top of the file:

```python
CLIENT_ID     = "your_client_id_here"
CLIENT_SECRET = "your_client_secret_here"
REDIRECT_URI  = "http://127.0.0.1:8888/callback"
```

Alternatively, you can use **environment variables** (recommended for security):

```bash
# Linux / macOS
export SPOTIPY_CLIENT_ID="your_client_id"
export SPOTIPY_CLIENT_SECRET="your_client_secret"
export SPOTIPY_REDIRECT_URI="http://127.0.0.1:8888/callback"

# Windows (Command Prompt)
set SPOTIPY_CLIENT_ID=your_client_id
set SPOTIPY_CLIENT_SECRET=your_client_secret
set SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback
```

### Step 3: Export Your Shazam Library

1. Go to [https://www.shazam.com/myshazam](https://www.shazam.com/myshazam)
2. Log in with your Shazam / Apple account
3. Click **"Download CSV"** on the right side
4. Place the downloaded file in the `library/` folder of this project

> **Note:** The CSV file should be named `shazamlibrary.csv` or you can pass a custom path using the `--csv` flag.

---

## Usage

```bash
# Basic usage (uses library/shazamlibrary.csv by default)
python shazam2spotify.py

# Specify a custom CSV path
python shazam2spotify.py --csv /path/to/your/shazamlibrary.csv

# Specify a custom playlist name
python shazam2spotify.py --name "My Shazam Discoveries"

# Don't open the browser when done
python shazam2spotify.py --no-browser
```

### First Run — Authentication

The first time you run the script, a browser window will open asking you to log in to Spotify and authorize the app. After authorizing, you will be redirected to `http://127.0.0.1:8888/callback` — the script will capture this automatically and save a `.cache` file so you won't need to log in again.

### Example Output

```
============================================================
  Shazam2Spotify — Fixed Version
============================================================

[1/4] Connecting to Spotify...
      Logged in as: Your Name (yourusername)

[2/4] Reading Shazam library from: library/shazamlibrary.csv
      Found 142 songs.

[3/4] Creating Spotify playlist: 'Shazam2Spotify'
      Playlist created with ID: 1LgH0v9zO9uPaWpba7maLd

[4/4] Searching and adding songs to playlist...
------------------------------------------------------------
  [1/142] ✓ Added:      Blinding Lights — The Weeknd
  [2/142] ✓ Added:      Someone Like You — Adele
  [3/142] ✗ Not found:  Some Obscure Track — Unknown Artist
  ...

============================================================
  Done!
  Songs processed : 142
  Songs added     : 138
  Duplicates      : 1
  Not found       : 3

  Playlist URL: https://open.spotify.com/playlist/1LgH0v9zO9uPaWpba7maLd
  Opening playlist in browser...
============================================================
```

---

## Troubleshooting

**"Spotify credentials are not configured"**
→ You need to fill in `CLIENT_ID` and `CLIENT_SECRET` in the script, or set the environment variables. See Setup above.

**"CSV file not found"**
→ Make sure your Shazam export is in the `library/` folder, or pass `--csv /path/to/file`.

**"INVALID_CLIENT: Invalid redirect URI"**
→ Make sure `http://127.0.0.1:8888/callback` is added as a Redirect URI in your Spotify app settings at [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard).

**Songs not found**
→ Some songs in your Shazam library may not be available on Spotify, or the search query may not match exactly. The script will list all not-found songs at the end.

---

## Credits

Original concept by [@jclosadev](https://github.com/jclosadev). This fixed version resolves compatibility and usability issues.
