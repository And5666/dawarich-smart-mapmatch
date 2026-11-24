# Deploying Mapmatch

What a self-hoster has to provide, how Valhalla plugs in, and the account model
for one Dawarich serving several people. This is the deployment plan as of
2026-11-24: the Valhalla and single-user parts are what the code does today; the
account section is what it is moving to.

## What you need

Three things, only one of which is unusual:

1. A Dawarich you can reach from the container, and one API key per person.
2. A **Valhalla** server with road, path and rail tiles for the area you travel
   in. Nothing is bundled: the image ships GB rail alignments only.
3. One container and one volume (`/data`: SQLite database plus the archive of
   original GPS points). No database server, no queue, no front-end build.

`compose.yml` is the whole install: `docker compose up -d --build`, open the
portal, sign in with a Dawarich key, **Sync**, **Match**, **Push**.

## Valhalla

Yes, it has to exist before the portal is useful. The engine is not a
self-contained matcher: it asks Valhalla's `trace_attributes` for each mode
segment and routes gaps through Valhalla's `route` endpoint. Anything that
speaks that Valhalla API works; OSRM, GraphHopper or a routing service without
`trace_attributes` does not.

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

Then give the portal `VALHALLA_URL=http://valhalla:8002` (the compose file's
default points at `dawarich-valhalla:8002`, the usual name when Valhalla is part
of a Dawarich stack).

Practical notes that have bitten already:

- **Tiles are regional.** A trip outside the PBF matches only up to the tile
  boundary and then fails or falls back to raw GPS. Build England and Wales for
  a UK user, not "Europe" unless you have the disk: the two PBFs behind the test
  instance are 1.7 GB and 150 MB, and the built tiles are larger still.
- **Trace limits matter.** Valhalla's default `service_limits.trace` is
  `max_search_radius: 100` m, `max_distance: 200000` m. The engine's profiles are
  written to sit inside those (search radius 80–100 m, breakage 2–3 km) — do not
  lower them, and if you raise `search_radius` in the matcher, raise the limit
  too or Valhalla rejects the call.
- **Trains are Great Britain only.** `data/rail_shapes.json.gz` and
  `rail_stations.json.gz` are GB rail alignments. In GB, train legs are snapped
  to rail; elsewhere train legs are matched as road and look wrong. That is a
  known limitation, not a config error.
- **Throughput.** Matching is one HTTP call per segment, so a big account takes
  minutes and a small one seconds. `MATCH_WORKERS` (default 4) should stay at or
  below the thread count you gave `valhalla_service` (the test instance uses 6).
- The Valhalla container needs no admin database, no elevation and no traffic
  for this; `valhalla_build_admins` is only for place names Valhalla itself
  reports.

## Accounts

Today the Dawarich API key **is** the sign-in: `/connect` validates it against
`/api/v1/users/me`, then finds or creates an account named
`dawarich:<host>:<dawarich user id>` and keeps the key for syncing. That has two
useful consequences and two bad ones.

Good: every portal account already belongs to exactly one Dawarich user, all
data is already scoped by `user_id`, and the first account created becomes the
admin. Bad: people do not carry API keys and will not remember them, and a key
is a credential people paste around — it is currently stored as plain text in
`portal.db`.

The move is to password accounts, with the key kept as a *link* rather than a
*login*:

- **Sign-in** is username and password (`/login`), using the pbkdf2 hashing and
  `authenticate()` that already exist. Admin is bootstrapped from
  `ADMIN_USER` / `ADMIN_PASSWORD` on first start; the existing sign-up toggle
  lets an admin allow or block self-registration.
- **Linking Dawarich** becomes a Settings action: paste the key once, the portal
  verifies it, shows which Dawarich user it belongs to, and stores it encrypted
  (key from an environment secret) rather than in the clear. Re-link or unlink
  from the same place.
- **One instance, several people.** Each portal account has its own key, its own
  archive, its own jobs and its own settings row; nothing today crosses between
  `user_id`s, so this is a UI and admin problem, not a data model one.
- **Matching per person.** Not everyone wants their points rewritten. Split the
  current `auto_enabled` into three per-account switches — `sync_enabled`
  (download and archive), `match_enabled` (run the matcher), `push_enabled`
  (write back to Dawarich) — honoured by the manual buttons and the scheduler
  alike. Someone who only wants a cleaned-up archive runs sync on, match off.
- **Roles.** `admin` (all accounts, all jobs, global settings, Valhalla status),
  `member` (their own data), `viewer` (read-only, useful for showing someone
  their own trips). `users.is_admin` already exists; add a role column or a
  second flag rather than a new table.

What admins get in the next pass — the part deliberately left for after this
round: a user list with invite, reset and disable; the three per-user switches;
per-user job and push history; the sign-up toggle; the global dry-run switch;
Valhalla reachability and tile coverage for the configured region; and an audit
view of pushes and reverts across accounts.

## What is deliberately not in the image

The container needs `mapmatch/`, `pyproject.toml`, and the two GB rail files.
Everything else in the repository is development: `bench/` (its Valhalla cache
is tens of megabytes), `tests/`, `scripts/*.rb`, private CSV exports, and the
`out/` scratch dumps. `.dockerignore` keeps them out of the build context as well
as the image.

The retired bench review site (the Leaflet pages that showed the worst trips on
a map) is archived on the server under
`Homelab/projects/_attic/dawarich-smart-mapmatch-reviewer-2026-11-24/`, together
with the `bench.py` that could regenerate it.

## First-run checklist

1. `PUSH_DRY_RUN=1` (the shipped default) so the first pushes are planned and
   logged but change nothing in Dawarich.
2. Sign in, **Sync**, match a handful of trips, look at them, then **Push** one.
3. Check it in Dawarich, then run the rest, then set `PUSH_DRY_RUN=0`.
4. Back up the `/data` volume: it is the only copy of the archive that push and
   revert depend on.
