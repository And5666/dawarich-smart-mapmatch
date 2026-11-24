# Mapmatch for Dawarich

Turns the raw GPS in a self-hosted [Dawarich](https://dawarich.app) account into
clean, map-matched trips, with a web portal to review and fix them.

- **Raw first**: **Sync** (and CSV import) only copies your raw points and
  splits them into trips. Nothing is map matched until you ask: per trip,
  with **Map match** for everything pending, or by the automatic run.
- **Automatic when you want it**: on a schedule it fetches new points, works
  out how you travelled (walk, cycle, drive, train), snaps each part to
  roads, paths or railways, and can push the result back into Dawarich.
- **In your control**: the portal shows every trip with its segments; change a
  segment's mode, split it, recalculate, undo, lock it, push it or revert it.
- **Nothing original is lost**: every GPS point read from Dawarich is copied
  into the portal's archive before anything changes, and nothing in the
  portal ever deletes from that archive. Any pushed trip can be reverted.

## How matching works

`mapmatch.engine.api.match_trip(points)` runs per trip:

1. **Clean**: drop duplicates, bad clocks, fixes worse than 150 m, spikes and
   short teleport excursions. They are excluded from matching, not deleted.
2. **Stays**: places you stayed (≥3 min within 50 m) become a single point.
3. **Modes**: per movement leg, a Viterbi pass over the phone's reported speed
   (falling back to position-derived speed) and railway proximity labels
   walk, cycle, drive or train. Mode changes are expensive and short blips merge
   into their neighbours, so a trip reads like *cycle → train → cycle*, not 50
   flickering parts.
4. **Match**: one Valhalla `trace_attributes` call per mode segment (trains use
   GB rail alignments). Each fix gets an exact position on the matched path.
5. **Fill**: every gap (unmatched fixes, a break in the match, a mode change,
   a data dropout) goes through one filler. It tries a route, then a route
   through the recorded fixes, and only then keeps the original fixes. A
   route is accepted only if its length and implied speed fit the time taken.
6. **Assemble**: segments with timed geometry, a quality score, and a point
   stream for Dawarich with strictly increasing timestamps.

Trips are found in the whole point stream by `mapmatch.engine.trips.split_trips`.
A trip ends after 30 minutes without data, or a stay of 30 minutes (both settable).

### Results on the test account (280 routes, 15,197 fixes)

`bench/old-tracker.json` is the previous engine; `bench/new-tracker.json` is this one.

| | previous engine | now |
|---|---|---|
| straight-line jumps | 720 | 103 (mostly < 100 m) |
| isolated one-fix raw parts | 297 | 8 |
| routes with > 20 % unmatched | 32 | 6 |
| unmatched distance | 101 km | 63 km |
| 95th-percentile distance from GPS to line (mean) | 1,094 m | 29 m |
| output km / GPS km | 1.054 | 1.036 |

Most of what remains unmatched is genuinely bad data, such as runs of fixes
reporting 300–700 km/h.

## The portal

Standalone FastAPI app with a no-build Preact + MapLibre front end.

- **Trips**: map of every trip (raw trips as grey dashed lines, matched ones
  coloured by mode), an "All GPS points" layer, distance by mode, filters for
  not matched, needs review, failed, not pushed, and edited since push, and a
  **Push** button beside **Match** that writes every matched trip in the view
  back to Dawarich in one job.
- **Trip editor**: the route drawn by how it was made (solid = on the map,
  dashed = routed across a gap, dotted = original GPS kept), the original and
  ignored fixes, a timeline and speed chart linked to the map, and per-segment
  mode, split, recalculate, undo, lock, push, revert and GPX/GeoJSON export.
- **Activity**: background jobs and a log of every change made in Dawarich.
- **Settings**: Dawarich URL and API key (with a connection test), automatic
  runs and interval, automatic push, a start date, trip gap, gap strategy
  ("route across gaps" or "only what was recorded"), CSV import, revert all.
- **Admin**: accounts, sign-up toggle, Valhalla status, matcher version.

### What a push does

For one trip, inside its time window:

1. Fetch the window from Dawarich and archive any point not yet archived.
2. Write the matched points (tracker `mapmatch`).
3. Delete only the original points that are confirmed in the archive, plus
   matched points from an earlier push of that trip.

If any point in the window cannot be archived, nothing is changed. **Revert**
re-posts the archived originals (with their original tracker) and removes the
matched points. Start with `PUSH_DRY_RUN=1` to see the plan without changing
Dawarich; every push and revert is listed under Activity.

## Run it

```bash
cp compose.example.yml compose.yml   # set URLs, admin password, network
docker compose up -d --build
```

Then open the portal and connect with your Dawarich API key: today the key is the
sign-in. Press **Sync**, then **Match** (or **Push** once matched). You will see
your raw trips; press **Map match** (or open a trip and match just that one) when
ready. Valhalla is not bundled — you point the portal at one you already run,
or at the one that came with your Dawarich stack. `docs/deploying.md` covers the
choices and the account model.

| Variable | Default | |
|---|---|---|
| `DAWARICH_URL` | `http://dawarich_app:3000` | Dawarich as seen from the container (users can override it in Settings) |
| `DAWARICH_HOST_HEADER` | – | Host header to send, if Dawarich only answers its public name |
| `VALHALLA_URL` | `http://127.0.0.1:8002` | Valhalla reachable from the container, with tiles for the area you ride in (see `docs/deploying.md`) |
| `PORTAL_DATA` | `/data` | SQLite database and archive. **Back this up.** |
| `PORTAL_BASE_PATH` | – | e.g. `/mapmatch` when served under a path |
| `PORTAL_SECURE_COOKIE` | `0` | `1` behind https |
| `PUSH_DRY_RUN` | `0` | `1` plans pushes without changing Dawarich |
| `MATCH_WORKERS` | `4` | parallel trip matching |
| `RAIL_DATA_DIR` | `data/` | GB rail shapes and stations (set in the image) |

The Dawarich API key is stored in the portal's database; protect the data volume.

## Develop

```bash
uv venv .venv && uv pip install -e '.[dev]'
.venv/bin/pytest                      # engine + portal tests, no network
VALHALLA_URL=http://host:8002 .venv/bin/python -m mapmatch.tools.bench \
    --csv data/test-account-points.csv --trips tracker --compare bench/old-tracker.json
DAWARICH_URL=https://your-dawarich VALHALLA_URL=http://host:8002 PORTAL_DATA=./portal-data \
    .venv/bin/uvicorn mapmatch.portal.app:app --reload --port 8080
```

The bench caches Valhalla answers in `bench/cache/` (git-ignored: they contain
coordinates). Private GPS exports (`data/*.csv`) are git-ignored.

Layout:

- `mapmatch/engine/`: matcher, standard library only (`api.py` is the entry point)
- `mapmatch/portal/`: web app (`push.py` holds the Dawarich safety rules)
- `mapmatch/tools/bench.py`: benchmark on a CSV export
- `tests/`: unit tests with a fake Valhalla and a fake Dawarich
- `docs/deploying.md`: what a self-hoster needs to provide (Valhalla), and the
  account model for one Dawarich, several people
- `scripts/*.rb`: Dawarich `rails runner` helpers for rebuilding tracks
