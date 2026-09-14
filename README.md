# Precision Touchdown Analyzer

Measures glider spot landings from one fixed camera: where the main wheel first
touched the ground, in metres from the target line, with a clip and a proof
image per landing, an aircraft identification from OGN, and points under the
club's scoring rules. Built for the Ziellandewettbewerb at LSTB (Bellechasse).

The camera films the touchdown zone continuously. A recorder writes the stream
to disk in 10 s segments; the analyzer reads those segments, finds every
aircraft, tracks its wheel to the instant of contact and maps that pixel onto
the ground plane through a one-time calibration. A judge confirms each landing
in the browser.

## Install

**Windows** - run `PrecisionTouchdownAnalyzer-Setup-<version>.exe`. It installs
per user (no administrator rights) under `%LOCALAPPDATA%\Programs`, adds Start
menu and desktop shortcuts and an entry in *Apps & features*. Recordings,
results and the configuration go to `%LOCALAPPDATA%\PrecisionTouchdownAnalyzer`
and survive an uninstall. Unattended: `Setup.exe /S [/D=C:\path]`.

**Debian / Ubuntu** (amd64, Python 3.12 or 3.13):

```sh
sudo apt install ./precision-touchdown-analyzer_<version>_amd64.deb
sudo systemctl enable --now touchdown-analyzer      # web server on port 8080, data in /var/lib/precision-touchdown-analyzer
```

The package carries its wheels and builds a venv under `/opt` on install, so no
network is needed on the target machine; `ffmpeg` is a dependency. The control
window is in the application menu and as `touchdown-analyzer-launcher`.

**From source** - Python 3.12+, [ffmpeg](https://ffmpeg.org/) on `PATH`:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pip install -e ".[ui,analysis]"
```

## Run

The installed program opens a **control window**: start and stop the web
server, choose the port and whether the field WiFi may reach it, set the
airfield for OGN identification (type the ICAO code, *Look up* fetches
position, elevation and time zone from the OGN FlightBook, *Save* writes
`config/ogn.json` and hands it to the running server), and watch the services -
recorder, analysis worker, OGN feed, calibration, ffmpeg - with the server log
underneath. From a checkout the same window is `touchdown-analyzer launcher`.

Without the window:

```powershell
touchdown-analyzer serve            # http://localhost:8080
touchdown-analyzer serve --host 0.0.0.0   # reachable from the field WiFi (no login - trusted networks only)
```

In VS Code, **F5** or **Ctrl+Alt+B** does the same. The browser UI has five
programs:

| Program | What you do there |
|---|---|
| **Calibration** | Once per camera position: grab a still, click the surveyed markers (three pairs, one on each strip edge), solve. Aim for a residual under 0.10 m. |
| **Capture** | Pre-flight check of the camera, start/stop the recording, live viewfinder, disk and frame-rate status. |
| **Frames** | Step through a segment frame by frame and hand-mark a touchdown (the ground truth the analysis is scored against). |
| **Landings** | The judge's page: every event of the day with its measured offset, the contact frame with the geometry drawn on, a scrubber and loop, a magnifier, the OGN proposal, and *Confirm / Reject / Use this frame*. *Analyse session* processes a finished day; *Follow recording* analyses while recording. |
| **Scoring** | The club's rules - points on the line, deduction per metre short and per metre long, floor, decimals - with the scale drawn out and the day's ranking. |
| **Board** (`/board`) | Read-only results for a big screen: one row per aircraft with its number of confirmed landings, each landing's offset and points, and the total (points added up over all its landings) that ranks it; the unverified landings listed beside without distance or score; refreshed every 5 s, follows the newest session. `?session=2026-09-13`, `?theme=light`, `?refresh=10`, `?page=8` (seconds per page when the list is long). |

The same things from the command line:

```powershell
touchdown-analyzer probe                       # is the camera set up right? (60 fps, no Zipstream...)
touchdown-analyzer record --session 2026-09-13 # record until Ctrl+C
touchdown-analyzer index --session 2026-09-13  # continuity report of a session
touchdown-analyzer analyze --session 2026-09-13 [--fresh] [--no-clips]
```

The camera URL is given once with `--source` (or in the UI) and remembered in
`.env`, which is git-ignored because it carries the camera password.

## What comes out

```
data/raw/<session>/         10 s segments, session.json, segments.jsonl, recorder.log
data/landings/<session>/    landings.json - every event, measurement, judge's decision, history
                            <time>_<REG>_overlay.jpg - the contact frame with the geometry drawn in
                            <time>_<REG>.mp4 - the landing, -3 s / +5 s
config/calibration.json     the ground-plane mapping           config/scoring.json   the scoring rules
config/ogn.json             the airfield for OGN identification
```

Every measured landing carries an uncertainty. Landings outside the ±19 m
window are reported as a bound (`< −19 m`, `> +19 m`); a wheel that skims the
whole window too low to tell from rolling is reported as *pick frame* for the
judge; take-offs and fly-throughs are listed but not scored.

## Where the numbers come from

Background subtraction finds the aircraft; inside its box the pixels are split
into aircraft and shadow; the black tyre is found along the belly and tracked
as an object through the pass. Two independent cues give the contact instant
at sub-frame resolution - the reach of the shadow under the wheel, which stops
shrinking at contact, and the wheel's apparent depth through the calibration,
which stops moving at contact - and their spread is the uncertainty. The OGN
FlightBook gives the registration and settles landing versus take-off from the
logbook minute. The full method, the error budget and what the first footage
taught are in [`docs/design.md`](docs/design.md).

Accuracy today is limited by the calibration (2.25 m residual against a 0.10 m
target) and a low tripod; a proper survey and the planned 8 m mast are what
bring it to the ±0.3 m the design is built for.

## Development

```powershell
pytest              # tests
ruff check .        # lint
ruff format .       # format
mypy                # types
```

`capture/`, `clips/`, `identify/` and `store/` are stdlib-only so the recorder
can never fail to import at the airfield; OpenCV arrives only with `analysis/`.
Raw video and results stay out of git.

Building the installers (outputs land in `dist/`):

```powershell
.\packaging\windows\build.ps1           # PyInstaller -> Setup exe; put ffmpeg.exe/ffprobe.exe in vendor\ffmpeg\ to bundle them
python packaging\debian\build_deb.py    # downloads the wheels from PyPI, writes the .deb (works on Windows too)
```

The .deb is assembled with the standard library and has so far only been
checked structurally on Windows; the first install on a real Debian machine
should be watched (`journalctl -u touchdown-analyzer`).

Filming at the airfield records people as well as aircraft: agree on signage, a
retention period for raw footage and who may access it before the first session.
