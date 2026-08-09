# Holiday Radio Matcher

Monitors holiday radio streams, identifies every song that airs, and turns them
into playlists — a Spotify playlist per station plus portable `.m3u8` files.

Built to run unattended on an UnRaid server. Songs it is confident about go
straight to the playlist; anything uncertain waits in a review queue, and every
choice you make there is remembered permanently.

![icon](app/web/icon.png)

---

## What it does

```
stream metadata ──▶ normalize ──▶ match ──▶ score ──▶ ┬── confident ──▶ playlist
                        │                             └── uncertain ──▶ review queue
                        └── jingle / station ID ──▶ filtered out            │
                                                                            ▼
                                                              your choice becomes a
                                                              permanent rule (learned)
```

* **Reads streams three ways** — AzuraCast's JSON API (best: separate artist and
  title fields, exact durations, and a 15-deep play history), Icecast's
  `status-json.xsl`, or raw inline ICY metadata. It picks the best one available
  automatically, so a 45-second poll never misses a track.
* **Filters station imaging** — jingles, IDs and promos are detected and never
  reach a playlist.
* **Matches against MusicBrainz and Spotify**, scoring artist, title and track
  length, then cross-checking the two databases against each other.
* **Learns from you** — confirming a match in the review queue writes a rule, so
  that song resolves instantly and offline forever after. Radio rotations repeat
  heavily, so the queue shrinks fast.

---

## Why the matching is accurate

Accuracy was the priority, so the engine is built around the specific ways
real stream metadata is wrong. Every example below is live data from Halloween Radio:

| Problem in the wild | How it is handled |
|---|---|
| `Elfman Danny` (surname first) | Both name orders are searched; token-set comparison makes ordering irrelevant |
| `The purple people eater` (case mangled) | Accent-, case- and punctuation-insensitive comparison keys |
| `Poor unfortunate souls ~ The Little Mermaid` | Decoration after `~` is stripped into a separate query variant |
| `album: "www.halloweenradio.net"` | Recognised as a watermark and discarded |
| `JINGLE — Halloweenradio.net 20-1` (16s) | Rejected as station imaging before any lookup |
| Karaoke and tribute re-recordings | Heavily penalised so they can never outrank the real thing |
| Live bootlegs scoring identically on text | Penalised; studio releases preferred |
| MusicBrainz rate-limiting mid-batch | Retried with backoff — **never** silently downgraded to a vague query |

The last row matters more than it looks. A failed lookup and a genuine "no such
song" are treated as completely different outcomes: a failure leaves the song
queued for a later retry rather than being recorded as unmatched.

**Confidence scoring** blends title (0.42), artist (0.38) and duration (0.20)
agreement. When a duration is unavailable the weight is redistributed rather
than guessed. Two independent databases agreeing raises confidence; a borderline
score triggers an ISRC cross-check that resolves identity exactly.

Defaults: **≥ 0.92** auto-accepted, **0.62–0.92** sent to review, below that
listed as unmatched. Both thresholds are adjustable in Settings.

---

## Install on UnRaid

The image is built automatically by GitHub Actions and published to GHCR, so
UnRaid pulls it directly — nothing is built on the server.

```
ghcr.io/tronvondoom/holiday-radio-monitor:latest
```

Published for `linux/amd64` and `linux/arm64`, and public — UnRaid pulls it
without any registry credentials.

### Install the template

Copy the template onto the server:

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/my-Holiday-Radio-Monitor.xml \
  https://raw.githubusercontent.com/TronVonDoom/Holiday-Radio-Monitor/main/unraid/holiday-radio-matcher.xml
```

Then **Docker → Add Container → Template: Holiday-Radio-Monitor**, set your paths,
and hit Apply. UnRaid pulls the image and the **WebUI** button opens the interface.

To add it without the template, use **Docker → Add Container** and set the
Repository to `ghcr.io/tronvondoom/holiday-radio-monitor:latest`.

### Updating

UnRaid's **Check for Updates** picks up new `:latest` pushes normally. Every push
to `main` rebuilds and republishes the image.

### Building it yourself instead

```bash
git clone https://github.com/TronVonDoom/Holiday-Radio-Monitor.git
cd Holiday-Radio-Monitor
docker build -t holiday-radio-monitor:latest .
```

| Setting | Default | Notes |
|---|---|---|
| WebUI Port | `8080` | The **WebUI** button opens this |
| Config Storage | `/mnt/user/appdata/holiday-radio-matcher` | Database + settings. Back this up |
| Playlists Folder | `/mnt/user/music/playlists` | Where `.m3u8` files are written |
| Access Token | *(blank)* | Optional. Set to require a token to open the UI |

The **WebUI** button is wired to `http://[IP]:[PORT:8080]/` and works as soon as
the container is running.

### Or with Docker Compose

```bash
docker compose up -d          # http://localhost:8080
```

---

## First run

The app seeds **Halloween Radio Main** and starts monitoring immediately — no
configuration needed to see it working.

1. **Add more stations** — *Stations → Discover*, enter
   `https://radio1.streamserver.link`, and add any of the 15 stations on that
   server in one click (6 Halloween, 3 Christmas, and others). Any other
   Icecast/SHOUTcast URL can be added manually, with a **Test** button that shows
   what is playing right now before you commit.

2. **Connect Spotify** (optional but recommended) — *Settings → Spotify*:
   - Create a free app at [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
   - Copy the **redirect URI** shown in Settings into the Spotify app's settings
   - Paste the Client ID and Secret into Settings and save
   - Click **Connect Spotify account**

   Without Spotify the app still works: it identifies songs through MusicBrainz
   and writes `.m3u8` files. Spotify adds playable links, real playlists, and a
   meaningful lift in match coverage.

3. **Work the review queue** — the badge in the sidebar shows how many songs are
   waiting. Each shows the stream metadata, ranked candidates, and *why* each
   scored the way it did. Confirming teaches the matcher permanently.

---

## The interface

| View | What it is for |
|---|---|
| **Dashboard** | Match rate, queue depth, what is on air across every station, recent plays, worker activity |
| **Review** | The queue, with ranked candidates, per-signal score breakdowns, and manual search |
| **Library** | Every song ever seen, filterable by status, searchable, sortable by lowest confidence |
| **Playlists** | What is being delivered per station, with links into Spotify and JSON export |
| **Stations** | Add, discover, test, pause and remove streams |
| **Settings** | Thresholds, providers, Spotify link, and your learned rules |

The UI re-skins itself per holiday — orange and purple for Halloween, red and
green for Christmas.

---

## Playlist output

**Spotify** — one private playlist per station, named `<Station> — Matched`,
created automatically. Sync is additive and reconciles against the real playlist
contents, so editing it by hand never causes duplicates.

**M3U** — one extended `.m3u8` per station in the playlists folder, rewritten
atomically:

```
#EXTM3U
#PLAYLIST:Halloween Radio Main
#EXTINF:132,The Doors - People Are Strange
#EXTALB:Strange Days
#EXTISRC:USEE10608399
https://open.spotify.com/track/...
```

Identified songs with no playable link are kept as `#EXT-X-UNRESOLVED` comments
rather than dropped, so a correct identification is never lost.

---

## Configuration reference

Environment variables (all optional — everything else is in the UI):

| Variable | Default | Purpose |
|---|---|---|
| `HRM_CONFIG_DIR` | `/config` | Database and settings |
| `HRM_PLAYLIST_DIR` | `/playlists` | Generated `.m3u8` files |
| `HRM_PORT` | `8080` | Web interface port |
| `HRM_AUTH_TOKEN` | *(blank)* | Require a token to open the UI |
| `HRM_SPOTIFY_REDIRECT_URI` | *(auto)* | Override when behind a reverse proxy |

Everything else — thresholds, poll interval, provider toggles, Spotify
credentials — is editable in Settings without restarting.

---

## Running locally

```bash
pip install -r requirements.txt
HRM_CONFIG_DIR=./data HRM_PLAYLIST_DIR=./playlists HRM_PORT=8080 python -m app.main
```

API docs are at `/api/docs`.

---

## Project layout

```
app/
  main.py          entrypoint, static UI, optional auth
  config.py        env configuration + setting defaults
  db.py            SQLite schema and access
  normalize.py     text cleanup, query expansion, jingle detection
  matcher.py       scoring and confidence engine
  monitor.py       poll / match / sync background loops
  sources.py       AzuraCast, Icecast and ICY readers
  playlists.py     Spotify sync and M3U writing
  api.py           REST API
  providers/       MusicBrainz and Spotify clients
  web/             the interface (no build step)
tools/make_icon.py regenerates the app icon
unraid/            UnRaid Community Applications template
```

---

## Notes and ideas

* **Back up `/config`.** It holds the database, your Spotify link, and — most
  valuably — every matching rule you have taught it.
* **MusicBrainz is rate-limited to ~1 request/second** by design. A large backlog
  drains steadily rather than all at once; this is deliberate and polite.
* **Worth adding later:** audio fingerprinting (AcoustID/Chromaprint) for the
  genuinely unidentifiable tracks — obscure indie songs that exist in no
  database. It needs real audio sampling, so it is a heavier feature, but it is
  the natural next step if the review queue ever has a stubborn tail. The
  provider interface is structured to accept it as another source.
* **Adding another holiday** is just adding a station; set its *Holiday* field to
  pick up the matching accent colour in the UI.
