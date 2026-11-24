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
admin signs in, and the address is set in the portal: **Admin → Map matching**.
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

Then in the portal, **Admin → Map matching**, enter `http://valhalla:8002` (the
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
- **Trains are Great Britain only.** `data/rail_shapes.json.gz` and
  `rail_stations.json.gz` are GB rail alignments. In GB, train legs are snapped
  to rail; elsewhere train legs are matched as road and look wrong. That is a
  known limitation, not a config error.
- **Throughput.** Matching is one HTTP call per segment, so a big account takes
  minutes and a small one seconds. `MATCH_WORKERS` (default 4) should stay at or
  below the thread count you gave `valhalla_service` (the test instance uses 6).
- Valhalla needs no admin database, no elevation and no traffic for this.

## Regions

Each instance keeps a **region catalogue** next to the Valhalla address: name,
the area it covers, the OSM extract it was built from, and when it was built.
Every account sees the list (so people know where matching works); admins add,
edit and delete rows under **Admin → Map data**. It is documentation, not
configuration: the tiles are whatever the Valhalla server has.

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
- **Making accounts.** Admins create them under **Admin → People**: username,
  password, optional name, optional admin flag. Reset a password, promote or
  demote, or disable an account from the same row. Disabling ends the account's
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
recorded. The trip editor can override it twice over. **Route them all** and
**Keep them all** change the whole trip; below them every hole the matcher had
to bridge is listed with its time span and length, each with a button that
switches that one hole and nothing else. The same choice can be made by clicking
the gap's line on the map — a routed gap is a dashed curve, a kept one a dotted
straight run between the points either side.

Kept means kept: the line runs between the recorded points rather than along a
road, so the trip gets shorter and may be flagged "needs a look". A gap you
chose yourself is drawn in the warning colour so it is distinguishable from one
nothing could route. Exceptions are stored per trip (`keep_gaps`), survive
editing, and are cleared by **Route them all**.

**Route anyway.** A route is refused when it would imply more speed than a car
can do (270 km/h for driving: 200 × 1.3 + 10) or a detour far longer than the
recorded path. That is usually a sign the timestamps in the hole are wrong, not
that the road is wrong. The cap stays on everywhere by default — automatic runs
never override it — but a refused gap offers **Route anyway** in the editor,
and one button below the list does the same for every refused gap at once. It
routes the way the router drew it and records that it was your call; the gap
then shows as "over the cap" so it is obvious which ones were forced.

Because a stored match from before this existed has no per-hole detail, the
editor asks for a re-match the first time; Settings offers **Rematch them** for
every trip matched by an older engine.

## What is deliberately not in the image

The container needs `mapmatch/`, `pyproject.toml`, and the two GB rail files.
`.dockerignore` is an allowlist, so `bench/`, `tests/`, `docs/`, `out/`, the
development virtualenv and private CSV exports never enter a build.

The retired bench review site (the Leaflet pages that showed the worst trips on
a map) is archived on the server under
`Homelab/projects/_attic/dawarich-smart-mapmatch-reviewer-2026-11-24/`, together
with the `bench.py` that could regenerate it.

## First-run checklist

1. Set `ADMIN_PASSWORD` in compose (or register the first account), sign in.
2. **Admin → Map matching**: add the Valhalla address and press **Test**.
3. **Admin → Map data**: add the region(s) the tiles cover.
4. **Admin → People**: make an account for each person, or open sign-up.
5. Sign in as a person, link a Dawarich key, **Sync**, match a few trips, then
   **Push** one — with `PUSH_DRY_RUN=1` (the shipped default) until you trust it.
6. Check the pushed trip in Dawarich, run the rest, then set `PUSH_DRY_RUN=0`.
7. Back up the `/data` volume: it is the only copy of the archive that push and
   revert depend on.
