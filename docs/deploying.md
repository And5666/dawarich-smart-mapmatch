# Deploying Mapmatch

What a self-hoster has to provide, how Valhalla plugs in, and how accounts work
when one instance serves several people.

## What you need

1. A Dawarich you can reach from the container, and one API key per person.
2. A **Valhalla** server with road, path and rail tiles for the area you travel
   in. Nothing is bundled: the image ships GB rail alignments only.
3. One container and one volume (`/data`: SQLite database plus the archive of
   original GPS points). No database server, no queue, no front-end build.

Valhalla is deliberately **not** in the compose file. The container starts, an
admin signs in, and the address is set in the portal: **Admin → Map server**.
It is stored in the portal's database and applies to every account at once, so
people who join later inherit it and nobody has to redeploy to change it.

## Valhalla

It has to exist before matching is useful. The engine is not a self-contained
matcher: it asks Valhalla's `trace_attributes` for each mode segment and routes
gaps through Valhalla's `route` endpoint. Anything that speaks that API works;
OSRM, GraphHopper or a routing service without `trace_attributes` does not.

The reference setup, which is what the test instance runs:

```bash
docker volume create valhalla
# 1. Build tiles once, for the region you actually travel in.
docker run --rm -v valhalla:/data ghcr.io/valhalla/valhalla:latest bash -c '
  curl -L -o /data/region.osm.pbf https://download.geofabrik.de/europe/great-britain-latest.osm.pbf &&
  valhalla_build_config --mjolnir.tile_dir /data/tiles --mjolnir.concurrency 4 > /data/valhalla.json &&
  valhalla_build_tiles -c /data/valhalla.json /data/region.osm.pbf'
# 2. Serve it.
docker run -d --name valhalla --restart unless-stopped \
  -p 8002:8002 -v valhalla:/data \
  ghcr.io/valhalla/valhalla:latest valhalla_service /data/valhalla.json 6
```

Then in the portal, **Admin → Map server**, enter `http://valhalla:8002` (the
container name works if both are on the same docker network) and press **Test**.
The page reads Valhalla's `/status`, so a version and a map-data date mean it is
working.

Practical notes that have bitten already:

- **Tiles are regional.** A trip outside the PBF matches only up to the tile
  boundary and then fails or falls back to raw GPS. The two PBFs behind the test
  instance (England, Wales) are 1.7 GB and 150 MB, and the built tiles are larger
  still.
- **Trace limits matter.** Valhalla's default `service_limits.trace` is
  `max_search_radius: 100` m and `max_distance: 200000` m. The engine's profiles
  are written to sit inside those (search radius 80–100 m, breakage 2–3 km) — do
  not lower them, and if you raise `search_radius` in the matcher, raise the
  limit too or Valhalla rejects the call.
- **Trains outside Great Britain need a railway network.** GB trains use the
  bundled national timetable alignments (`data/rail_shapes.json.gz`). For any
  other country, an admin builds a railway network from the same OpenStreetMap
  extract Valhalla was built from: **Admin → Railways → Build railways** lists
  the `.osm.pbf` files in `VALHALLA_DATA_DIR`. Without a mount, run
  `docker exec mapmatch python -m mapmatch.tools.railways /path/region-latest.osm.pbf`.
  Networks are written to `PORTAL_DATA/rail/` and used at once; match the trips
  there again afterwards. Running lines are kept; sidings, yards and disused
  track are not.
- **Places with no map.** A trip somewhere Valhalla has no tiles for is kept as
  recorded and marked "No map here" instead of "Needs a look". Add that
  region's tiles to Valhalla (and its railways here) to match it.
- **Throughput.** Matching is one HTTP call per segment, so a big account takes
  minutes and a small one seconds. `MATCH_WORKERS` (default 4) should stay at or
  below the thread count you gave `valhalla_service` (the test instance uses 6).
- Valhalla needs no admin database, no elevation and no traffic for this.

## Regions

Each instance keeps a **region catalogue** next to the Valhalla address: name,
the area it covers, the OSM extract it was built from, and when it was built.
Every account sees the list (so people know where matching works); admins add,
edit and remove regions under **Admin → Regions**. Adding one starts from a list
of common Geofabrik extracts, and the area is set by panning a map and pressing
**Use this view**. It is documentation, not configuration: the tiles are
whatever the Valhalla server has.

If you want the portal to look into those tiles, mount the Valhalla data
directory read-only and tell the container where it is:

```yaml
    environment:
      VALHALLA_DATA_DIR: /valhalla
    volumes:
      - /var/lib/docker/volumes/valhalla/_data:/valhalla:ro
```

The admin page then lists the extracts and built tiles with sizes and dates, and
each file has a download link. Nothing in the portal deletes or rebuilds tiles:
building a region needs `valhalla_build_tiles` on the machine that runs Valhalla,
not in this container, and the portal has no way to run it. Without the mount
the page says so plainly rather than pretending.

## Accounts

The portal has its own accounts, and the Dawarich API key is a link rather than a
login:

- **Sign-in** is a username and password (pbkdf2 hashes in the database).
- **The first admin** comes from `ADMIN_USER` / `ADMIN_PASSWORD`, or from
  registering on an install that has no accounts yet (the first account made
  becomes the admin). Once an admin exists the environment pair is ignored.
- **Making accounts.** Admins create them under **Admin → People** with
  **Add a person**: username, password (or **Generate** one), optional name,
  optional admin flag. Each person's menu sets a new password, makes them an
  admin or a member, or stops them signing in. You can't demote or lock out
  yourself or the last admin. Disabling ends the account's
  sessions on the next request.
- **Lost passwords.** No web path back in: `PORTAL_DATA=/data python -m
  mapmatch.portal.admin reset <username>` sets one, and `... list` shows the
  accounts, their role, trip count and whether they have linked a Dawarich key.
- **Sign-up.** Off by default. The checkbox at the top of the admin page opens
  it; when it is off, registration is refused and people are told to ask an
  admin.
- **Linking Dawarich.** After signing in, each person pastes their own API key
  once, in Settings or on the connect screen. The key is verified against
  `GET /api/v1/users/me` before it is kept, is stored in that account's row, and
  the account is named after the Dawarich user the first time it is linked.
  Unlinking removes it and leaves the archive alone.
- **Several people, one Dawarich.** Each account has its own key, archive, trips,
  jobs and settings; every table is scoped by `user_id`. Nothing is shared except
  the Valhalla address, the region catalogue and the sign-up switch.

What is not built yet: per-account switches for whether matching or pushing may
run (today every account can match and push its own trips), and roles beyond
admin/member. Both are natural next steps, not hidden limitations.

## Gaps: routed or kept, one at a time

Each account sets a default under Settings: route across a gap, or keep what was
recorded. In the trip editor the **Gaps** tab overrides it: **Route all**,
**Keep all** or **Default** for the whole trip, then one row per hole with its
time, length and a **Route / Keep GPS** switch that changes that hole only.
Clicking a gap's line on the map opens its row.

Kept means kept: the line runs between the recorded points rather than along a
road. A gap you kept yourself is drawn in the warning colour, so it stands apart
from one nothing could route.

**Route anyway.** A route is refused when it would imply more speed than the
mode allows (270 km/h for driving) or a detour far longer than the recorded
path. That usually means the timestamps in the hole are wrong, not the road.
Automatic runs never override it, but a refused gap shows **Route anyway** in
its switch, and the gap is then labelled as routed over the speed check.

## What a push does in Dawarich

Dawarich draws tracks from its points, one track per device, and only drops a
track once fewer than two of its points are left. So a push never swaps a trip
on its own: it widens the window to every Dawarich track the trip touches,
deletes all the points in it, and writes them back. Matched trips go back as
their matched points on the device the GPS came from (marked as mapmatch's by
their Wi-Fi name); everything else goes back as the original fixes on their own
device. Points Dawarich's anomaly filter had hidden are fetched and archived too.

The stops on either side of a pushed trip are tidied as well. The fixes your
phone recorded while you were stopped wander tens to hundreds of metres, and
Dawarich draws every one, so a stop would otherwise show as a star of spikes.
Each place you stopped becomes one point at its centre, at arrival, at departure
and every half hour between; a lone fix worse than 100 m is left out. A stop
between two trips that both show their original GPS is left exactly as it was.
The links from a stop to the trips either side are routed along the road too,
and matched lines get a point at least every 150 m, even along straight
stretches.

That last part is what lets Dawarich hide the gaps you didn't record. In
Dawarich's settings, set **Meters between routes** to **300** and
**Minutes between routes** to **30**. Dawarich then breaks a line wherever two
points are further apart than that, which after a push only happens where your
phone genuinely stopped recording.

Every point in the window is in the archive before anything is deleted. If
Dawarich stops answering between the delete and the write, the portal says so
on every page and **Put them back** writes the missing points again from the
archive. **Settings → Troubleshooting → Repair tracks** rewrites every trip mapmatch has touched;
you should never need it, except once after upgrading from a version before 2.4, whose pushes left
straight lines between the points either side of a trip.

## What is deliberately not in the image

The container needs `mapmatch/`, `pyproject.toml`, and the two GB rail files.
Railway networks for other countries are built on the server, into `PORTAL_DATA/rail/`.
`.dockerignore` is an allowlist, so `bench/`, `tests/`, `docs/`, `out/`, the
development virtualenv and private CSV exports never enter a build.

## First-run checklist

1. Set `ADMIN_PASSWORD` in compose (or register the first account), sign in.
2. **Admin → Map server**: add the Valhalla address and press **Test**.
3. **Admin → Regions**: add the region(s) the tiles cover.
4. **Admin → People**: make an account for each person, or open sign-up.
5. Sign in as a person, link a Dawarich key, **Sync**, match a few trips, then
   **Push** one — with `PUSH_DRY_RUN=1` (the shipped default) until you trust it.
6. Check the pushed trip in Dawarich, run the rest, then set `PUSH_DRY_RUN=0`.
7. Back up the `/data` volume: it is the only copy of the archive that push and
   revert depend on.
