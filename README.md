# mapmatch for Dawarich

Phones log GPS every few seconds at best, and often much less. In
[Dawarich](https://dawarich.app) that shows up as lines cutting through
buildings, corners clipped off junctions, and train journeys drawn as straight
spokes across the country. mapmatch works out how you travelled, snaps each part
of a trip to the road, path or railway you were actually on, and writes the
result back into your Dawarich account. You can look over every trip before
anything changes.

![The same trips through Guildford town centre: the recorded GPS on the left, the matched routes on the right](docs/img/guildford-before-after.png)

*Drives and rides through Guildford. Left: what the phone recorded. Right: after
mapmatch, coloured by mode (blue driving, green cycling).*

> **This is early software.** It rewrites points in your Dawarich account. It
> keeps a copy of every original point and can put them back, and it has been
> run against real accounts, but it hasn't had many users yet. **Back up your
> Dawarich points before you push anything**, and keep that backup until
> you're sure you're happy with the results. See [Backing up](#backing-up) and
> [Known issues](#known-issues).

## What it does

mapmatch reads your raw points from Dawarich and cuts them into trips, ending a
trip after a long stop or a long gap in the data. It doesn't care how your
phone app grouped things.

Each part of a trip gets a mode: walk, cycle, drive, train or stopped. One trip
can be cycle, then train, then cycle again. Each part is then matched with rules
that suit it. Cars follow one-way streets and bikes don't. Walks and
mountain-bike rides only snap to a path when the GPS clearly follows one;
otherwise the line stays as you recorded it. Trains follow the railway.

Some GPS doesn't need matching at all. A watch or bike computer logging every
second or two already draws a better line than the map, down to which side of
the road you were on. mapmatch spots those stretches and leaves them exactly as
recorded, even when they're only part of a trip. A trip recorded entirely like
that is never touched in Dawarich.

Where the signal dropped out, the gap is routed along the road only if the time
it took makes sense. Otherwise it stays a straight line, so a tunnel doesn't
turn into a ten-mile detour.

You stay in charge of what happens:

- the **Match** page sets what gets snapped to the map. That can be everything,
  bikes only, cars only, or nothing at all. It also decides whether detailed GPS
  is kept and whether gaps get routed. You can save the choices or try them once
  on some trips;
- on any trip you can change the mode of a stretch by dragging across the
  timeline, move or leave out single points, route or keep each gap, and go
  back to any earlier version;
- each trip, or every trip at once, can go back to the original GPS, in
  mapmatch, in Dawarich, or in both.

It can also run on its own: download new points on a schedule, match them, and
push finished trips to Dawarich. That's off until you turn it on.

## Backing up

Before mapmatch changes anything in Dawarich, it copies every original point
into its own archive, and nothing in mapmatch ever deletes from that archive.
Pushing a trip swaps its points in Dawarich for the matched route. Reverting
puts the originals back exactly as they were.

That archive lives in mapmatch's `/data` volume, on the same machine. If the
volume goes, so does the safety net. So:

1. **Export your points from Dawarich first** (Dawarich → Export data, as GPX or
   GeoJSON). Keep the file somewhere else.
2. Back up mapmatch's `/data` volume too. It's one SQLite file.
3. Start with `PUSH_DRY_RUN: "1"` (the example compose file does). Pushes are
   then worked out and logged, but Dawarich isn't changed.
4. Push a handful of trips for real and check them in Dawarich before you push
   the rest, or turn on automatic pushing.

## Known issues

These are the things most likely to catch you out. None of them lose your
original points, but some will make a trip look wrong until you fix it.

- **Modes are guesses.** They come from speed, plus how close you were to a
  railway. A slow ride with lots of stops can come out as a walk. A car crawling
  through town can look like a bike, and a bus or coach comes out as driving.
  Fix any part on the trip page, and the trip is matched again with your choice.
- **Some gaps get routed the wrong way.** When a stretch is missing and a route
  fits the time, mapmatch takes it, even if you went another way. Switch that
  gap to "straight line" in the trip's Gaps tab.
- **Detailed GPS has a cut-off.** A stretch counts as detailed when it logs
  roughly every 5 seconds or faster and the line is smooth. A phone logging every
  10 seconds gets matched like any other phone track. A very jittery watch track
  does too.
- **No map, no match.** mapmatch only knows the roads your Valhalla server has
  tiles for. Trips somewhere else are kept as recorded and marked "No map here".
- **Trains outside Great Britain need setting up.** GB trains use the bundled
  national rail lines. Anywhere else, an admin has to build a railway network
  from an OpenStreetMap extract first (Admin → Railways). Without one, trains
  abroad are matched as roads and look wrong. Where there's no road map either,
  a journey only counts as a train if it stays right on the railway.
- **Pushing changes Dawarich's data, not just how it looks.** Matched points
  replace the recorded ones, so Dawarich's distances, stats and anything built
  from the points will change too. Stops next to a pushed trip are tidied into
  one point per place. Reverting brings all of that back.
- **A push can stop halfway**, for example if Dawarich goes down in the middle.
  Some points are then missing from Dawarich until you press "Put them back" on
  the banner that appears. They're safe in the archive in the meantime.
- **Only tested with Dawarich 1.23.** mapmatch uses Dawarich's points API. If a
  later Dawarich changes that API, syncing or pushing could break.
- **API keys are stored as plain text** in mapmatch's database, as they have to
  be sent to Dawarich. Keep the `/data` volume private, put the portal behind
  https, and don't expose it to the internet without a good reason.
- **Big accounts take a while.** Matching is one request to Valhalla per part of
  a trip. A few thousand trips take minutes, not seconds, and a Valhalla server
  with little memory will slow everything down.

## What you need

- **Dawarich**, reachable from where mapmatch runs, and an API key for each
  person (Dawarich → Account).
- **Valhalla**, the open-source routing engine, with map tiles for the places
  you travel. mapmatch asks it which roads and paths fit your GPS. It isn't
  bundled because tiles are big and depend on your region.
  [docs/deploying.md](docs/deploying.md#valhalla) has the two commands that build
  and serve it.
- **Docker**, with one container and one volume. Everything sits in a single
  SQLite file, so there's no database server to run.

## Set it up

1. Get the code and the compose file:

   ```bash
   git clone https://github.com/And5666/dawarich-smart-mapmatch.git
   cd dawarich-smart-mapmatch
   cp compose.example.yml compose.yml
   ```

2. Edit `compose.yml`:
   - `DAWARICH_URL`: how the container reaches Dawarich, for example
     `http://dawarich_app:3000` on Dawarich's Docker network.
   - `ADMIN_USER` and `ADMIN_PASSWORD`: your first sign-in.
   - the network name at the bottom: the one your Dawarich containers use
     (`docker network ls`).

   Leave `PUSH_DRY_RUN: "1"` for now.

3. Start it:

   ```bash
   docker compose up -d --build
   ```

4. Open `http://your-server:8080` and sign in as the admin. Then, under
   **Admin**:
   - **Map server**: enter your Valhalla address and press **Test**.
   - **Regions**: note which areas your tiles cover, so everyone knows where
     matching works.
   - **Railways**: for trains outside Great Britain, build a network from the
     same `.osm.pbf` extract your Valhalla tiles came from.
   - **People**: add accounts for anyone else, or let people sign up.

5. Connect Dawarich by pasting your API key. mapmatch downloads your points and
   splits them into trips. Nothing is matched or changed yet.

6. In Dawarich's settings, set **Meters between routes** to `300` and
   **Minutes between routes** to `30`. Otherwise Dawarich draws straight lines
   across stretches your phone didn't record.

7. Have a look at the **Match** page, then press **Match** on the Trips page.
   Open a few trips and push one. With dry run on, you'll see what would change.
   When you're happy, back up (see above), set `PUSH_DRY_RUN: "0"`, restart,
   and push for real.

Behind a reverse proxy, set `PORTAL_BASE_PATH` (for example `/mapmatch`), and
`PORTAL_SECURE_COOKIE: "1"` if it's served over https.

[docs/deploying.md](docs/deploying.md) goes into more detail: Valhalla, regions,
railways, accounts for several people, how gaps are handled, and exactly what a
push does in Dawarich.

### Environment

| Variable | Default | |
|---|---|---|
| `DAWARICH_URL` | `http://dawarich_app:3000` | Dawarich as the container sees it (each person can point at their own in Settings) |
| `DAWARICH_HOST_HEADER` | none | Host header to send, if Dawarich only answers to its public name |
| `ADMIN_USER`, `ADMIN_PASSWORD` | `admin`, none | First admin account; ignored once an admin exists |
| `PORTAL_DATA` | `/data` | Database, archive and railway networks. **Back this up.** |
| `PORTAL_BASE_PATH` | none | e.g. `/mapmatch` when served under a path |
| `PORTAL_SECURE_COOKIE` | `0` | `1` behind https |
| `PUSH_DRY_RUN` | `0` (`1` in the example compose) | `1` works out pushes without changing Dawarich |
| `PUSH_TRIPS_ONLY` | `0` | `1` makes a push rewrite the pushed trips and nothing else: stops are not tidied, no links are drawn, and no point outside a trip being pushed or undone is deleted or written |
| `MATCH_WORKERS` | `4` | Trips matched in parallel; keep at or below Valhalla's thread count |
| `VALHALLA_DATA_DIR` | none | Optional read-only mount of Valhalla's data, so admins can see the map files and build railways from them |
| `REQUIRE_ADMIN` | `0` | `1` refuses to start without an admin account, so a first visitor can never register as admin |
| `PORTAL_HOME_URL`, `PORTAL_HOME_LABEL` | none, `Dashboard` | A link in the top bar back to the site the portal is part of |
| `PORTAL_EXTRA_CSS` | none | One more stylesheet, loaded after the portal's own (your fonts or colours) |
| `PORTAL_THEME_KEY` | `mm-theme` | The `localStorage` key for light/dark, to share the choice with a site on the same origin |
| `PORTAL_PMTILES_URL`, `PORTAL_PMTILES_ASSETS` | none | An offline basemap instead of CARTO's hosted one: see [Offline basemap](docs/deploying.md#offline-basemap) |

Lost the admin password? Run
`docker exec -it mapmatch python -m mapmatch.portal.admin reset <username>`.

## How well it works

Measured on one real account: 280 journeys (15,197 GPS points) over three months
of driving, cycling, walking, hiking and commuting by train, all logged by a
phone. The comparison is with the previous version of this project.

| | before | now |
|---|---|---|
| straight-line jumps between matched parts | 720 | 120 |
| lone GPS points left unmatched | 297 | 5 |
| journeys with more than 20% unmatched | 32 | 8 |
| distance left unmatched | 101 km | 56 km |
| typical worst-case distance from GPS to the line (95th percentile, mean) | 1,094 m | 29 m |

Most of what's still unmatched is bad data, like fixes that would mean
travelling at 300–700 km/h, or walks off-trail where there's no path to snap
to. A second account, logged mostly on a Garmin watch, was used to tune the
detailed-GPS handling. Over 99.9% of its watch-logged points come through
untouched.

## How it works

For each trip:

1. **Clean.** Duplicates, broken clocks, very inaccurate fixes, spikes and short
   teleports are set aside. They aren't deleted.
2. **Stops.** Places you stayed become a single point. A pause in the middle of
   a walk still counts as walking.
3. **Modes.** Speed, plus how close you were to a railway, gives each stretch a
   mode. Changing mode is made expensive, so a short blip doesn't split a trip
   into pieces.
4. **Detailed GPS.** Stretches logged at watch rate and smooth enough are kept
   point for point, unless you've asked for them to be snapped.
5. **Match.** Everything else goes to Valhalla with the profile for its mode.
   Trains are matched on the railway instead.
6. **Fill.** Every break goes through one filler, whether it's a lost signal,
   an unmatched run or a change of mode. It tries a route, then a route through
   the recorded points, and otherwise keeps the GPS.
7. **Write.** The result is a timed list of points that goes into Dawarich in
   place of the originals.

## Develop

```bash
uv venv .venv && uv pip install -e '.[dev,rail]'
.venv/bin/pytest                      # engine and portal tests, no network needed
VALHALLA_URL=http://host:8002 .venv/bin/python -m mapmatch.tools.bench \
    --csv your-export.csv --trips auto
PORTAL_DATA=./portal-data .venv/bin/uvicorn mapmatch.portal.app:app --reload --port 8080
```

- `mapmatch/engine/`: the matcher, standard library only (`api.py` is the entry point)
- `mapmatch/portal/`: the web app (`push.py` holds the rules for changing Dawarich)
- `mapmatch/tools/bench.py`: benchmark against a CSV export
- `mapmatch/tools/railways.py`: build a railway network from an OpenStreetMap extract
- `tests/`: tests with a fake Valhalla and a fake Dawarich
- `docs/deploying.md`: running it for yourself or for several people

GPS exports (`data/*.csv`, `data/*.gpx`), the portal's data folder and the
benchmark's Valhalla cache are git-ignored, because they all contain
coordinates.
