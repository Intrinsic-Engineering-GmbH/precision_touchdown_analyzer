# touchdown_analyzer

Video-based landing accuracy measurement for glider spot-landing competitions
(Ziellandewettbewerb).

A single fixed camera films the touchdown zone of the landing strip
continuously. The analyzer finds each landing in that stream, cuts out the
touchdown — a few seconds before and after — as its own clip, detects the first
contact of the main wheel and reports how far it was from the target line in the
middle of the landing field.

> **Status: early development.** The repository currently contains the project
> skeleton. See [`docs/design.md`](docs/design.md) for the implementation plan
> and [Roadmap](#roadmap) for what is built next.

## Features

### Feature 1 — Automatic landing segmentation

The continuous video is recorded without gaps and analyzed in parallel. Each
detected touchdown is exported as its own clip — a fixed window around the
contact instant, by default **3 s before to 5 s after** (~12 MB), not the whole
landing:

```
2026-07-18_14-32-07_HB-3123.mp4
2026-07-18_14-35-51_D-8842.mp4
2026-07-18_14-39-12_UNKNOWN-0003.mp4   # not yet assigned - pending review
```

Identification comes from the landing sequence rather than from the image: at
75 m the painted registration is only ~15 px high, too small to read reliably.
In order of preference — **FLARM / OGN** (matching the touchdown timestamp
against the Open Glider Network track identifies the aircraft automatically),
a **start list plus landing order**, and **manual confirmation** in the review
UI. OCR is still attempted opportunistically and offered as a proposal when it
is confident. Every clip is renamed once the aircraft is confirmed.

### Feature 2 — Touchdown displacement

The signed distance between the first main-wheel touchdown and the target line is
estimated in metres:

- **Longitudinal** — along the strip axis, positive = beyond the target line.
- **Lateral** — offset from the strip center line.

The measurement window is **±19 m** around the target line — what the installed
camera sees from 75 m, and the agreed scope of the measurement. Landings that
touch down outside it are still detected and clipped, and are reported as
`< −19 m` or `> +19 m`: "clearly short" or "clearly long" rather than a number.

This requires a one-time **optical calibration** per camera position: the strip
edges and the target line are marked with known ground points, from which a
homography (image plane → ground plane) is computed. Because the wheel contact
point lies exactly on the ground plane, that mapping is geometrically exact at
the moment of touchdown.

## How it works

```
continuous stream
   │
   ├─► recorder ──────► 10 s raw segments on disk        (nothing is ever lost)
   │
   └─► analysis worker
          │  background subtraction + tracking (camera is fixed)
          │  state machine: IN_FRAME → CONTACT → GONE
          ├─► clip cutter ──► touchdown clip, -3 s / +5 s (ffmpeg, from segments)
          ├─► touchdown estimator ──► contact frame (sub-frame accurate)
          │        └─► homography ──► displacement in metres
          └─► OCR ──► registration proposal
                          │
                          ▼
                    review UI (judge confirms) ──► results.csv + SQLite
```

## Measurement setup

Hardware: one fixed **AXIS P1485-LE** (1920×1080, 50/60 fps, 10.8–28.2 mm,
HFOV 29°–11°), mounted side-on to the strip at **75 m**, looking at the target
line.

| At 75 m, lens at the wide end | |
|---|---|
| Field of view | 38.8 m wide → **±19.4 m** around the target line |
| Scale at the target line | 49.5 px/m (20 mm per pixel) |
| Travel per frame @ 60 fps | 0.42 m |
| Motion blur @ 1/1000 s | ≈ 1.2 px |

> **Measurement window:** the camera covers ±19.4 m around the target line and
> films the touchdown only — roughly 1.5 s per landing — rather than the whole
> approach and rollout. `docs/design.md` §2.2 keeps two upgrade paths documented
> (a wider-lens P1465-LE, or a second P1485-LE) should a wider window ever be
> wanted.

Key points for the site:

- **Retention is asymmetric.** Raw footage is the bulk (5–7 GB/h); clips are
  ~12 MB. Keep the raw segments a couple of weeks so a competition can be
  re-processed with improved algorithms, keep clips and results indefinitely.
- **Frame rate over resolution.** At 25 m/s one frame at 60 fps is 0.42 m, one at
  30 fps is 0.83 m — timing dominates the error budget, while 20 mm/px of spatial
  resolution is far better than needed. Make sure the camera really runs 60 fps
  (Forensic WDR caps it on many Axis models).
- **Mast height > 8 m (decided).** It hardly changes the longitudinal result,
  but at 3–4 m the ground plane is seen at ~3° and the cross-strip offset
  degrades to ~0.5 m per pixel; above 8 m it is better than 0.2 m per pixel.
  Aim the camera ~6° down with the ground line about two thirds down the image,
  leaving 10–15 m of airspace above the strip in view.
- **Lock the camera down:** fixed zoom and focus, autofocus off, EIS off, barrel
  distortion correction off, Zipstream off (it drops frames), max shutter
  1/1000 s. See `docs/design.md` §2.4 for the full checklist — several of these
  settings silently invalidate the calibration if left on.
- **Identification does not come from the image.** At 75 m a 30 cm painted
  character is ~15 px high, against the ~22 px+ OCR needs, and the glider crosses
  the field in ~1.5 s. Plan for FLARM/OGN or a start list instead — an OGN
  receiver at the field is the cheapest way to make clip naming automatic.
- **Calibration markers:** at least four points with known positions — both ends
  of the target line plus two further points along the strip axis (e.g. ±25 m).
  Measured once with a tape; valid as long as the camera does not move.

Expected accuracy in this configuration: **≈ 0.2 m**, dominated by touchdown
timing rather than by the optical mapping. See `docs/design.md` §4.4 for the full
error budget and §4.5 for the validation plan.

Site planning helper:

```powershell
python tools/site_geometry.py --distance 75 --mast 8
```

## Requirements

- Python 3.10 or newer
- [ffmpeg](https://ffmpeg.org/) on `PATH` (recording and clip cutting)
- Git, and VS Code with the Python extension (recommended)

## Setup (Windows / PowerShell)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
pip install -e .
```

On Linux/macOS use `python3 -m venv .venv` and `source .venv/bin/activate`.

## Usage

### Capture (implemented — this is M0)

```powershell
# 1. before committing to a flying day: verify the camera really delivers what
#    the measurement needs (60 fps, fixed GOP, regular frame spacing)
touchdown-analyzer probe --source rtsp://camera/stream

# 2. record continuously into 10 s segments; restarts itself across dropouts
touchdown-analyzer record --source rtsp://camera/stream --session 2026-07-18

# ... or stop automatically after a set time
touchdown-analyzer record --source rtsp://camera/stream --session 2026-07-18 --duration 28800

# 3. afterwards, index the segments and check continuity
touchdown-analyzer index --session 2026-07-18
```

### Browser control

The same three operations, driven from a browser — useful when the recording
machine sits in a shed and you want to check on it from a phone:

```powershell
pip install -e ".[ui]"
touchdown-analyzer serve                      # http://localhost:8080
touchdown-analyzer serve --host 0.0.0.0       # also reachable from the field WiFi
touchdown-analyzer serve --port 8088          # if 8080 is taken
```

In VS Code, **F5** starts it and opens the browser automatically.

> Port 8000 is avoided by default: Windows reserves it on many machines
> (http.sys, or a Hyper-V dynamic range), where binding fails with a bare
> `WinError 10013`. `serve` checks the port up front and tells you what to do.
> `netsh interface ipv4 show excludedportrange protocol=tcp` lists the
> reserved ranges.

Start and stop recordings, run the pre-flight check and read its verdict, watch
frames / fps / dropped / segments / disk live, and index a finished session.
The page polls once a second and remembers the camera URL locally, so it is not
retyped every morning.

> **No login.** `--host 0.0.0.0` exposes the page to everyone on the network,
> and the source field holds an RTSP URL with the camera password in it. Only do
> that on a network you trust. The recorder itself stays stdlib-only, so
> `touchdown-analyzer record` keeps working whether or not the UI is installed.

`probe` records a short sample and inspects the actual frames. It is the only
way to catch the settings that silently ruin a measurement — Forensic WDR
capping the frame rate, Zipstream dropping frames, dynamic GOP — because all
three look fine in a live view. A `FAIL` means do not record yet.

### Calibration (implemented — this is M1)

One-time per camera position. Open `http://localhost:8080/calibration`, grab a
still, click each marker, solve.

**Marker layout — three pairs, one on each edge of the strip:**

```
                 x = -15 m        x = 0 (target)       x = +15 m
  far edge  ────────●────────────────●───────────────────●────────
                    │                │                   │
  near edge ────────●────────────────●───────────────────●────────
                              ▲ camera, 75 m this side
```

> **Markers along the strip axis alone will not work.** Three cones at −15 / 0 /
> +15 m on the centre line are *collinear*, and a homography fitted to collinear
> points is not merely inaccurate — it is undetermined, because infinitely many
> mappings fit equally well. The pair on each edge supplies the lateral spread
> that makes the fit solvable. The tool refuses a degenerate set rather than
> returning a plausible-looking wrong answer.

Four markers is the minimum; six gives a better fit and a residual worth
trusting. Measure each with a tape from the target line and enter the real
numbers — the layout does not have to be symmetric.

**Click precision matters more than tape precision.** At an 8 m mast the ground
is seen at ~6°, so one vertical pixel is ~0.19 m of ground depth:

| Click error | Survey error | Residual |
|---|---|---|
| 0 px | 5 cm | 0.039 m — passes |
| 0.5 px | 3 cm | 0.060 m — passes |
| 1.0 px | 1 cm | 0.107 m — fails |
| 2.0 px | 0 cm | 0.213 m — fails |

A sloppy tape measure is survivable; sloppy clicking is not. The calibration
page therefore shows a 5× magnifier that follows the cursor — use it. Use
high-vis cones too: at 75 m a 30 cm marker is only ~15 px.

M1 is met when the RMS residual is **under 0.10 m**. The page reports the
residual per marker, so a single misclick shows up as one bad row rather than
quietly degrading the whole fit. Solving writes `config/calibration.json`.

Re-calibrate whenever the camera moves, or its zoom or focus changes.

### Frame viewer and ground truth

`http://localhost:8080/viewer` — pick a session and segment, step frame by
frame (<kbd>←</kbd>/<kbd>→</kbd>, hold <kbd>Shift</kbd> for ten), and click the
point where the main wheel met the ground.

With a calibration saved, the click is projected straight to metres, so the
viewer produces the **hand-marked ground truth M3 is scored against** — measured
against manual annotation, bias < 0.1 m and σ < 0.3 m ([design §7](docs/design.md)).
Each mark appends to `data/raw/<session>/annotations.jsonl`:

```json
{"session": "2026-07-18", "segment": "2026-07-18_14-32-07.mp4", "frame": 300,
 "touchdown_utc": "2026-07-18T12:32:12+00:00", "image_x": 964.2, "image_y": 712.8,
 "world_x": -2.34, "world_y": 0.41, "in_range": true, "aircraft": "HB-3123"}
```

Annotations save with or without a calibration — marking the contact *frame* is
worth doing before the homography exists, since the timing is the harder half.
A touchdown beyond ±19.4 m is stored as a bound rather than a number, which is a
legitimate result for a spot-landing competition, not an error.

> **Click the wheel, never the fuselage.** The homography is exact only for
> points *on* the ground plane. A point 0.8 m up is wrong by metres through the
> same mapping ([design §4.1](docs/design.md)). The viewer magnifies 5× for this
> reason.

Frames are extracted a window at a time (±30 frames, half a second either way at
60 fps) in one ffmpeg pass, so stepping is instant and re-centres transparently
when you walk off the end.

### Analysis (planned)

```powershell
# one-time, per camera position: click the known ground markers in a still frame
touchdown-analyzer calibrate --source rtsp://camera/stream --out config/calibration.json

# detect landings, cut clips, measure displacement
touchdown-analyzer reprocess --session 2026-07-18

# review and confirm registrations / touchdown frames in the browser
touchdown-analyzer review --session 2026-07-18

# export results
touchdown-analyzer export --session 2026-07-18 --format csv
```

## What a recording session produces

```
data/raw/2026-07-18/
  2026-07-18_14-32-07.mp4   10 s segments, -c copy, camera PTS untouched
  2026-07-18_14-32-17.mp4
  ...
  session.json              source, fps, codec, UTC offset, tool versions
  segments.jsonl            per segment: start_utc, duration, frames, fps, size
  gaps.jsonl                any recorder downtime, with duration
  recorder.log
```

`session.json` plus `segments.jsonl` are what make the footage analysable: they
turn *"touchdown at 14:32:09.4"* into *"this file, this frame"*. Two clocks are
kept deliberately apart — absolute time comes from the segment filename
(recorder wall clock, NTP synced, good enough to match an OGN track), while
relative time inside a segment comes from the camera's own PTS, which is the
regular clock the sub-frame touchdown fit depends on. The recorder therefore
never passes `-use_wallclock_as_timestamps`: that would bake network jitter
into exactly the signal being measured.

Where those two clocks disagree, `index` reports it as an **overlap**. On a
correctly configured camera it should be near zero; a growing overlap across a
day means the camera clock is drifting against the recorder's.

**`gaps.jsonl` and `index` measure different things — trust `index`.**
`gaps.jsonl` records how long the *recorder process* was down; `index` derives
what footage is actually missing from the segment files themselves. The latter
is normally larger, because ffmpeg writes out buffered frames before exiting.
`index` is the authoritative answer to "what did we not capture".

### Before the first real session

- Sync time: NTP on the camera **and** the recording machine.
- Film a phone showing GPS/NTP time for the first 10 s — a hard sync anchor.
- Lay markers every 5 m along the strip edge for at least one session; M1 and
  M3 validation need them (`docs/design.md` §4.5).
- Log each landing by hand: time, nearest marker, landing direction, wind.
  Without that there is no ground truth to validate against.
- Budget disk: 5–7 GB/h, ~60 GB for a flying day.

## Layout

```
src/touchdown_analyzer/
    config.py             capture settings and camera targets
    cli.py                probe | record | index | serve  (analysis verbs planned)
    capture/              stdlib only - nothing here can fail to import
        ffmpeg.py         toolchain discovery and ffprobe wrappers
        probe.py          pre-flight camera check against docs/design.md 2.4
        recorder.py       ffmpeg segment recorder, watchdog, disk guard
        segments.py       segment index, instant -> (segment, frame)
        frames.py         frame-exact stills for calibration and the viewer
    calibration/
        homography.py     image plane -> ground plane, with degeneracy guards
    control/              browser UI; optional, needs the [ui] extra
        service.py        supervises recording, calibration, frames (stdlib only)
        app.py            FastAPI routes
        static/           capture control, calibration, frame viewer
docs/design.md            architecture and implementation plan
tools/site_geometry.py    camera coverage / resolution / error calculator
tests/                    pytest test suite
tests/data/               small tracked test fixtures
config/                   calibration and session configuration (tracked)
data/                     recorded video and clips - not tracked by git
output/                   results, exports, overlays - not tracked by git
```

The capture package is deliberately **stdlib-only**: the recorder runs
unattended for a whole flying day, so it carries no dependency that can fail to
import. ffmpeg does the actual work. `control/` adds FastAPI on top but only
drives that same recorder, so a broken UI never costs a landing. The rest of
the heavy stack (OpenCV, OCR) arrives with the analysis side.

Raw video and generated output stay out of the repository (see `.gitignore`).
Only small fixtures needed by the tests belong in `tests/data/`.

## Roadmap

| Milestone | Content |
|-----------|---------|
| M0 | Record a full flying day, collect 20–50 landings as a test set — *capture tooling built, footage outstanding* |
| M1 | Calibration tool + verification against tape-measured ground points |
| M2 | Feature 1: detection, landing segmentation, clip cutting |
| M3 | Feature 2: touchdown estimation + validation vs. manual annotation |
| M4 | Identification (OGN or start list) + review UI |
| M5 | Hardening for a full season of club use |

## Development

```powershell
pytest              # run the test suite
ruff check .        # lint
ruff format .       # format
mypy                # type check
```

## Notes

Filming at the airfield records people as well as aircraft. Agree on signage, a
retention period for raw footage and who may access it before the first session.
