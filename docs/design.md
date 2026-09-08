# Design and implementation plan

Scope: measure the displacement between the first main-wheel touchdown of a
glider and the target line of a spot-landing competition, from a continuously
recording fixed camera, and store each landing as its own clip.

Assumptions from the current decisions:

- **One fixed camera, side-on** to the strip.
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

## 2. Feature 1 — landing segmentation and clips

### 2.1 Detection

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

### 2.2 Landing state machine

Per track:

| State | Enter when | Notes |
|-------|------------|-------|
| `APPROACH` | track appears in the approach ROI, descending | starts the clip pre-roll |
| `TOUCHDOWN` | contact detected (§3) | the measured event |
| `ROLLOUT` | vertical motion stopped, still moving along the strip | best OCR window |
| `CLEARED` | track stops or leaves the strip ROI for > 3 s | ends the clip |

Guards: minimum track lifetime, minimum traversed distance, and a "two aircraft in
frame" rule that keeps tracks separate rather than merging blobs.

### 2.3 Clip cutting

The event has a start and end wall-clock time; `segments.py` maps that to the
covering raw segments. Cut with ffmpeg by concatenating the covering segments and
trimming with `-ss/-to`. Prefer `-c copy` (fast, lossless); if the keyframe
spacing makes the cut too coarse, re-encode just that clip — a 30 s clip re-encodes
in seconds. Padding: 5 s before `APPROACH`, 5 s after `CLEARED`.

Naming: `YYYY-MM-DD_HH-MM-SS_<REG>.mp4`, timestamp = touchdown time (not file
creation), `<REG>` = confirmed registration. Until a human confirms, the file is
named `..._UNKNOWN-<seq>.mp4` and renamed on confirmation — the DB keeps both
names so nothing is lost.

### 2.4 Registration OCR

Read during `ROLLOUT`, where the glider is largest and slowest:

1. Crop the tracked box, upscale ×2–4, deskew, CLAHE contrast.
2. Run OCR on every Nth frame across the whole rollout (typically 30–80 attempts).
3. Filter candidates by regex for the expected formats (`HB-\d{3,4}`, `D-\d{4}`,
   `[A-Z]{1,2}` competition numbers) and **vote across frames** — the same string
   read 20 times from different angles is far more reliable than any single read.
4. Emit the top candidate + confidence. Below threshold → `UNKNOWN`.

Reality check: from 40 m side-on with 1080p, a registration on the fin is roughly
20–40 px tall — marginal but workable after upscaling; **competition numbers are
much larger and much easier**, so prefer them when the competition uses them. If
OCR turns out to be the weak link, importing the start list and matching by
landing order collapses the problem to picking from ~40 known strings.

---

## 3. Feature 2 — touchdown displacement

### 3.1 Calibration (once per camera position)

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

### 3.2 Detecting the contact instant

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

### 3.3 From contact point to metres

```python
p_img   = undistort(contact_point_px)
p_world = perspective_transform(p_img, H)      # (x, y) in metres
longitudinal = p_world.x                       # signed distance to target line
lateral      = p_world.y                       # offset from center line
```

Store both, plus the frame index, the sub-frame time, the cue agreement and an
uncertainty estimate. Render an **overlay PNG** (contact frame + projected target
line + measured distance) — the artefact that makes the number believable to a
pilot.

### 3.4 Error budget

| Source | Typical | Mitigation |
|--------|---------|------------|
| Timing (frame rate) | 0.8 m @30 fps, 0.2 m @120 fps | high fps + sub-frame line fit → ~0.1 m |
| Contact point extraction | 2–5 px ≈ 0.05–0.1 m | high contrast, tuned ROI |
| Homography / marker survey | 0.05–0.15 m | more markers, tape-measured, check residual |
| Lens distortion (uncorrected) | up to 1 m at frame edges | chessboard calibration |
| Camera drift | unbounded | ORB drift check, refuse when large |

Timing dominates. If accuracy ever falls short, the first lever is frame rate,
not resolution.

### 3.5 Validation

- Manually annotate the contact frame of ~30 landings from M0; compare bias and σ.
- Lay markers every 5 m along the strip edge, photograph, verify the mapping
  measures the known spacing.
- Roll a wheel to a tape-measured point and "land" it — a static ground truth for
  the geometry alone, independent of the timing question.

---

## 4. Data model

```sql
landing(
  id, session, touchdown_utc, registration, registration_confidence, confirmed_by,
  longitudinal_m, lateral_m, uncertainty_m, frame_index, subframe_t,
  clip_path, overlay_path, calibration_id, status  -- ok | needs_review | rejected
)
calibration(id, created_utc, camera, homography, markers, reproj_error_px, notes)
```

Export: one CSV per session for the club; the SQLite file is the archive.

---

## 5. Review UI

Minimal FastAPI app on localhost: a table of the session's landings, each row with
the clip, the overlay image, the OCR proposal and the measured distance. The judge
can correct the registration, step the touchdown frame ±1, and confirm. Confirming
renames the clip and marks the row `ok`. Every change is written with a timestamp,
so the automatic and the confirmed value both remain visible.

---

## 6. Milestones

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

## 7. Open questions

- Camera hardware: fps and shutter type decide the achievable accuracy.
- Power and weather protection at the strip; how long can it run unattended?
- Does the competition use competition numbers (easy OCR) or only registrations?
- Is a start list available in advance? It would make identification nearly free.
- Landing direction changes with the wind — one calibration per direction, or a
  camera position that covers both?
- Retention policy for raw footage (people are filmed too).
