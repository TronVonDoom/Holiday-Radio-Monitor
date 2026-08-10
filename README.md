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
* **Matches against four catalogues** — MusicBrainz, Spotify, Deezer and Apple
  Music — scoring artist, title and track length, then cross-checking them
  against each other. Only Spotify needs an account.
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
| A song two catalogues have never heard of | Two more are asked; they disagree far more often than you would expect |
| A keyword search returning its closest guess for a song that isn't there | Rejected on artist disagreement, so noise cannot reach the queue |
| Any provider rate-limiting mid-batch | That provider pauses, matching continues on the others — **never** silently downgraded to a vague query |

The last row matters more than it looks. A failed lookup and a genuine "no such
song" are treated as completely different outcomes: a failure leaves the song
queued for a later retry rather than being recorded as unmatched. And when a
song genuinely cannot be found, the queue says which catalogues answered and
which could not be reached — so archiving something is a decision rather than a
guess.

### Four catalogues, and why

| Provider | Account needed | What it is for |
|---|---|---|
| **MusicBrainz** | no | Canonical identity: stable MBIDs, careful editorial data, no commercial bias |
| **Spotify** | yes | The playable one — a match needs a Spotify URI to reach a Spotify playlist |
| **Deezer** | no | Coverage, and the only one that returns an **ISRC in its search results** |
| **Apple Music** | no | The long tail: seasonal compilations, novelty singles and small-label reissues |

The last two were added because "unmatched" was hiding two completely different
problems — metadata too mangled to search on, and songs those first two
catalogues simply do not carry. Only the second is fixed by asking somebody
else, and holiday radio leans hard on exactly the material where MusicBrainz
thins out and where Spotify and Deezer, which ingest from the same modern
distributors, tend to agree with each other about nothing being there. Neither
addition needs credentials.

Deezer's ISRC earns its keep twice: cross-catalogue identity becomes an exact
assertion rather than an inference, and a song Deezer identifies can be looked
up on Spotify **by recording code** instead of by another fuzzy text search — so
it reaches the playlist even when Spotify's own search could not find it.

Apple Music is the one provider with no fielded query syntax: it answers keyword
relevance and never says "nothing here", so it always returns its closest guess.
That is precisely why it is worth asking after a fielded search has failed, and
precisely why a candidate whose performer has nothing in common with the one the
stream named is rejected outright. Every title comparison here deliberately
tolerates a missing subtitle — that tolerance is what repairs stream metadata —
and the same tolerance rates a title *fragment* highly. The artist is what
separates the two.

### Staying inside the providers' rate limits

All four throttle, for different reasons. MusicBrainz allows one request per
second per client and defends its search endpoint more aggressively than a plain
lookup; Spotify enforces a rolling-window quota per *application*, so its
throttling follows the matcher rather than the machine it runs on; Apple Music
allows roughly twenty requests a minute per IP and reports the limit as a bare
`403`; Deezer allows about fifty requests every five seconds and — the trap —
reports a refusal as **HTTP 200 with an error in the body**, so a client that
only checks the status code reads a quota rejection as "this song does not
exist" and records it as permanently unmatched.

Four things keep this app a well-behaved citizen of all of them:

- **Spacing.** MusicBrainz and Apple Music each get a global lock that
  serialises every call and measures the gap from the *end* of the previous
  request, so the real rate stays inside the budget no matter how many stations
  are monitored. Spotify and Deezer are two orders of magnitude inside their
  limits at the volume this app generates, so they are throttled on refusal
  rather than spaced.
- **A meaningful User-Agent**, with a contact URL, as MusicBrainz policy requires.
- **Honouring backpressure.** A `503` or `429` opens a per-provider cooldown: no
  further requests are sent until it expires, `Retry-After` leads whenever it is
  longer than our own floor, and repeated throttling escalates the pause. A
  refusal also abandons the remaining query spellings, because they would each be
  refused too. How far the pause escalates depends on how much the service tells
  us: MusicBrainz and Apple Music name no delay, so their cooldowns step up to
  3×, 10× then 30×; Spotify does, so its own figure leads and its floor only
  steps up to 2×, 4× then 8×. Deezer names none either, but its quota window is
  only five seconds wide, so it escalates gently for the same reason Spotify
  does — a long guess there would be pure dead time.
- **Not being parked for a day.** A single pause is capped — 5 minutes for
  Deezer, 15 for Spotify, 30 for MusicBrainz and Apple Music, all at or above
  the top escalation step so the
  cap never shortens our own backoff. It exists because a service can ask for far
  longer: Spotify answers an application that has broken its *longer-window*
  quota with a `Retry-After` measured in hours, and obeying that literally takes
  matching and playlist delivery out for the rest of the day with no way to
  notice it has recovered. Capping costs one refused request per cap period and
  resumes on its own the moment the ban lifts. The Activity log says when a wait
  was capped and what was actually asked for.
- **Degrading instead of stalling.** A cold provider means matching continues on
  the remaining three; the dashboard names whichever are paused and offers
  **Resume now** for each, and the Activity log records it. Confidence is a
  little lower with fewer databases corroborating, so expect more items in
  review while it lasts — but with four catalogues, losing one is now a dent
  rather than a halving. A cooldown is only a prediction about when the service will accept us
  again — if you know better, resuming skips the wait, and the next refusal
  simply opens a new one. Cooldown state is in-memory by design, so restarting
  the container also clears it.

Query volume is kept low for the same reason: a confident hit on the first
spelling ends the search, so a recognisable song costs exactly one request, and
each search has a wall-clock budget so a slow provider cannot hold the queue.

A match identified without Spotify still needs a Spotify URI to reach a
playlist. If Spotify is throttled at that moment the match is kept anyway, and a
healing pass in the sync loop attaches the link once Spotify answers again.

**Confidence scoring** blends title (0.42), artist (0.38) and duration (0.20)
agreement. When a duration is unavailable the weight is redistributed rather
than guessed. A borderline score triggers an ISRC cross-check that resolves
identity exactly — usually for free, because Deezer already handed the code over.

When several catalogues return the same recording it becomes **one** candidate,
not four: the agreement raises its confidence, and the merged row carries
MusicBrainz's MBID alongside Spotify's playable URI and artwork. So the ranked
list is a list of genuinely different answers, and an identification made
elsewhere often arrives playable without a second lookup.

How much the agreement is worth scales with how many agree — **+0.07** for the
second catalogue, **+0.10** for the third, **+0.12** for the fourth. It is
deliberately not proportional: the streaming catalogues ingest from overlapping
distributors, so Spotify, Deezer and Apple agreeing is not three independent
opinions.

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
   - Create a free app at [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard),
     making sure **Web API** is ticked when you choose which APIs the app uses
   - Copy the **redirect URI** shown in Settings into the Spotify app's settings
   - Paste the Client ID and Secret into Settings and save
   - Click **Connect Spotify account**
   - Click **Test Spotify access** to confirm the link can really write playlists

   Without Spotify the app still works, and works well: MusicBrainz, Deezer and
   Apple Music all identify songs without an account, and `.m3u8` files are
   written either way. What Spotify adds is playable links and real playlists.

   <details>
   <summary>If syncing fails with <code>403 Forbidden</code></summary>

   Versions before v1.0.1 created playlists through `POST /users/{user_id}/playlists`.
   Spotify withdrew that endpoint from Development Mode apps in its
   [February 2026 Web API changes](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide),
   enforced for existing apps on **9 March 2026**, and it now answers a bare 403
   for every caller with no indication that the endpoint itself is the problem.
   v1.0.1 moved to `POST /me/playlists`; **update the container** and it works again.

   If a 403 survives the update, **Test Spotify access** walks the whole delivery
   path — token refresh, account read, granted permissions, playlist read, and a
   real playlist create/remove — and names the step that fails. What is left to
   check:

   - **The app is still in Development mode** and the Spotify account is not on
     its user list. Add it under *Dashboard → your app → Settings → User
     Management*, using the account's email exactly.
   - **The app was created without the Web API product.** Enable Web API in the
     app's settings, or create a new app with it ticked.
   - **The saved login predates the permissions playlists need.** Disconnect and
     reconnect; the Settings page flags this on its own once it knows the granted
     scopes.
   </details>

3. **Work the review queue** — the badge in the sidebar shows how many songs are
   waiting. Each shows the stream metadata, a prefilled search box, the four
   strongest candidates, and *why* each scored the way it did; weaker ones are
   one click away under **Show more**. Correcting a field and pressing Enter
   searches every enabled catalogue at the same time. Confirming teaches the
   matcher permanently. Anything you would rather not decide on yet can be
   **archived**: it leaves the queue without a verdict and waits in the Library
   under *archived*, where **Restore** puts it back exactly as you left it.

---

## The interface

| View | What it is for |
|---|---|
| **Dashboard** | Match rate, queue depth, what is on air across every station, recent plays, worker activity |
| **Review** | The queue: search at the top, then ranked candidates with per-signal score breakdowns. Resolve, archive for later, or mark as imaging |
| **Library** | Every song ever seen, filterable by status, searchable, sortable by lowest confidence — and where the archive lives |
| **Playlists** | What is being delivered per station, with links into Spotify, JSON export, and duplicate cleanup |
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

### One song, one entry

A song is identified by its **normalized artist and title** — case, accents,
punctuation and leading articles removed — and by nothing else. So
`The purple people eater` and `The Purple People Eater` are one song in the
library, matched once, reviewed once and delivered once.

Earlier versions preferred the source's own identifier when one was offered,
which sounds authoritative and was the opposite of helpful: AzuraCast hashes the
raw metadata text, so every spelling of a song became a separate identity — and
the normalization built to see through exactly that was skipped whenever an
identifier existed. Stations reached over plain Icecast supply no identifier at
all, and two AzuraCast servers share no identifier namespace, so the same song
could be several rows for several unrelated reasons.

**Existing databases repair themselves on first start.** Duplicate rows are
merged into the furthest-along copy: plays keep their history, playlist
membership keeps the earliest join date, and a track already delivered to
Spotify stays marked as delivered so it is never sent twice.

Delivery is deduplicated by *recording* rather than by row, too. Two songs that
resolve to the same track — different spellings on different days — put one
entry in the playlist, not two.

If a playlist already collected duplicates under the old behaviour, **Playlists
→ Remove duplicates** cleans it. Spotify's removal endpoint clears every copy of
a track at once, so each duplicated track is removed and re-added once, which
moves it to the end of the playlist; tracks appearing only once are never
touched. It runs when you ask rather than during a sync, for that reason.

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

Four catalogue toggles — MusicBrainz, Spotify, Deezer and Apple Music — are in
*Settings → Matching*. All are on by default; only Spotify needs an account, so
switching any of the other three off costs coverage and buys nothing.

Each provider's pacing is tunable too. These have sensible defaults and are
worth touching only if a service is persistently unhappy with you:

| Setting | Default | Purpose |
|---|---|---|
| MusicBrainz rate limit (seconds) | `1.1` | Minimum gap between MusicBrainz requests |
| MusicBrainz cooldown (seconds) | `60` | Pause after a `503`/`429`, escalating to 3×, 10×, 30× |
| Spotify cooldown (seconds) | `10` | Floor for Spotify's per-application quota, escalating to 2×, 4×, 8× |
| Deezer cooldown (seconds) | `30` | Pause after a quota refusal, escalating to 2×, 4×, 8× |
| Apple Music rate limit (seconds) | `3.0` | Minimum gap; the store allows about 20 requests a minute |
| Apple Music cooldown (seconds) | `60` | Pause after a `403`, escalating to 3×, 10×, 30× |

They differ because the services do. MusicBrainz and Apple Music never say how
long to wait, so the pause is a guess and guessing long is the safe direction.
Spotify states its own `Retry-After` and that is honoured in full — its setting
is only a floor to stop a tight retry loop. Deezer says nothing either, but its
quota window is five seconds wide, so it is treated the gentle way for the same
reason.

Raising a cooldown is the right response to persistent throttling; lowering
either rate limit is not, and will get the IP blocked.

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
  providers/       the four catalogue clients + the shared registry they
                   are all reached through (__init__.py)
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
* **Adding a fifth catalogue** is one module and one line. Implement `search`,
  `is_configured`, `status`, `resume`, `cooldown_remaining` and `aclose`, return
  the shared candidate shape, then add a row to `REGISTRY` in
  `app/providers/__init__.py` — the matcher's fan-out, the manual search, the
  settings toggle, the dashboard's paused-provider banner and its **Resume now**
  button all read that registry rather than a list of their own.
* **What is left in the tail.** With four catalogues answering, a song none of
  them has is usually either metadata too mangled to search on or something
  never released commercially — station-produced content, a YouTube-only novelty
  cut. The unmatched reason now names which catalogues answered and which could
  not be reached, so the two are told apart before you archive anything. What
  would genuinely help the residue is audio fingerprinting
  (AcoustID/Chromaprint), which identifies from the sound rather than from the
  text; it needs real audio sampling, so it is a heavier feature, but it slots
  in as another provider.
* **Adding another holiday** is just adding a station; set its *Holiday* field to
  pick up the matching accent colour in the UI.
