# Design and implementation plan

Scope: measure the displacement between the first main-wheel touchdown of a
glider and the target line of a spot-landing competition, from a continuously
recording fixed camera, and store each landing as its own clip.

Assumptions from the current decisions:

- **One fixed AXIS P1485-LE**, side-on to the strip, 75 m away — both fixed by
  the installation. It films the **touchdown window only** (±19 m around the
  target line, ~1.5 s per landing), not the whole landing; that window is the
  agreed measurement scope, and landings outside it are clipped and bounded
  rather than measured.
- **Aircraft ID by OCR with manual review** — the machine proposes, a human confirms.
- **Purpose: training / club feedback** — target accuracy ~0.3–0.5 m, full
  automation is worth more than provable exactness. The design keeps an audit
  trail anyway, so it can be tightened for official scoring later.

---

## 1. Architecture

Two processes that share a folder, so analysis can never cause a recording gap:

```
┌──────────────┐   10 s mp4 segments   ┌──────────────────────────────┐
│  recorder    │ ────────────────────► │  analysis worker             │
│ (ffmpeg)     │   data/raw/<date>/    │  detect → segment → measure  │
└──────────────┘                       └──────────────────────────────┘
        │                                        │
        │                                        ├─► clips/   (ffmpeg cut)
        │                                        ├─► SQLite   (landings)
        └────────── same files ──────────────────┴─► overlays/ (PNG proof)
                                                 
                                          ┌──────────────┐
                                          │  review UI   │ FastAPI, localhost
                                          └──────────────┘
```

Why the split: a dropped frame in the analyzer costs a measurement, a dropped
frame in the recorder costs the whole landing. `ffmpeg -f segment` writes fixed
length chunks and is extremely hard to disturb. Everything downstream reads those
files, which also makes the whole day **re-processable offline** — essential while
the algorithms are still changing.

### Module layout

```
src/touchdown_analyzer/
    config.py            pydantic settings (camera, paths, thresholds)
    cli.py               typer CLI: calibrate | run | reprocess | review | export
    capture/
        recorder.py      ffmpeg segment recorder, watchdog, disk housekeeping
        segments.py      segment index, PTS ↔ wall-clock mapping
    calibration/
        intrinsics.py    lens distortion (chessboard), optional but recommended
        homography.py    marker picking, findHomography, save/load, reproj error
        drift.py         ORB background match → detect/correct camera movement
    detect/
        background.py    MOG2 foreground, ROI masks
        tracker.py       Kalman + IoU association
        events.py        landing state machine (APPROACH/TOUCH/ROLLOUT/CLEARED)
    touchdown/
        contact.py       wheel contact point extraction per frame
        subframe.py      descent/ground line fit → sub-frame contact time
        cues.py          shadow-gap and dust-puff cross-checks
    measure/
        geometry.py      image → ground plane, signed longitudinal/lateral offset
        uncertainty.py   per-landing error estimate
    identify/
        ocr.py           registration / competition number proposal
        startlist.py     optional CSV start list matching
    clips/
        cutter.py        ffmpeg cut from raw segments
        naming.py        timestamp + registration naming, rename after review
    store/
        db.py, models.py SQLite schema, results export
    review/
        app.py           FastAPI + minimal HTML review UI
```

### Stack

Python 3.11, OpenCV, NumPy, SciPy, ffmpeg (subprocess), Typer (CLI), pydantic
(config), SQLite via `sqlite3`, FastAPI + uvicorn (review UI), PaddleOCR or
EasyOCR (registration), optionally Ultralytics YOLO later if background
subtraction proves insufficient. Everything runs on one laptop; a GPU helps OCR
but is not required if detection runs on a downscaled ROI.

---

## 2. Camera and site

### 2.1 What the AXIS P1485-LE gives us

| | |
|---|---|
| Sensor / resolution | 1/2.8" CMOS, 1920 × 1080 (2.1 MP) |
| Max frame rate | 50 / 60 fps |
| Lens | 10.8 – 28.2 mm varifocal, HFOV 29° – 11°, VFOV 17° – 6° |
| Housing | outdoor, IP66/IP67, OptimizedIR (irrelevant for daytime gliding) |
| Stream | RTSP, H.264 / H.265, Zipstream |

At **75 m**, lens at the wide end (10.8 mm, HFOV 29°):

| | |
|---|---|
| Covered width | **38.8 m — i.e. ±19.4 m around the target line** |
| Scale at the target line | 49.5 px/m (20 mm per pixel) |
| Travel per frame @ 60 fps | 0.42 m |
| Motion blur @ 1/1000 s | 25 mm ≈ 1.2 px |

Run `python tools/site_geometry.py --distance 75 --mast 8` to redo these numbers
for any distance, mast height or lens setting.

### 2.2 The measurement window — ±19 m by design

The camera is a **touchdown-window camera, not a whole-landing camera**. It sees
38.8 m of the strip (±19.4 m around the target line) at 75 m, and that is the
accepted measurement window: what is filmed and measured is the touchdown itself,
shortly before and shortly after — not the approach, not the rollout to a stop.

This simplifies the system considerably compared with observing a whole landing:

1. **The clip is a fixed window around the touchdown** (§3.3), not a
   variable-length "approach to standstill" recording. Clips are ~8 s instead of
   ~30 s: about 12 MB each, so a season of landings is a few gigabytes.
2. **The state machine has three states, not four** (§3.2) — the aircraft enters
   the frame, touches down, and leaves. There is no "strip cleared" condition to
   detect and no dependence on where the glider finally stops.
3. **Identification cannot come from the image** — there is no slow rollout phase
   in view to read a registration from (§3.4).

A glider is in frame for ~1.5 s (38.8 m at 25 m/s, ~93 frames at 60 fps), which
is ample for the descent/ground-line fit that needs ~0.3 s before contact, but it
does mean detection must lock on within a few frames.

Landings that touch down outside the window still get a clip and are reported as
`< −19 m` / `> +19 m` rather than a number — see §3.2. This is expected output for
a spot-landing competition, not an error: "clearly short" and "clearly long" are
legitimate results.

If the window ever needs to be wider, two upgrade paths stay open — neither
requires changing anything downstream of the calibration:

| Option | Setup | Scale | Effect |
|--------|-------|-------|--------|
| D | add an **AXIS P1465-LE, 3–9 mm variant** at HFOV ≈ 40°, demote the P1485-LE to identification | 28 mm/px | ±27 m in one view *and* ~40 px characters for OCR |
| A | a second P1485-LE covering the other half, overlap across the target line | 20 mm/px | ±25 m+, but two calibrations, frame sync, and a seam where landings are densest |

### 2.3 Mast height — decided: > 8 m

Height barely affects the longitudinal measurement (the number that is scored)
but strongly affects the *cross-strip* one, because the ground plane is seen at a
grazing angle:

| Mast | Elevation angle | Cross-strip resolution | Longitudinal error per 1 px vertical (frame edge) |
|------|-----------------|------------------------|---------------------------------------------------|
| 3 m | 2.3° | 0.52 m/px | 0.13 m |
| 4 m | 3.1° | 0.39 m/px | 0.10 m |
| **> 8 m (chosen)** | **> 6.1°** | **< 0.19 m/px** | **< 0.05 m** |
| 12 m | 9.1° | 0.13 m/px | 0.03 m |

**Decision: mount above 8 m.** At that height the lateral offset from the center
line becomes a real measurement (better than 0.2 m per pixel) rather than an
indication, and a single mis-detected pixel costs under 0.05 m longitudinally.
Higher is strictly better for the geometry — 12 m would gain another third — so
take whatever the installation allows above 8 m.

Aiming: tilt the camera down ~6° so it looks at the target line, and frame it so
the **ground line sits about two thirds down the image**. That leaves roughly
10–15 m of airspace above the strip in view — headroom for the approach, for a
high flare and for a balloon-and-recover — while keeping the foreground out of
the way. The vertical field (17°, ≈ 22 m at 75 m) is generous enough that this
costs nothing.

Orientation: put the camera on the side from which it never looks into a low sun
(in Switzerland, looking north). Late-afternoon competition landings into a low
sun are the worst realistic case for detection.

### 2.4 Camera configuration (this matters as much as the code)

Every item below can silently ruin a measurement:

- **Capture mode 1080p, 60 fps.** With the power line frequency set to 50 Hz the
  camera runs 50 fps; outdoors there is nothing to flicker, so select the 60 Hz /
  60 fps mode. Verify the *actual* fps in the live view — **Forensic WDR caps the
  frame rate on many Axis models, so turn WDR off** and confirm.
- **Max shutter 1/1000 s** (1/2000 s in bright sun). Set the Blur/noise trade-off
  fully towards low motion blur, exposure zone on the strip.
- **Zipstream off** (or lowest), and explicitly **no dynamic FPS and no dynamic
  GOP** — Zipstream drops frames in static scenes, which is exactly the
  information the touchdown estimator lives on. Fixed GOP ≈ 60, capped VBR
  12–16 Mbit/s H.264. That is ~5–7 GB per hour, ~60 GB for a flying day.
- **Electronic image stabilisation OFF, barrel distortion correction OFF, defog
  OFF.** Any camera-side geometric processing that can change by itself
  invalidates the calibration. We correct distortion ourselves, once.
- **Focus and zoom locked, autofocus off.** The lens is remote zoom/focus: a
  single autofocus event changes the geometry and therefore the calibration.
  Record the zoom setting; re-zooming means re-calibrating.
- **Force day mode** (no automatic IR-cut switching mid-session) and disable IR.
- **NTP time sync** for clip timestamps; with two cameras this also keeps them
  aligned.
- Fixed IP, and a wired PoE link — a Wi-Fi bridge that drops frames costs whole
  landings.

Rolling shutter: this is a rolling-shutter CMOS, so a fast horizontal pass is
slightly skewed and rows are exposed at slightly different times. The wheel
occupies a narrow band of rows, so the residual timing bias is a few milliseconds
(centimetres) and largely cancels in the descent/ground-line intersection. Worth
measuring once (film a known fast horizontal motion) and then ignoring.

---

## 3. Feature 1 — landing segmentation and clips

### 3.1 Detection

The camera is fixed, so start simple and only escalate if needed:

1. **MOG2 background subtraction** on a downscaled grayscale frame (e.g. 480p is
   plenty for *finding* an aircraft), restricted to two ROIs: the approach sector
   (sky) and the strip.
2. Morphological open/close, contour extraction, filter by area, aspect ratio and
   speed. A glider is a large, fast, high-aspect blob — this rejects people, dogs
   and birds fairly well; birds are the main false positive and are filtered by
   size plus trajectory smoothness.
3. **Tracking**: constant-velocity Kalman filter + IoU association (SORT-style,
   ~150 lines, no dependency needed). One track per aircraft.

Escalation path if that is not robust enough (rain, low sun, crowded strip):
fine-tune a small YOLO (`yolov8n`) on a few hundred frames labelled from the M0
recordings. Keep the interface identical (`detect(frame) -> list[Box]`) so it is a
drop-in swap.

### 3.2 Landing state machine

The camera sees only the touchdown window, so a track has three states:

| State | Enter when | Notes |
|-------|------------|-------|
| `IN_FRAME` | a track passes the size/speed/trajectory filters | measurement starts immediately, there is no approach phase to wait for |
| `CONTACT` | wheel contact detected (§4) | the measured event; defines the clip window and the file timestamp |
| `GONE` | the track leaves the frame or stops | ends the track; the clip window is already known from `CONTACT` |

Guards: minimum track lifetime, minimum traversed distance, and a "two aircraft in
frame" rule that keeps tracks separate rather than merging blobs. A track that
never reaches `CONTACT` and never satisfies the "landed long" test below is
discarded as a false positive (bird, vehicle, person).

Two outcomes are expected rather than exceptional, and both still produce a clip:

- **Landed short** — the track appears at the frame edge *already on the ground*
  (no descent phase; vertical velocity ≈ 0 from the first frames). Result:
  `out_of_range`, bound `< −19 m`.
- **Landed long** — the track leaves the opposite frame edge still airborne (the
  wheel never reaches the ground line). Result: `out_of_range`, bound `> +19 m`.

Telling "landed short" from a normal landing is why the descent-line fit (§4.2)
requires at least ~0.2 s of airborne track: with less than that the landing is
bounded rather than measured from a fit that has nothing to fit.

### 3.3 Clip cutting

The clip is a **fixed window around the touchdown**, configurable, default
**−3 s / +5 s** relative to the contact instant. The pre-roll comes from the
continuous recording, not from an early detection — by the time the aircraft is
detected it is already about to land, and the recorder has the preceding seconds
on disk anyway. For an out-of-range landing the window is anchored on the frame
entry or exit instead.

`segments.py` maps the window to the covering raw segments; ffmpeg concatenates
them and trims with `-ss/-to`. Prefer `-c copy` (fast, lossless); with a GOP of
60 the cut lands within a second of the requested point, and when that is too
coarse the clip is re-encoded — an 8 s clip re-encodes in well under a second.

Sizes: ~12 MB per clip at 12 Mbit/s. Raw footage is the bulk (5–7 GB/h), so the
retention policy is asymmetric — **keep raw segments for a couple of weeks (long
enough to re-process a competition with improved algorithms), keep clips and
results indefinitely**.

Naming: `YYYY-MM-DD_HH-MM-SS_<REG>.mp4`, timestamp = touchdown time (not file
creation), `<REG>` = confirmed aircraft. Until one is assigned the file is
`..._UNKNOWN-<seq>.mp4` and renamed on confirmation — the DB keeps both names so
nothing is lost.

### 3.4 Identification of the aircraft

With option C there is no tele camera, and the glider crosses the field in ~1.5 s
without a readable rollout phase. **OCR cannot carry identification here** — a
30 cm character is ~15 px at the wide end. So identification is built the other
way round: the sequence identifies the aircraft, and the image only confirms it.

In order of preference:

1. **FLARM / OGN (recommended).** Nearly every glider transmits FLARM, and an
   Open Glider Network receiver (an SDR on a Raspberry Pi at the field, or an
   existing nearby OGN station) gives aircraft ID, position and time at ~1 Hz.
   Matching a landing's touchdown timestamp against the OGN track identifies the
   aircraft automatically and reliably — the very thing the camera cannot do.
   GNSS accuracy of a few metres is useless for the *measurement*, but it is
   ample for *identification*, and the track is a free sanity check on the
   detected touchdown time.
   Caveats: gliders whose owners set the OGN no-track / no-record flag must be
   excluded and identified manually; reception at ground level right at
   touchdown can drop, so match on the approach track rather than the last fix.
2. **Start list + landing order.** Import the competition start list; landings
   are numbered in sequence and matched to it. Cheap, no hardware, and in a
   competition the order is usually known anyway.
3. **Manual confirmation in the review UI.** The judge names or corrects the
   aircraft while reviewing the landing. This is the fallback under every
   scheme, and with a start list it is one click per landing.
4. **Opportunistic OCR.** Still attempted on the best few frames, with the
   existing regex + voting logic, and offered as a proposal when the confidence
   is high. Expect a low hit rate at this distance; treat any success as a bonus,
   never as the primary path.

Until an aircraft is assigned, the clip is named `..._UNKNOWN-<seq>.mp4` and
renamed on confirmation; the database keeps both names.

## 4. Feature 2 — touchdown displacement

### 4.1 Calibration (once per camera position)

**Lens.** If the camera is wide-angle (action cams especially), do a one-time
chessboard calibration (`cv2.calibrateCamera`) and undistort. Barrel distortion of
a GoPro-class lens is worth metres at the frame edges — this step is not optional
for such cameras.

**Ground plane.** Place ≥ 4 markers with known positions, measured with a tape:
both ends of the target line, plus two points on the strip axis at, say, ±25 m
(6–8 markers give a better fit and a real residual). In the calibration tool the
user clicks each marker in a still frame and enters its ground coordinates; the
tool computes `H = cv2.findHomography(img_pts, world_pts, cv2.RANSAC)` and saves
it with the marker set and the **reprojection error**, which is the honest quality
number to display.

Define the world frame as: origin on the target line at the strip center line,
x = along the strip (positive = beyond the line, in landing direction),
y = lateral (positive = right seen from behind).

**Why a plane homography is enough:** the contact point of the wheel is *on* the
ground plane at the instant of touchdown, so the mapping is exact there. This is
also why the estimator must find the **wheel contact point**, never the fuselage
centroid — a point 0.8 m above ground is off by metres through the same mapping.

**Drift.** Store ORB features of the static background at calibration time.
Before each session and every ~30 min, re-match; if the camera moved slightly,
estimate the small affine correction and warn; if it moved a lot, refuse to
measure and ask for re-calibration. A bumped tripod is the most likely way to get
plausible-but-wrong numbers.

### 4.2 Detecting the contact instant

Primary method, robust and cheap:

1. In each frame of the final approach, take the tracked silhouette and extract
   the **lowest point of the main-wheel region** (bottom of the fuselage contour
   between the wing root and the nose, in image coordinates).
2. Plot that y over time. It has two regimes: a **descent** (steep, roughly
   linear over the last ~0.3 s) and a **ground run** (y follows the ground line,
   nearly constant in the rectified frame).
3. Fit a line to each regime robustly (RANSAC / Theil–Sen) and **intersect them**.
   The intersection is the contact time — with *sub-frame* resolution, which is
   what beats the frame-rate limit. Uncertainty follows from the fit residuals.

Cross-checks (used to validate, and to flag low-confidence landings):

- **Shadow gap:** in sunshine the gap between glider and its shadow closes to
  zero at touchdown. Very precise when the sun cooperates, useless when overcast —
  hence a check, not the primary method.
- **Dust / grass puff:** localized frame differencing in a small window around the
  predicted contact point; a sudden burst of high-frequency change confirms it.
- **Vertical velocity → 0** in rectified world coordinates.

If the cues disagree by more than ~2 frames, mark the landing `needs_review` and
let the judge step frames in the review UI.

Special cases to handle explicitly:

- **Bounce.** Report the *first* contact, but also store subsequent contacts —
  a bounced landing is legitimately interesting and the naive "lowest y" logic
  would otherwise pick the wrong one.
- **Tail wheel first.** Some gliders touch the tail wheel first. Restrict the
  contact search to the main-wheel region, and record which wheel touched first.
- **Wheel hidden in tall grass.** Detect by contact point ending up below the
  ground line and clamp with a warning.

### 4.3 From contact point to metres

```python
p_img   = undistort(contact_point_px)
p_world = perspective_transform(p_img, H)      # (x, y) in metres
longitudinal = p_world.x                       # signed distance to target line
lateral      = p_world.y                       # offset from center line
```

**Both landing directions are covered by one calibration.** The camera looks at
the strip from the side, so a landing from the east and one from the west produce
the same ground-plane mapping; only the sign convention flips. The tracker
already knows the direction of travel, so `geometry.py` takes it as input and
reports the longitudinal offset as positive = *beyond* the target line in the
direction that aircraft was landing. Store the direction with the result so a
day with a wind shift stays interpretable.

Store both, plus the frame index, the sub-frame time, the cue agreement and an
uncertainty estimate. Render an **overlay PNG** (contact frame + projected target
line + measured distance) — the artefact that makes the number believable to a
pilot.

### 4.4 Error budget

For the P1485-LE at 75 m, 8 m mast, 60 fps, lens at the wide end:

| Source | Contribution | Note |
|--------|--------------|------|
| Timing, raw frame rate | 0.42 m | 25 m/s ÷ 60 fps |
| Timing after sub-frame fit | **0.08 – 0.15 m** | descent/ground line intersection |
| Contact point extraction (2 px) | 0.04 m | 20 mm/px at the target line (0.06 m at option D's 28 mm/px) |
| Vertical mis-detection (1 px) → longitudinal | 0.05 m | at the frame edge, 8 m mast |
| Homography + marker survey | 0.05 – 0.15 m | tape-measured markers, check the residual |
| Lens distortion, uncorrected | up to 0.3 m at the frame edges | one-time chessboard calibration removes it |
| Camera drift (bumped mast) | unbounded | ORB drift check, refuse when large |
| Heat shimmer over grass | 0.05 – 0.2 m on hot afternoons | worse the further back the camera stands |

Realistic total: **≈ 0.2 m RSS**, dominated by touchdown timing. 60 fps is
therefore not optional — at 30 fps the same setup lands around 0.4 m.

### 4.5 Validation

- Manually annotate the contact frame of ~30 landings from M0; compare bias and σ.
- Lay markers every 5 m along the strip edge, photograph, verify the mapping
  measures the known spacing.
- Roll a wheel to a tape-measured point and "land" it — a static ground truth for
  the geometry alone, independent of the timing question.

---

## 5. Data model

```sql
landing(
  id, session, touchdown_utc, registration, registration_confidence, confirmed_by,
  longitudinal_m, lateral_m, uncertainty_m, frame_index, subframe_t,
  clip_path, overlay_path, calibration_id,
  status,        -- ok | needs_review | out_of_range | rejected
  range_bound    -- '< -19 m' / '> +19 m' when status = out_of_range
)
calibration(id, created_utc, camera, homography, markers, reproj_error_px, notes)
```

Export: one CSV per session for the club; the SQLite file is the archive.

---

## 6. Review UI

Minimal FastAPI app on localhost: a table of the session's landings, each row with
the clip, the overlay image, the OCR proposal and the measured distance. The judge
can correct the registration, step the touchdown frame ±1, and confirm. Confirming
renames the clip and marks the row `ok`. Every change is written with a timestamp,
so the automatic and the confirmed value both remain visible.

---

## 7. Milestones

| # | Deliverable | Done when |
|---|-------------|-----------|
| M0 | Recording rig + a full flying day of footage | 20–50 landings archived, camera geometry settled |
| M1 | Calibration tool | reprojection error < 0.1 m, verified against tape-measured points |
| M2 | Feature 1 | every landing of a test day is cut correctly, no missed landings |
| M3 | Feature 2 | measured vs. manually annotated: bias < 0.1 m, σ < 0.3 m |
| M4 | OCR + review UI | a session is reviewable in < 5 min |
| M5 | Season hardening | runs a whole day unattended, disk housekeeping, crash recovery |

Do M0 before writing any detection code. Every threshold in this document is a
guess until there is footage from *that* camera at *that* airfield.

---

## 8. Open questions

The site is settled: one P1485-LE, 75 m, mast above 8 m, ±19 m window. What
remains:

- Is there an OGN receiver in range of the field already, or does one need to be
  set up? This decides whether identification is automatic or one click per
  landing.
- What fraction of landings actually falls outside ±19 m? M0 answers this, and
  with it whether the option D upgrade is worth buying later.
- Power and weather protection at the strip; how long can it run unattended?
- Is a start list available in advance? It would make identification nearly free.
- Signage and an agreed retention period for the raw footage — people are filmed
  as well as aircraft. (The technical default is ~2 weeks raw, clips kept.)
