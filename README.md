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

## Usage (planned CLI)

```powershell
# one-time, per camera position: click the known ground markers in a still frame
touchdown-analyzer calibrate --source rtsp://camera/stream --out config/calibration.json

# run a session: record continuously, segment landings, measure, name clips
touchdown-analyzer run --source rtsp://camera/stream --session 2026-07-18

# re-analyze recorded footage without touching the camera
touchdown-analyzer reprocess --session 2026-07-18

# review and confirm registrations / touchdown frames in the browser
touchdown-analyzer review --session 2026-07-18

# export results
touchdown-analyzer export --session 2026-07-18 --format csv
```

## Layout

```
src/touchdown_analyzer/   package source (src layout)
docs/design.md            architecture and implementation plan
tools/site_geometry.py    camera coverage / resolution / error calculator
tests/                    pytest test suite
tests/data/               small tracked test fixtures
config/                   calibration and session configuration (tracked)
data/                     recorded video and clips - not tracked by git
output/                   results, exports, overlays - not tracked by git
```

Raw video and generated output stay out of the repository (see `.gitignore`).
Only small fixtures needed by the tests belong in `tests/data/`.

## Roadmap

| Milestone | Content |
|-----------|---------|
| M0 | Record a full flying day, collect 20–50 landings as a test set |
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
