# Changes

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
