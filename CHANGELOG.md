# Changes

## 2.12.0

- **The engine can read the basemap** (`MAPMATCH_GROUND_PMTILES`, the same
  Protomaps PMTiles file the portal can draw offline; `mapmatch/engine/ground.py`,
  pure Python). It answers three questions the road network cannot: is this
  point on water, is it under a roof, and does a straight line between two
  points cross water or run through buildings. Without the file nothing changes.
- With it: fixes on water are dropped unless a run of them travels (a ferry);
  positions matched to a "footway" that is on water (a floating pontoon) are
  not trusted; a trip that ends under a roof stops at the first fix inside when
  nothing after it gets further than a room away; kept GPS that stays under one
  roof for twenty seconds is taken out; a straight step of 25 m or more across
  water or through buildings is routed if a path fits the time, else left
  unconnected; and across open ground (a car park, a square) a short hop goes
  straight instead of round the aisles.
- "Points far from the line" no longer counts the fixes of a stop: a stop is
  one dot, and the scatter round it is not something the route missed.

## 2.11.0

Found by looking at the overview map of a whole city with every trip drawn:
lines over water, tangles inside shops, loops round car parks, straight lines
through blocks. Each is a rule, measured over 580 trips (`line drawn while
stopped` 135 km to 5 km; straight jumps of 150 m+ in kept GPS 42 to 4; fixes
dropped as end-of-trip scatter 24 to 1,150; watch-ride truth 97.0% to 97.5%).

- **A stop is one dot, however it was logged.** A watch left running in a shop
  drew a tangle a hundred metres wide; so did a "wide stop". Five minutes of
  milling about on one spot anywhere in a walk or a ride is a stop too.
- **A trip logged in fine detail is no longer exempt.** Its holes are routed or
  left unconnected and its stops are dots, like any other trip. It is reported
  as "kept as recorded" only when nothing in it needed tidying; otherwise it
  can be pushed like any corrected trip. The same goes for a trip abroad.
- **Where a trip ends indoors**, the last fixes (and the first of the trip out)
  scatter over the building. Such a tangle is the way in and one position.
- **Kept GPS is pruned and judged.** Fixes that could not have been walked to
  at the stretch's own pace go. What is still a zigzag is scatter, not a walk
  off the paths: it becomes the path there was time for, else a plain line.
- **Holes from 200 m** (was 1 km) are tried every way and otherwise left
  unconnected; kept GPS is cut where a step is 150 m and eight times the
  usual step of its track (a watch that lost signal for 700 m).
- **No ferry lines for walks and rides.** A ferry route is the only "path" out
  on a harbour and a walk along the quay snapped to it.
- **Car parks.** Nobody on foot or a bike follows the parking aisles: positions
  matched to an aisle are not trusted, and no route is tried across them. A
  matched or routed stretch may be 1.3 times the GPS it replaces (was 1.4 to 2).
- **A straight line of 60 m or more is drawn only where a path runs under it**
  (`valhalla.off_paths`); otherwise the two ends are left unconnected.
- Known limit: floating pontoons are footways on the map, so a line can still
  follow one along a quay. Telling them from land needs a water layer.

## 2.10.1

### Safety: nothing reaches Dawarich that the portal doesn't show or the GPS doesn't back

- **Days with no data got points.** With the phone off from 13 October to
  26 November, the stop either side was stamped every half hour through all
  six weeks (48 points a day). Stop points now never bridge more than 12 hours
  with nothing recorded.
- **Made-up routes between trips.** An hour with nothing recorded between a
  stop and the next trip, 4.6 km away, was drawn as a walk the portal never
  showed. A link between a stop and a trip is now only the short step between
  them: at most 15 minutes and 1.5 km.
- **A last check before every write** (`mapmatch/portal/guard.py`), separate
  from the code that builds the points. A matched point must lie on the line
  the portal draws; a stop point must sit where fixes were, between two fixes
  at most 12 hours apart; a link must be a short step between them; a point
  put back must be in the archive. Anything else and the span is refused: the
  trip shows the reason and Dawarich is left exactly as it was, for a push by
  hand and for automatic runs alike.
- **`PUSH_TRIPS_ONLY=1`** for those who want Dawarich changed as little as
  possible: a push swaps each pushed trip's fixes for its matched line and
  touches nothing else. Stops keep their recorded fixes (no half-hourly stop
  points, which sit on one spot and read as duplicates in Dawarich), no links
  are drawn, and trips that are not pushed are not deleted and written again.
- After updating, **Settings → Repair tracks** rewrites what earlier pushes
  wrote, removing the points above.

### Matching: trains on the real rails, long gaps, unreliable fixes (engine 2.10.1)

- **Trains follow the real rails.** The shipped timetable shapes are coarse
  (often one point per station, a straight line between), so a train was
  drawn as chords across the country. Where an OpenStreetMap railway network
  is installed, trains are matched on its rails, by a matcher that follows
  one track (snapping fix by fix ran to a crossover and back whenever two
  fixes fell on different tracks of a double line). A watch-logged trip with
  a train ride in the middle keeps its detailed parts and gets the ride on
  the railway.
- **Holes.** Two fixes a kilometre or more apart with nothing between them
  are tried every way the ground could have been covered in the time (train
  between stations, on foot, by bike, by car). If nothing fits they are left
  unconnected: no line is drawn, the gap can still be routed by hand, and
  its length still counts as distance. Kept GPS is cut at such holes too.
- **Unreliable fixes on a walk.** A fix the phone itself rates at 25 m or
  worse, that a walker would have had to run to, is dropped; and GPS that is
  kept because no path fits is drawn only through the fixes the phone was
  sure of. Walks between tall buildings were being drawn out over water.

## 2.10.0

Found by running the matcher over a real account (a few hundred trips, two
years, phone and watch), looking at every flagged trip and at the trips whose
drawn line strayed furthest from the GPS, and then using the portal as five
kinds of user. Every item below started as a wrong or doubtful trip; each is a rule
with a test, not a correction to that trip.

### Matching

- **No map here.** A trip abroad was reported as unmatched instead of
  "no map, kept as recorded" when a ferry line reached into its port: the
  coverage check now needs a road within 1 km.
- **Trains with nothing recorded on the way.** Two fixes at two stations, a
  railway between them and a pace no walker keeps: a train, drawn along the
  line (timetable shapes first, then an OpenStreetMap network, which adds the
  Underground). Before, these were walks along the road at 20 km/h, or
  straight lines. A walking route across a gap of more than 1 km is refused
  above 9 km/h.
- **A timetable shape that doubles back** (out, back and out again along the
  same track) no longer pins several fixes to one spot.
- **Stray fixes.** A phone hopping hundreds of metres to and fro while parked
  indoors is recognised and the hops dropped; so is one fix that jumps off a
  walk and straight back. A fix that pulls the matched line round the block
  is left out when the line is better without it (reported as dropped,
  "hook"), also at the first and last hop of a segment.
- **Routed gaps.** A route several times longer than the short hop it fills
  is tried on foot, then drawn straight. Legs of a longer route that go a
  long way round between two fixes are cut out. Times along a routed gap are
  pinned to the fixes it passes, so its speed is the speed travelled.
- **Arrived and stayed.** Twenty minutes within 100 m at the start or end of
  a trip is a stop that keeps its points, not a walk with a guessed route.

### Speed and travel mode

- **Speed.** With a fix every half minute or more, the phone's own reading is
  one instant (a junction, a bend) and made drives look like rides. The speed
  is now never less than the ground covered allows, judged on the hop before
  and the hop after so one GPS jump cannot fake it. The portal's speed chart
  uses the same reading.
- **Ride or drive.** After the speed-based label the segment was matched as
  the other mode too and the better fit won by a hair, so a ride that never
  passed 30 km/h became a drive (any ride on ordinary streets fits the car
  network). Fit now only decides when it is clearly better.
- **One vehicle between stops.** A "ride" that touches a drive with no stop
  between them is the car slowing down; a short quick hop next to a car
  journey is the car too.

### Knowing what to look at

- **Area between the GPS and the drawn line**, per trip: the average distance
  apart, the widest part and the longest way round, each with a Show button.
  A new sort, "Furthest from the GPS".
- **Needs a look** no longer counts GPS kept point for point along a path the
  map does not have; it counts straight lines across holes, and a line that
  goes a long way round. Each flagged trip lists why, stretch by stretch, and
  can be passed with "Looks right" (a later match is judged again).

### Portal

- Trips: a to-do list in order of urgency, a review queue, route thumbnails,
  the reason on each flagged row, a map that opens on where most trips are.
- "In Dawarich" card: how much is pushed, with **Push** and **Undo all** side
  by side; **Undo push** on a trip. Every push and undo asks first and says
  whether test mode (`PUSH_DRY_RUN`) is on; test mode shows in the top bar.
- Map button on both maps: matched routes, every original GPS point, line
  thickness, map style. The original track is drawn above the matched line
  (it was hidden under it), points are large enough to see.
- Moving points: the press takes the nearest point within reach instead of
  needing to land on a 5 px dot, and it works on touch screens.
- Phones: tabs at the bottom, 44 px targets, a shorter map.
- Plain-language errors, a three-step "How this works", "Sync" is
  "Get new points".
- Settings for running as part of another site: `PORTAL_HOME_URL`,
  `PORTAL_EXTRA_CSS`, `PORTAL_THEME_KEY`, and an offline basemap
  (`PORTAL_PMTILES_URL`, `PORTAL_PMTILES_ASSETS`) so viewed areas are not sent
  to a map host. `REQUIRE_ADMIN=1` refuses to start without an admin account.

### Safety

- A push checks that Dawarich returned only points inside the span it asked
  for before deleting any, and notes the span before deleting so an
  interrupted write always shows as needing "Put them back".
- `PUSH_DRY_RUN` set to anything but 0/false/no/off means dry run.

### Tools

- `mapmatch.tools.railways` reads the extract in two passes: a whole country
  in about 200 MB instead of several GB.

### Known limits

- The app's own undo puts every point back at the same second and place, but
  as new rows: Dawarich columns the portal never read (battery, raw data,
  import and tracker ids) are not restored. Keep a database backup if those
  matter to you.
- A push rewrites everything in its span from the portal's archive, so points
  deleted in Dawarich after they were fetched would come back (from reading
  the code; not tested).
- Sparse phone logging cannot always tell a fast ride from a slow drive.
