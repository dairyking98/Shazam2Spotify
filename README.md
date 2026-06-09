# Shazam2Spotify — Web Interface

A web-based tool that transfers your entire Shazam library to a new Spotify playlist. Features a clean dark UI, config file for credentials, CSV drag-and-drop, live transfer progress, and full options control.

![Shazam2Spotify](https://github.com/jclosadev/Shazam2Spotify/blob/master/header.jpg?raw=true)

---

## Features

- **Web UI** — runs locally in your browser, no command line needed after startup
- **Config file** — credentials saved to `config.json` on first use, loaded automatically on restart
- **CSV drag & drop** — upload your Shazam export directly in the browser
- **Live progress** — real-time log of every song being added
- **Options** — playlist name, public/private, skip duplicates, request delay, open browser on finish
- **Cross-platform** — works on Windows, macOS, and Linux

---

## Installation

```bash
git clone https://github.com/dairyking98/Shazam2Spotify.git
cd Shazam2Spotify
pip install -r requirements.txt
python web_app.py
```

Then open **http://127.0.0.1:5000** in your browser.

---

## Setup (one-time)

### 1. Create a Spotify App

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
2. Log in and click **Create App**
3. Fill in any name and description
4. Set the **Redirect URI** to: `http://127.0.0.1:8888/callback`
   > ⚠️ Spotify no longer accepts `localhost` (banned April 2025). Use `127.0.0.1` with the port number.
5. Check **Web API** under "Which API/SDKs are you planning to use?"
6. Click **Save**, then copy your **Client ID** and **Client Secret**

### 2. Enter credentials in the web UI

Paste your Client ID and Client Secret into Step 1 of the web interface and click **Save Credentials**. They are stored in `config.json` and loaded automatically next time.

### 3. Export your Shazam library

1. Go to [shazam.com/myshazam](https://www.shazam.com/myshazam)
2. Log in with your Apple/Shazam account
3. Click **Download CSV** on the right side

### 4. Run the transfer

1. Click **Connect Spotify Account** (Step 2) — a login window opens
2. Drag & drop your CSV into Step 3
3. Set your options in Step 4 (playlist name, public/private, etc.)
4. Click **Start Transfer** and watch the live progress

---

## Config file

Credentials and preferences are saved to `config.json` in the project folder:

```json
{
  "client_id": "your_client_id",
  "client_secret": "your_client_secret",
  "redirect_uri": "http://127.0.0.1:8888/callback",
  "playlist_name": "Shazam2Spotify",
  "open_browser": true,
  "public_playlist": true,
  "skip_duplicates": true,
  "delay_ms": 500
}
```

> **Note:** `config.json` and `.cache` (Spotify auth token) are listed in `.gitignore` so your credentials are never accidentally committed.

---

## CLI (optional)

If you prefer the command line, `shazam2spotify.py` is still included:

```bash
python shazam2spotify.py --csv library/shazamlibrary.csv --name "My Playlist"
```

---

## Troubleshooting

| Error | Fix |
|---|---|
| `localhost` not accepted in Spotify dashboard | Use `http://127.0.0.1:8888/callback` (with port) |
| "Spotify credentials are not configured" | Fill in Client ID & Secret in Step 1 and click Save |
| Songs not found | Some tracks may not be on Spotify; they're listed at the end |
| Rate limit errors | Increase the delay in Step 4 options |

---

## Credits

Original concept by [@jclosadev](https://github.com/jclosadev). This version adds a full web interface and fixes all compatibility issues.
