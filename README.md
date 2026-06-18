# Shazam2Spotify

Transfer your entire Shazam library to a Spotify playlist via a local web UI.

---

## Features

- **Playlist picker** — select an existing Spotify playlist or create a new one from a tab UI
- **Smart re-transfers** — pre-fetches all existing playlist tracks on start and skips the Spotify search for any song already present; re-running on a complete playlist takes seconds instead of minutes
- **Live progress** — per-song status log streamed in real time
- **Duplicate handling** — skip duplicates, skip + clean the playlist afterwards, or no filtering
- **Export Report** — after transfer, download a CSV of every song with its result status, matched Spotify title/artist, and notes
- **Export Not Found** — download a CSV of only songs Spotify couldn't find, ready to use as a separate playlist source
- **Dark web UI** — no command line needed after startup; credentials and preferences saved to `config.json`

---

## Requirements

- Python 3.9+
- A free Spotify developer app

---

## Installation

### Linux

```bash
git clone https://github.com/dairyking98/Shazam2Spotify.git
cd Shazam2Spotify
chmod +x install.sh
./install.sh
```

Then start the app any time with:

```bash
./run.sh
```

### Windows

1. Install [Python 3.9+](https://www.python.org/downloads/) — check **"Add Python to PATH"** during setup
2. Clone or download this repo and open the folder
3. Double-click **`install.bat`**

Then start the app any time by double-clicking **`run.bat`**.

---

Open **http://127.0.0.1:5000** in your browser after starting the app.

---

## Setup

### 1. Create a Spotify App

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
2. Log in and click **Create App**
3. Fill in any name and description
4. Under **Redirect URIs**, add exactly: `http://127.0.0.1:5000/callback`
5. Check **Web API** under "Which API/SDKs are you planning to use?"
6. Click **Save**, then copy your **Client ID** and **Client Secret**

> Spotify no longer accepts `localhost` as a redirect URI (banned April 2025). Use `127.0.0.1` with the port number shown above.

### 2. Enter credentials in the UI

Paste your Client ID and Client Secret into **Step 1** of the web interface and click **Save Credentials**. They are written to `config.json` and loaded automatically on every restart.

### 3. Export your Shazam library

1. Go to [shazam.com/myshazam](https://www.shazam.com/myshazam)
2. Log in with your Apple / Shazam account
3. Click **Download CSV** on the right side

### 4. Connect your Spotify account

Click **Connect Spotify Account** in Step 2. A Spotify login window opens. After you approve access, the status turns green and the playlist picker loads your playlists.

### 5. Run a transfer

1. Drag and drop your Shazam CSV into **Step 3**
2. In **Step 4**, choose your target playlist and options
3. Click **Start Transfer** and watch the live progress log

---

## Transfer Options

### Playlist

| Tab | Behaviour |
|---|---|
| **Existing** | Pick from a dropdown of all your Spotify playlists. The app pre-selects the last-used playlist by name. |
| **New** | Type a name; the app creates the playlist. The **Public playlist** toggle only appears here. |

### Duplicate handling

| Option | Behaviour |
|---|---|
| **Skip duplicates** (default) | Songs already in the playlist, or appearing more than once in the CSV, are skipped |
| **Skip + clean playlist after** | Same as above, then scans the entire playlist and removes any remaining duplicate tracks. **Permanent — cannot be undone.** |
| **No filtering** | Adds everything, including songs already in the playlist |

### Delay between requests

Controls the pause between each Spotify search call (default 500 ms). Lower values are faster but may trigger rate limiting. Increase to 1000 ms or more if you see 429 errors.

---

## After Transfer

Three buttons appear when the transfer completes:

| Button | File | Contents |
|---|---|---|
| **Open Playlist in Spotify** | — | Opens the playlist directly |
| **Export Report** | `shazam2spotify_report.csv` | All songs with original Shazam columns + Transfer Status, Spotify Title, Spotify Artist, Notes |
| **Export Not Found** | `shazam2spotify_not_found.csv` | Only songs Spotify could not find — ready to drop into another tool or manually search |

---

## Config file

`config.json` is created automatically on first run and loaded on every startup:

```json
{
  "client_id": "",
  "client_secret": "",
  "redirect_uri": "http://127.0.0.1:5000/callback",
  "playlist_name": "Shazam2Spotify",
  "public_playlist": true,
  "dupes_mode": "skip",
  "delay_ms": 500
}
```

`dupes_mode` accepts `"skip"`, `"remove"`, or `"none"`.

`config.json` is tracked in git with **blank credentials**. Never commit a version that contains real values. The `.cache` file (Spotify auth token) is listed in `.gitignore` and is never committed.

---

## Debug mode

```bash
# Linux
./run.sh --debug

# Windows
run.bat --debug
```

Logs every request path and JSON body to the terminal, and prints full tracebacks on errors.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Redirect URI mismatch" error from Spotify | Make sure `http://127.0.0.1:5000/callback` is saved in your Spotify app's Redirect URIs (exact match, including the port) |
| "Spotify credentials are not configured" | Fill in Client ID and Secret in Step 1 and click Save |
| Transfer shows no progress after starting | Restart the server; run with `--debug` to see what the error is |
| 429 rate limit errors | Increase the delay to 1000 ms or more in Step 4 |
| Songs not found | Some tracks may not be available on Spotify in your region. Use **Export Not Found** to get the full list. |
| `INVALID_CLIENT` or auth loop | Delete `.cache` from the project folder and reconnect |

---

## Credits

Original concept by [@jclosadev](https://github.com/jclosadev). This version adds a full web interface, playlist picker with pre-screening, transfer report exports, and fixes all Spotify Feb 2026 API changes.
