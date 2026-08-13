# OpenTune Sync

Sync your music library to YouTube Music — from an OpenTune `.backup` file, or from any CSV playlist export (Spotify via [Exportify](https://exportify.net/), or similar).

Runs entirely on your own machine. No accounts, servers, or data pass through anything but your browser, this script, and Google/YouTube's API.

## Requirements

- Python 3.8+
- A Google Cloud project with the YouTube Data API v3 enabled (the app walks you through this on first run)

No manual `pip install` needed — the script installs Flask itself on first launch.

## Running it

```bash
python3 opentune_sync.py
```

This opens `http://localhost:8080` in your default browser. Everything — server and UI — shuts down together via the **⏻ Exit** button in the header, or `Ctrl+C` in the terminal.

## Setup

### ① Import Your Library

Pick one of two paths:

- **OpenTune Backup** — upload a `.backup` file directly, or paste a filesystem path to it (useful for very large backups you'd rather not re-upload through the browser). The file is copied locally, extracted, and read as SQLite — your original is never modified.
- **Playlist File (CSV)** — a 3-step flow: export a playlist to CSV (Exportify is embedded inline, one click away), upload the CSV, then head to the Songs tab to match songs to YouTube videos.

You can switch between the two at any time with "← Change method."

### ② YouTube Projects

Connects your Google account via OAuth so the app can create and edit playlists on your behalf. A guided wizard walks you through creating a free Google Cloud project, enabling the YouTube Data API, and generating credentials.

**Quota note:** YouTube's API gives each Google Cloud project ~200 song-adds/day on the free tier. Add multiple projects here and the app rotates between them automatically when one hits its daily limit — jobs pause and resume seamlessly rather than failing.

## Songs tab

Browse, search, sort, and select songs from your loaded backup. Selected songs carry over to **Sync → Custom Songs**.

### Playlist File Import (CSV path)

If you imported a CSV, its songs show up here too, each needing a YouTube match before they can sync (a CSV file only has title/artist/album — no YouTube video ID):

- **Find on YouTube** — search and manually pick the right video per song.
- **⚡ Match All** — auto-matches every song against YouTube search using a title/artist similarity score, only falling back to manual picking for low-confidence matches. Runs with a visible "N of M" progress indicator so it's clear it's working in the background.
- **Fast match** toggle — on by default, controls whether Match All auto-picks or always prompts manually. Synced between the Songs tab and Setup.
- **▶ Preview** — play just the audio of any candidate before committing, with a seek bar and Start/Mid/End jump buttons.

## Sync tab

Three modes:

- **Map Existing** — link a backup playlist to an existing YouTube playlist; songs get added to it.
- **Create New** — pick backup playlists to upload as brand-new YouTube playlists (with a privacy setting).
- **Custom Songs** — sync whatever's currently selected (from the backup library or CSV-matched songs) into one destination playlist.

Every mode has a **Preview** button to see what's new before committing, and skips anything already on YouTube automatically.

## Progress tab

Live status for every sync job — rate, ETA, failures, and quota-rotation events. Jobs checkpoint after every single song, so a crash, quota limit, or manual pause never loses progress; **Resume** picks up exactly where it left off, even across app restarts.

## Notes

- All config and job state lives in `~/.opentune_sync/` (`config.json`, per-credential tokens, and per-job JSON files).
- Nothing is ever silently synced — Custom Songs, Create New, and Map Existing all require an explicit selection or mapping before Start Sync is enabled.
- The Exportify embed and its "Open in new tab" fallback both point to `https://exportify.net/` — if Spotify's login page refuses to render inside the embed (a restriction Spotify's login page sets, not something this app controls), use the fallback link.
