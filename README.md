# OpenTune Sync

Local web app that syncs your OpenTune (backup file) or a Spotify/CSV playlist export to YouTube Music — matching songs, creating playlists, and uploading them via the YouTube Data API.

## Requirements

- Python 3.8+
- pip
- A free Google Cloud project with the YouTube Data API v3 enabled (the app walks you through creating one)

## Setup & Run

From the project root:

```
python3 setup.py
```

This checks your Python version, installs Flask if missing, and launches the app. It opens automatically at `http://localhost:8080`.

Alternatively, run the app directly — it self-installs Flask on first launch:

```
python3 1786653076755_opentune_sync.py
```

## First-time configuration

On first launch, the Setup tab walks you through:

1. Creating a free Google Cloud project.
2. Enabling the YouTube Data API v3.
3. Creating OAuth credentials (Desktop app type) and adding every Google account you plan to sync as a **Test user** — required while the project is in Testing mode, otherwise Google blocks the login.
4. Pasting the downloaded credentials JSON (or entering Client ID/Secret manually).
5. Connecting your Google account.

You can add multiple Google Cloud projects/accounts and switch which one is active, or connect several at once to multiply your daily upload quota.

## Using it

1. **Setup** — load an OpenTune `.backup` file, or import a CSV playlist export (e.g. from Exportify).
2. **Songs** — browse your library, search/filter, select songs to sync. For CSV imports, match each song to a YouTube video (auto-match with confidence scoring, or pick manually).
3. **Playlists** — browse your backup's playlists and your connected YouTube account's playlists.
4. **Sync** — three modes:
   - **Map Existing**: pair a backup playlist to an existing YouTube playlist.
   - **Create New**: create new YouTube playlists from backup playlists.
   - **Custom Songs**: sync hand-picked songs to an existing or brand-new YouTube playlist.
5. **Progress** — track running/paused/failed jobs. Jobs checkpoint after every song, auto-pause on quota limits, and can be resumed later without re-uploading anything already added.

## Data & storage

Everything is stored locally under `~/.opentune_sync/`:

- `config.json` — saved Google Cloud credentials and app settings
- `tokens_<n>.json` — OAuth tokens per connected account
- `workdir/` — extracted backup files
- `jobs/` — persisted sync job state (for resume/checkpoint support)

Each user account on each machine gets its own independent `~/.opentune_sync/` — sharing this project with someone else just means giving them the files; their setup and library stay fully separate from yours.

## Quota notes

The YouTube Data API free tier allows roughly 200 playlist-song-additions per day per Google Cloud project. Connecting multiple projects/accounts multiplies your daily capacity; large syncs are automatically split across days and resumed via the Progress tab.

## Troubleshooting

- **"Access blocked" during Google sign-in** — the account isn't added as a Test user on your OAuth consent screen yet (Setup → step 3).
- **Sync job stuck on quota_exceeded** — wait for the daily reset (midnight Pacific Time) or add another Google Cloud project, then click Resume.
- Logs for the setup/launch script are written to `<project_root>/logs/`.
