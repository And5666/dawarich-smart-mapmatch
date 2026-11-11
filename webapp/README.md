# Mapmatch portal

FastAPI page mounted at `/review/portal` next to the review page.

## What it does

- Login and optional signup. Admin can turn signup off.
- Stores a Dawarich API key per user on the server.
- Syncs points, groups them by Dawarich `tracker_id`, runs the matcher, saves the source and matched data.
- Map view defaults to the last year; date range is editable.
- Route editor shows original and matched lines, segments, mode changes, raw toggles, full rematch, remove, and raw point JSON editing.
- Push replaces the Dawarich account with the edited review stream. A backup is written before anything is deleted.
- Revert restores the newest backup.
- Per-user automatic matching interval. The worker checks, backs up, matches and pushes.
- Admin panel shows Valhalla status and a region download box.

## Important behaviour

- The Dawarich key is stored in the portal SQLite database on the server. Protect `/data/portal`.
- Push is destructive to the target account's visible points. The source backup is the undo.
- Initial sync runs base matching only for speed. Open a route and press "Map match all" to add connectors and repaired gaps for that route.
- Valhalla region downloads only run if `VALHALLA_DOWNLOAD_CMD` is configured on the host. The app never runs a browser-supplied shell command.
- Admin seeding needs `PORTAL_ADMIN_PASSWORD` on first boot. If the account already exists, the env is ignored.

## Deployment

The app lives inside the existing cleaner container:

- matcher package: `/data/smartmap`
- rail data: `/data/rail_shapes.json.gz`, `/data/rail_stations.json.gz`
- portal: `/app/app/portal.py`
- router added to `/app/app/main.py`
- data: `/data/portal`

Restart `dawarich-cleaner` after copying the files. The page is then at `https://deepseek.andw.net/review/portal`.
