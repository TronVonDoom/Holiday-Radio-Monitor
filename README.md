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
* **Matches against three catalogues** — MusicBrainz, Deezer and Apple Music —
  scoring artist, title and track length, then cross-checking them against each
  other. None of them needs an account. Spotify is the *destination* rather than
  a fourth opinion: once a song is identified, Spotify is asked for its link by
  exact recording code, which is one request instead of five guesses.
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
| A real song the imaging filter caught anyway | **It's a song** in the Library overturns it permanently — see below |
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

### When the imaging filter is wrong

The non-song filter has to lean towards rejecting, because one jingle in a
playlist is worse than one song in the review queue. So it will catch real music
sometimes — a soundtrack cue under the minimum length, a station that leaves the
artist field as `Various`, a title that happens to look like an imaging filename.

*Library → nonsong → **It's a song*** overturns it. The song goes straight back
through matching and lands wherever its confidence puts it.

The important half is that it stays overturned. The filter is deterministic, so
simply re-running the matcher reached the same verdict every time and the song
was stuck there permanently. So the button also writes a rule — one that carries
no identity, unlike a confirmation, and does nothing but disarm the filter for
that exact artist and title. Rules are listed in *Settings → Learned rules* as
*always treated as music*, and deleting one puts the song back under the filter.

Each filtered song records *why* it was filtered; hover the button to see it. If
the reason is the track length, the minimum is in Settings rather than something
to overturn song by song.

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

- **Spacing.** All four get a global lock that serialises every call and measures
  the gap from the *end* of the previous request, so the real rate stays inside
  the budget no matter how many stations are monitored.

  Spotify and Deezer were once exempt, on the
  reasoning that the app's daily total sat far inside the quota. That was true
  and irrelevant: the quota is a *rolling window* tens of seconds wide, so what
  gets refused is the burst, not the total. Resolving one song Spotify cannot
  find costs about eleven requests — five query spellings, an ISRC lookup, then
  the same again to attach a playable link — and the match loop runs eight songs
  a batch, which unpaced is most of a hundred requests inside a couple of
  seconds. A backlog is what turns that from a spike into sustained traffic, and
  a backlog is exactly what an outage leaves behind: the stations' play history
  backfills all at once when polling resumes.

  Deezer genuinely does have headroom — about 50 requests per 5 seconds — but it
  is spaced anyway, at a tenth of a second, because that costs nothing and
  removes the assumption rather than re-testing it.
- **Watching the send rate, not just the refusals.** Every provider reports how
  many requests it has actually received in the last minute and the last hour,
  and the dashboard shows the total with the busiest services named. A cooldown
  is the symptom and arrives too late to act on; this is the cause, and it is the
  only number that can be checked against a published budget while there is still
  time to turn something down.
- **Spending Spotify to a budget, not just a gap.** Watching a number only helps
  if something acts on it, and nothing did: the hourly count was reported and
  then ignored, so the first thing that ever enforced a sustained ceiling was
  Spotify banning the application for the better part of a day. Spotify's calls
  are now drawn from an hourly budget as well as spaced — a token bucket, so the
  sustained rate *is* the budget while a short burst still runs at full speed,
  rather than a count per clock hour whose cheapest use is a burst the moment it
  resets. Running into it slows the loops down; it does not refuse them, until
  the budget is set tight enough that waiting would be worse than falling back to
  another catalogue.
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
- **Not being parked forever.** A single pause is capped — 5 minutes for Deezer,
  30 for MusicBrainz and Apple Music, 6 hours for Spotify — all at or above the
  top escalation step so the cap never shortens our own backoff. It exists
  because a service can ask for far longer than we can verify, and obeying an
  unbounded number takes the provider out with no way to notice it has recovered.

  Spotify's cap used to be 15 minutes, which was too aggressive to be safe. When
  Spotify refuses an application that has broken its *longer-window* quota it
  sends a `Retry-After` measured in hours, and that figure is real — it counts
  down against a fixed unblock time, so nothing the app does shortens it.
  Capping it to 15 minutes did not get us back sooner; it just meant probing a
  service that had said "not for another eighteen hours" seventy-odd times, and
  a `429` is still a request the quota counts. Six hours keeps the reason the cap
  exists at three probes across a day-long ban instead of seventy. The Activity
  log says when a wait was capped and what was actually asked for.
- **Remembering across restarts.** A cooldown is written to the database, so a
  deploy, a crash or a machine coming back from a power cut does not walk
  straight back into the same refusal. A service that tells us to wait hours is
  saying something about this *application*, not about this run of it. Before
  this, every restart probed immediately and began a fresh escalation streak —
  which is how an app being punished for a burst kept re-announcing itself to
  the service punishing it.
- **Degrading instead of stalling.** A cold catalogue means matching continues on
  the remaining two; the dashboard names whichever are paused and offers
  **Resume now** for each, and the Activity log records it. Confidence is a
  little lower with fewer databases corroborating, so expect more items in
  review while it lasts. A cold *Spotify* is a different outage and the dashboard
  says so: identification is untouched, because the match loop never asks it —
  what waits is playlist delivery, and it resumes on its own.
  A cooldown is only a prediction about when the service will accept us
  again — if you know better, **Resume now** skips the wait, and the next refusal
  simply opens a new one. Restarting the container no longer clears it: that is
  what the persistence above is for, and it is why resuming is a deliberate
  button rather than a side effect of a deploy.

Query volume is kept low for the same reason: a confident hit on the first
spelling ends the search, so a recognisable song costs exactly one request, and
each search has a wall-clock budget so a slow provider cannot hold the queue.

### Being a good guest on the stations' own servers

The four catalogues are large companies with published quotas. The stations are
not: they are somebody else's AzuraCast install, and there is no support address
to appeal an IP ban to. Losing a catalogue costs some matching confidence.
Losing the metadata source costs everything, because there is nothing left to
match.

So stations are polled **per server, not per station**. A network is normally
several mounts on one host, and AzuraCast's `/api/nowplaying` returns every
station on the server in a single cached document — the same content the
per-station endpoint gives, and the endpoint AzuraCast itself recommends for
polling. Six stations across two servers cost two requests per cycle rather than
six, and no host ever sees more than one request from us at a time. A server that
does not expose the unscoped endpoint falls back to per-station polling, done
serially rather than all at once, and is not re-probed for half an hour — a
fallback that costs a failed request every 45 seconds is not a fallback.

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

Setting an access token puts a lock screen in front of everything except the
health probe and Spotify's OAuth callback, neither of which can carry one. The
token is compared in constant time, and a token supplied in the URL is exchanged
for a cookie and immediately redirected away — a credential in an address bar
ends up in browser history, in the referrer header and in the access log of every
proxy between you and the container.

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
| **Dashboard** | Match rate, queue depth, what is on air across every station, per-catalogue health and send rate, recent plays, worker activity |
| **Review** | The queue: search at the top, then ranked candidates with per-signal score breakdowns. Resolve, archive for later, or mark as imaging |
| **Library** | Every song ever seen, filterable by status, searchable, sortable by lowest confidence — where the archive lives, and where a wrongly filtered song is rescued |
| **Playlists** | What is being delivered per station, with links into Spotify, JSON export, and duplicate cleanup |
| **Stations** | Add, discover, test, poll, re-label, pause and remove streams |
| **Settings** | Thresholds, catalogues, per-provider pacing, delivery, the Spotify link, and your learned rules — grouped into sections rather than one long column |

No build step: the interface is plain ES modules and one stylesheet, so what
ships in the image is what runs.

**It re-skins itself per holiday** — orange and purple for Halloween, red and
green for Christmas — reading the accent from whichever holiday most of your
enabled stations are set to. A single station card carries its own station's
colour even when the rest of the app is wearing another.

**Light and dark**, following the system by default; the button in the top bar
cycles system → light → dark and the choice is remembered.

**Every view is a link.** `#/library?status=archived&q=elfman` is a real address,
so a filtered library can be bookmarked and the Back button undoes a navigation
rather than leaving the app.

**The review queue is keyboard-first**, because it is the screen you spend real
time in:

| Key | Does |
|---|---|
| <kbd>←</kbd> <kbd>→</kbd> | Previous / next item |
| <kbd>1</kbd>–<kbd>9</kbd> | Confirm that candidate |
| <kbd>a</kbd> / <kbd>x</kbd> | Archive for later / mark as station imaging |
| <kbd>e</kbd> | Jump to the search fields (<kbd>Enter</kbd> searches) |
| <kbd>g</kbd> then <kbd>d r l p t s</kbd> | Jump to any view |
| <kbd>r</kbd> / <kbd>/</kbd> / <kbd>?</kbd> | Refresh · focus search · list every shortcut |

The dashboard refreshes on a timer without rebuilding itself: each block carries
a signature of the data it was drawn from and is only redrawn when that changes,
so the activity log keeps its scroll position and artwork is not re-fetched every
fifteen seconds. It is **one request per refresh** — `/api/dashboard` returns
everything the screen draws — and **none at all** while the tab is in the
background, because nothing is being looked at.

### What the interface costs the server

The UI is the busiest client this app has: it polls every fifteen seconds, for as
long as a tab is open, and does it from every tab that is open.

| | Before | Now |
|---|---|---|
| Requests to open the app | 5 | **1** |
| Requests per refresh | 4 | **1** |
| Requests while the tab is hidden | 4 per tick | **0** |
| `COUNT(*)`s behind a refresh | 5, one of them over the whole `plays` table | **0** unless something wrote since the last one |
| Library search | full scan of `songs`, twice, per keystroke | **an index lookup** |

The counts behind `/api/stats` are cached against the database's *write
generation* rather than a clock, which makes the cache exact instead of merely
fresh: confirming a match is a write, so the queue badge updates on the very next
request rather than whenever a timeout happens to lapse.

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

Four toggles — MusicBrainz, Spotify, Deezer and Apple Music — are in
*Settings → Matching*, all on by default. The first three are what identify a
song, need no account, and switching one off costs coverage and buys nothing.

The Spotify switch means something narrower, because Spotify is not searched for
automatic matching at all. It covers the two paths that still reach it: a manual
search from the review queue, and the ISRC tie-break. Turning it off does not
stop playlist delivery — that is *Settings → Delivery → Sync to Spotify*.

### Why Spotify is not a matching catalogue

It is the only provider here that is also a destination, and it is the only one
whose quota is charged to the *application* rather than to this machine — which
is why overrunning it takes the app out for hours at a time rather than seconds.

Searching it for every song the stations play was not buying that back. Measured
against a real library of 3,469 matched songs, Spotify was the winning match for
**26** of them, and across a 260-song sample **not one** auto-accepted match
would have dropped into the review queue without Spotify's corroboration. The
other three already agree with each other, and Deezer returns the ISRC for free —
which is what makes the delivery lookup exact.

So the split is by *who is asking*, not by catalogue:

| Path | Uses Spotify | Why |
|---|---|---|
| Automated match loop | no | Runs on every song forever; the other three identify them |
| Delivery (link + playlist writes) | yes | A playlist needs a URI, fetched by recording code |
| ISRC tie-break | yes | One exact request, borderline songs only, and it returns a URI too |
| Manual search in the review queue | yes | One request, on demand, and the only route to a Spotify-only song |

The cost is roughly one song in 130 — the ones nothing but Spotify carries — which
now lands in review instead of matching automatically. That is what the manual
search is for, and why it deliberately still asks Spotify.

Each provider's pacing is tunable too, in *Settings → Catalogue pacing*. These
have sensible defaults and are worth touching only if a service is persistently
unhappy with you:

| Setting | Default | Purpose |
|---|---|---|
| MusicBrainz rate limit (seconds) | `1.1` | Minimum gap between MusicBrainz requests |
| MusicBrainz cooldown (seconds) | `60` | Pause after a `503`/`429`, escalating to 3×, 10×, 30× |
| Spotify rate limit (seconds) | `0.5` | Minimum gap between Spotify requests; bounds the **burst** |
| Spotify requests per hour | `1200` | Sustained ceiling; bounds the **hour**. See below — this is the one a rate limit cannot express |
| Spotify cooldown (seconds) | `10` | Floor for Spotify's per-application quota, escalating to 2×, 4×, 8× |
| Deezer rate limit (seconds) | `0.1` | Minimum gap; Deezer allows about 50 requests every 5 seconds |
| Deezer cooldown (seconds) | `30` | Pause after a quota refusal, escalating to 2×, 4×, 8× |
| Apple Music rate limit (seconds) | `3.0` | Minimum gap; the store allows about 20 requests a minute |
| Apple Music cooldown (seconds) | `60` | Pause after a `403`, escalating to 3×, 10×, 30× |

They differ because the services do. MusicBrainz and Apple Music never say how
long to wait, so the pause is a guess and guessing long is the safe direction.
Spotify states its own `Retry-After` and that leads whenever it is longer than
the floor — the floor only exists to stop a tight retry loop. Deezer says nothing
either, but its quota window is five seconds wide, so it is treated the gentle
way for the same reason.

**Spotify has two limits behind one status code**, which is why it is the only
provider here with an hourly budget as well as a gap. A short rolling window
forgives a burst within seconds; a long one answers a *sustained* overrun with a
lockout measured in hours. A minimum gap speaks only to the first. Spacing calls
half a second apart is entirely compatible with earning a 21.9-hour ban, because
nothing in a per-request gap knows how long you have been going.

Spotify publishes neither figure, and an app still in **Development mode** gets
less headroom than one granted extended quota. So the default is not derived from
a documented budget — there is none — but from the only hard measurement
available: sustained traffic at the 0.5s gap provably gets refused. `1200/hour`
is a third of a request a second, six times under that, and still enough to
attach Spotify links to a two-thousand-song backlog in under two hours. The
dashboard shows the hour's real count against the budget, so if it needs
correcting, correct it from that rather than from a guess.

Raising a cooldown is the right response to persistent throttling; lowering a
rate limit is not, and will get you blocked — for Spotify that means the whole
application, not just the IP.

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
    index.html     the shell
    styles.css     one design system: themes, holidays, every component
    app.js         routing, event dispatch, chrome, the poll timer
    js/            util · api · ui · state · router
    js/views/      one module per view: meta, render, actions, changes
tools/make_icon.py regenerates the app icon
unraid/            UnRaid Community Applications template
```

---

## Notes and ideas

* **Back up `/config`.** It holds the database, your Spotify link, and — most
  valuably — every matching rule you have taught it.
* **Library search uses SQLite's FTS5 index**, kept current by triggers that fire
  only when a song's names actually change — not on the `last_seen_at` bump every
  play writes. It matches whole words and prefixes, so `purple peo` finds *The
  Purple People Eater*. On a SQLite built without FTS5 the app still starts and
  falls back to a scan, and says so in the Activity log.
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
* **Adding another holiday** is just adding a station; set its *Holiday* field —
  editable inline on the Stations table — to pick up the matching accent colour.
  A new palette is four custom properties in `styles.css`, one line per theme.
* **Adding a view** is a module in `web/js/views/` exporting `meta` and
  `render`, plus one line in `VIEWS` in `app.js`. Its buttons are wired by
  `data-act="name"` resolving against the module's own `actions` map, so nothing
  in the shell has to learn about them.
