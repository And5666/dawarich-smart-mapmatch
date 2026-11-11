#!/usr/bin/env bash
# Copy the portal into the running cleaner container. Run on nitro5.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${HOST:-dawarich.andw.net}"

tar -C "$REPO" -cf - smartmap | docker exec -i dawarich-cleaner tar -xf - -C /data
docker cp "$REPO/data/rail_shapes.json.gz" dawarich-cleaner:/data/rail_shapes.json.gz
docker cp "$REPO/data/rail_stations.json.gz" dawarich-cleaner:/data/rail_stations.json.gz
docker cp "$REPO/webapp/portal.py" dawarich-cleaner:/app/app/portal.py
docker exec -i dawarich-cleaner python3 - <<'PY'
from pathlib import Path
p = Path('/app/app/main.py')
s = p.read_text()
if 'from . import dawarich, jobs, portal, report, review, valhalla' not in s:
    s = s.replace('from . import dawarich, jobs, report, review, valhalla',
                  'from . import dawarich, jobs, portal, report, review, valhalla')
if 'app.include_router(portal.router)' not in s:
    s = s.replace('app.include_router(review.router)',
                  'app.include_router(review.router)\napp.include_router(portal.router)')
p.write_text(s)
PY
docker restart dawarich-cleaner >/dev/null
echo "portal deployed at https://${HOST}/review/portal"
