# touchdown_analyzer

Video-based landing accuracy measurement for glider spot-landing competitions
(Ziellandewettbewerb).

A single fixed camera films the landing strip continuously. The analyzer finds
each landing in that stream, cuts it into its own clip, detects the first
touchdown of the main wheel and reports how far that touchdown was from the
target line in the middle of the landing field.

> **Status: early development.** The repository currently contains the project
> skeleton. See [`docs/design.md`](docs/design.md) for the implementation plan
> and [Roadmap](#roadmap) for what is built next.

## Features

### Feature 1 — Automatic landing segmentation

The continuous video is recorded without gaps and analyzed in parallel. Each
detected landing (approach → touchdown → rollout → strip cleared) is exported as
a separate clip with a few seconds of padding on both sides:

```
2026-07-18_14-32-07_HB-3123.mp4
2026-07-18_14-35-51_D-8842.mp4
2026-07-18_14-39-12_UNKNOWN-0003.mp4   # OCR unsure - pending review
```

The aircraft registration (Immatrikulation) or competition number is read by OCR
from the rollout phase, where the glider is large and slow in the frame. OCR is a
*proposal*: every landing is confirmed or corrected by a human in the review UI
before the clip gets its final name.

### Feature 2 — Touchdown displacement

The signed distance between the first main-wheel touchdown and the target line is
estimated in metres:

- **Longitudinal** — along the strip axis, positive = beyond the target line.
- **Lateral** — offset from the strip center line.

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
          │  landing state machine: APPROACH → TOUCHDOWN → ROLLOUT → CLEARED
          ├─► clip cutter ──► per-landing mp4 (ffmpeg, cut from raw segments)
          ├─► touchdown estimator ──► contact frame (sub-frame accurate)
          │        └─► homography ──► displacement in metres
          └─► OCR ──► registration proposal
                          │
                          ▼
                    review UI (judge confirms) ──► results.csv + SQLite
```

## Measurement setup

- **Camera position:** side-on, perpendicular to the strip, roughly level with
  the target line, 30–50 m off the strip, elevated 3–5 m. This geometry gives the
  best resolution exactly where it matters and keeps the whole plausible
  touchdown range in frame.
- **Frame rate matters more than resolution.** A glider touches down at ~25 m/s,
  so one frame at 30 fps is already 0.8 m of travel. Use **≥ 120 fps** if
  possible; the estimator additionally fits the descent and ground phases and
  intersects them, which recovers sub-frame precision.
- **Global shutter** preferred. Rolling-shutter action cams smear fast lateral
  motion and skew the measurement.
- **Calibration markers:** at least four points with known positions — both ends
  of the target line plus two further points along the strip axis (e.g. ±25 m).
  Measured once with a tape; re-used as long as the camera does not move.

Realistic accuracy with this setup: on the order of **±0.3 m**, dominated by
touchdown timing rather than by the optical mapping. Good enough for training
feedback; see `docs/design.md` for the error budget and the validation plan.

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
| M4 | OCR registration proposal + review UI |
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
