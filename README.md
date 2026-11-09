# dawarich-smart-mapmatch

State-first map matching for GPS traces in a self-hosted Dawarich.

Every input fix gets one state: `auto`, `bicycle`, `pedestrian`, `rail`, or
`raw`. The output line is built from that state sequence. Connectors are added
after matching only where two visible state parts do not meet; each connector
is routed with the profile that fits both sides, and falls back to a straight
line only when routing is impossible.

## How it works

1. **Prepare** - sort fixes, drop exact duplicates, mark stationary runs,
   spikes, and long data gaps. Those states stay raw.
2. **Hypotheses** - for each movement section, ask Valhalla's global
   `trace_attributes` matcher for three profiles (auto, bicycle, pedestrian)
   and ask the local rail shapes for a train hypothesis.
3. **Infer** - a Viterbi pass picks a per-fix state from the candidate points,
   with penalties for mode changes, rail/road switches, and impossible jumps.
   Unmatched fixes are not replaced by the input coordinate; they become raw.
4. **Assemble** - matched runs follow the matched shape, raw runs keep the
   recorded fixes. Stationary and gap states are never routed.
5. **Review** - contiguous state runs become clickable parts. Long GPS
   dropouts inside a road/walk/cycle part are filled with a route, and a
   routed connector is inserted wherever adjacent parts leave a real gap. A
   train seam is never routed as a footpath or road; it stays straight.

When Valhalla abandons a fast road section because the fixes are sparse, a
road route between the raw run's endpoints is used only if it explains most of
the fixes, the length is plausible, and the timing is plausible. Slow or
off-road runs are never rescued that way.

## Run

```bash
export VALHALLA_URL=http://192.168.0.76:8002
python3 run.py --raw data/raw_v5.json --ids data/routes_50.json --workers 4
```

Outputs:

- `out/tracks.json` - generated points, parts, raw points
- `out/review.json` - review-page document
- `out/summary.json` - per-route metrics

`data/raw_v5.json` is a private input and is not part of the repository.

## Review page

`out/review.json` is the data contract for the part-level review page used by
the cleaner. It has 50 routes, each split into contiguous state runs. Parts use
full geometry; the base line is thinned only for browser size.

## Layout

- `smartmap/segment.py` - stationary, spike, gap, movement sections
- `smartmap/infer.py` - candidate generation, Viterbi, rail, road rescue
- `smartmap/assemble.py` - state runs to points and parts
- `smartmap/valhalla.py` - `trace_attributes` and anchor routing
- `smartmap/rail.py` - GB rail shapes
- `smartmap/review.py` - review document builder
- `smartmap/run.py` - batch runner
