# YT Music Playlist Janitor

A terminal tool for finding and cleaning duplicate songs in your YouTube Music liked songs.

It scans your YouTube Music liked-songs playlist, shows duplicate groups, writes backups, and can unlike duplicate copies after you confirm the cleanup plan.

## Duplicate rules

A song is treated as a duplicate when either rule matches:

- Same YouTube video ID.
- Same normalized song title and same YouTube Music artist/channel.

For each duplicate group, Playlist Janitor keeps the earliest playlist entry and removes later copies.

## Install

```bash
git clone https://github.com/andrew-gromyko/yt-music-playlist-janitor.git
cd yt-music-playlist-janitor
python3 playlist_janitor.py
```

## Usage

Run the interactive CLI:

```bash
python3 playlist_janitor.py
```

Controls:

- Arrow keys or `j` / `k`: move
- Enter: select
- `q`, Esc, or Backspace: return or cancel

Menu actions:

- **Smart scan**: fetch live liked songs and analyze duplicates
- **Show duplicates**: view duplicate groups in the terminal
- **Preview plan**: create a before-backup and cleanup plan without changing likes
- **Execute dedupe**: remove duplicates after typed confirmation
- **Setup OAuth**: save or replace Google OAuth credentials, authorize the account, then run a first scan

The main screen shows the latest local duplicate status and how long ago the last live scan ran. Before cleanup, the app refreshes the scan automatically if it is more than 20 minutes old.

## Google OAuth setup

Playlist Janitor uses your own Google Cloud OAuth client. It never asks for your Google password.

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Create or select a project.
3. Enable **YouTube Data API v3**.
4. Open **Google Auth Platform** / **OAuth consent screen**.
5. Set **User type** to **External**.
6. Keep **Publishing status** as **Testing**.
7. Add your Google account under **Test users**.
8. Create an OAuth client:
   - Application type: **TVs and Limited Input devices**
   - Copy the client ID and client secret
9. Run `python3 playlist_janitor.py`.
10. Choose **Setup OAuth** and paste the client values.
11. Complete the Google device-code authorization shown in the terminal.

After authorization succeeds, Playlist Janitor runs the first scan automatically.

The app requests this YouTube scope:

```text
https://www.googleapis.com/auth/youtube
```

That scope is needed because cleanup removes a duplicate like with the YouTube Data API `videos.rate` endpoint using `rating=none`.

## Common setup errors

`YouTube Data API v3 is disabled`

Enable YouTube Data API v3 for the same Google Cloud project that owns your OAuth client. Wait a few minutes and try again.

`org_internal`

Your OAuth app is restricted to a Google Workspace organization. Change the OAuth audience to **External**.

`access_denied` while the app is in testing

Add the Google account you are signing in with to **Test users**.

`invalid_client` or `unauthorized_client`

Check that the client ID and secret are from the same OAuth client, and that the client type is **TVs and Limited Input devices**.

`insufficient authentication scopes`

The saved authorization was created with a narrower scope. Run **Setup OAuth** again and authorize the app again.

## Safety

Cleanup is never automatic.

Before cleanup, Playlist Janitor saves and writes:

- `duplicate_report/full_dedupe_backups/<timestamp>/liked_music_before.csv`
- `duplicate_report/full_dedupe_backups/<timestamp>/dedupe_plan.json`

The final cleanup step requires typing:

```text
DEDUPE
```

On macOS, OAuth client values and refresh tokens are stored in Keychain. On other systems, they are stored in a user-only config file:

```text
~/.config/yt-music-playlist-janitor/credentials.json
```
