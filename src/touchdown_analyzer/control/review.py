"""The judge's side of the control UI: analysis, results, confirmation.

Sits beside :class:`CaptureService` rather than inside it, because this is
the layer that needs OpenCV. If OpenCV is missing the recorder still
works; the landings page just says why analysis is unavailable.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from touchdown_analyzer.clips import cutter
from touchdown_analyzer.control.service import CaptureService, ServiceError
from touchdown_analyzer.identify import ogn
from touchdown_analyzer.store import landings as store_mod
from touchdown_analyzer.store.landings import Landing, LandingStore

log = logging.getLogger(__name__)

try:
    from touchdown_analyzer.analysis.worker import AnalysisWorker

    ANALYSIS_IMPORT_ERROR = ""
except ImportError as exc:  # OpenCV not installed
    AnalysisWorker = None  # type: ignore[assignment,misc]
    ANALYSIS_IMPORT_ERROR = str(exc)


class ReviewService:
    """Analysis worker, landing stores, OGN and the judge's edits."""

    def __init__(
        self,
        capture: CaptureService,
        *,
        out_root: Path | None = None,
        cut_clips: bool = True,
    ) -> None:
        self.capture = capture
        self.root = capture.root
        self.out_root = out_root or capture.root.parent / "landings"
        self.config_dir = capture.config_dir
        self.cut_clips = cut_clips
        self._worker: AnalysisWorker | None = None
        self._stores: dict[str, LandingStore] = {}
        self.field = ogn.load_field(self.config_dir)
        self._poller: ogn.Poller | None = None

    # -- stores -------------------------------------------------------------

    def store(self, session: str) -> LandingStore:
        if self._worker is not None:
            return self._worker.store_for(session)
        found = self._stores.get(session)
        if found is None:
            found = LandingStore(self.out_root / session)
            self._stores[session] = found
        return found

    def landing(self, session: str, landing_id: str) -> Landing:
        found = self.store(session).get(landing_id)
        if found is None:
            raise ServiceError(f"no landing {landing_id} in {session}")
        return found

    # -- analysis -----------------------------------------------------------

    @property
    def available(self) -> bool:
        return AnalysisWorker is not None

    def worker(self) -> AnalysisWorker:
        if AnalysisWorker is None:
            raise ServiceError(
                f'analysis needs OpenCV: pip install -e ".[analysis]"  ({ANALYSIS_IMPORT_ERROR})'
            )
        if self._worker is None:
            ffmpeg, ffprobe = self.capture.tools()
            self._worker = AnalysisWorker(
                self.root,
                self.out_root,
                self.capture.calibration_path,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                cut_clips=self.cut_clips,
                is_recording=self._is_recording,
                identify=self._identify,
            )
        return self._worker

    def _is_recording(self, session: str) -> bool:
        config = self.capture._config  # noqa: SLF001 - same package, read only
        return self.capture.is_recording and config is not None and config.session == session

    def start_analysis(self, session: str, *, follow: bool, fresh: bool = False) -> dict[str, Any]:
        worker = self.worker()
        try:
            worker.start(session, follow=follow, fresh=fresh)
        except RuntimeError as exc:
            raise ServiceError(str(exc)) from exc
        if follow:
            self.start_poller(session)
        return worker.status()

    def stop_analysis(self) -> dict[str, Any]:
        worker = self.worker()
        worker.stop()
        return worker.status()

    def analysis_status(self) -> dict[str, Any]:
        if self._worker is None:
            return {
                "available": self.available,
                "reason": ANALYSIS_IMPORT_ERROR,
                "running": False,
                "stage": "idle",
                "session": None,
                "ogn": self.ogn_status(),
            }
        payload = self._worker.status()
        payload["available"] = True
        payload["reason"] = ""
        payload["ogn"] = self.ogn_status()
        return payload

    # -- OGN ----------------------------------------------------------------

    def ogn_status(self) -> dict[str, Any]:
        return {
            "field": self.field.as_dict(),
            "poller": self._poller.status() if self._poller else None,
        }

    def save_field(self, payload: dict[str, Any]) -> dict[str, Any]:
        known = set(ogn.Field.__dataclass_fields__)
        try:
            self.field = ogn.Field(**{k: v for k, v in payload.items() if k in known})
        except TypeError as exc:
            raise ServiceError(f"bad field settings: {exc}") from exc
        ogn.save_field(self.config_dir, self.field)
        return self.ogn_status()

    def start_poller(self, session: str) -> None:
        if not self.field.enabled or not self.field.lat:
            return
        if self._poller is not None and self._poller.running:
            if self._poller.path.parent.name == session:
                return
            self._poller.stop()
        self._poller = ogn.Poller(self.field, self.root / session)
        self._poller.start()

    def stop_poller(self) -> None:
        if self._poller is not None:
            self._poller.stop()

    def _identify(self, landing: Landing) -> None:
        """Fill in the aircraft from OGN, if anything matches."""
        if not self.field.enabled or not landing.touchdown_utc:
            return
        found = ogn.identify(landing.touchdown_utc, self.root / landing.session, self.field)
        if found is None:
            return
        landing.ogn = found.as_dict()
        if not landing.registration and found.registration:
            landing.registration = found.registration
            landing.competition_number = found.competition_number
            landing.aircraft_type = found.aircraft_type
            landing.identified_by = "ogn"
        # The logbook knows whether that minute was a landing or a take-off,
        # which is exactly what the estimator cannot see without a shadow.
        note = f"OGN logbook: {found.registration or found.flarm_id} {found.event} at this time"
        if found.event == "takeoff" and landing.kind != "departure":
            if landing.status == store_mod.PENDING and landing.outcome != store_mod.MEASURED:
                landing.outcome = store_mod.DEPARTURE
                landing.kind = "departure"
                note += " - listed as a take-off"
        elif found.event == "landing" and landing.kind == "departure":
            note += " - check, the estimator saw a take-off"
        else:
            return
        if note not in landing.flags:
            landing.flags.append(note)

    def fetch_logbook(self, session: str) -> dict[str, Any]:
        """Pull the day's KTrax logbook and re-identify unconfirmed landings."""
        if not self.field.airfield:
            raise ServiceError("no airfield set in config/ogn.json")
        try:
            day = date.fromisoformat(session[:10])
        except ValueError as exc:
            raise ServiceError(f"session name {session!r} does not start with a date") from exc
        session_dir = self.root / session
        note = ""
        try:
            sorties = ogn.fetch_logbook(self.field, day)
            ogn.save_logbook(session_dir, sorties)
        except (OSError, ValueError) as exc:
            # No internet at the field is normal; a logbook fetched earlier
            # today is still the right answer for the landings it covers.
            sorties = ogn.load_logbook(session_dir)
            if not sorties:
                raise ServiceError(
                    f"OGN FlightBook unreachable ({exc}) - check the internet connection; "
                    "nothing fetched earlier for this session"
                ) from exc
            note = f"FlightBook unreachable ({exc}); used the logbook fetched earlier"
        store = self.store(session)
        matched = 0
        for landing in store.all():
            if landing.status == store_mod.CONFIRMED or landing.identified_by == "judge":
                continue
            before = landing.registration
            self._identify(landing)
            if landing.registration != before or landing.ogn:
                store.update(landing, "identified", by=landing.identified_by)
                matched += landing.registration != before
        return {
            "sorties": len(sorties),
            "matched": matched,
            "landings": len(store.all()),
            "note": note,
        }

    # -- the judge ------------------------------------------------------------

    def confirm(
        self,
        session: str,
        landing_id: str,
        *,
        registration: str = "",
        note: str = "",
    ) -> Landing:
        store = self.store(session)
        landing = self.landing(session, landing_id)
        registration = registration.strip().upper()
        detail: dict[str, Any] = {}
        if registration and registration != landing.registration:
            detail["registration_before"] = landing.registration
            landing.registration = registration
            landing.identified_by = "judge"
        if note:
            landing.note = note
        landing.status = store_mod.CONFIRMED
        landing.confirmed_utc = store_mod.now_utc()
        self._rename_clip(landing)
        return store.update(landing, "confirmed", **detail)

    def reject(self, session: str, landing_id: str, *, note: str = "") -> Landing:
        store = self.store(session)
        landing = self.landing(session, landing_id)
        landing.status = store_mod.REJECTED
        if note:
            landing.note = note
        return store.update(landing, "rejected")

    def reopen(self, session: str, landing_id: str) -> Landing:
        store = self.store(session)
        landing = self.landing(session, landing_id)
        landing.status = store_mod.PENDING
        landing.confirmed_utc = None
        return store.update(landing, "reopened")

    def edit(
        self,
        session: str,
        landing_id: str,
        *,
        registration: str | None = None,
        competition_number: str | None = None,
        aircraft_type: str | None = None,
        outcome: str | None = None,
        frame: int | None = None,
        reset_frame: bool = False,
        note: str | None = None,
    ) -> Landing:
        """The judge's corrections: aircraft, what happened, and which frame."""
        store = self.store(session)
        landing = self.landing(session, landing_id)
        detail: dict[str, Any] = {}
        if registration is not None:
            detail["registration_before"] = landing.registration
            landing.registration = registration.strip().upper()
            landing.identified_by = "judge" if landing.registration else ""
        if competition_number is not None:
            landing.competition_number = competition_number.strip().upper()
        if aircraft_type is not None:
            landing.aircraft_type = aircraft_type.strip()
        if note is not None:
            landing.note = note
        if outcome is not None:
            if outcome not in {
                store_mod.MEASURED,
                store_mod.SHORT,
                store_mod.LONG,
                store_mod.ON_GROUND,
                store_mod.DEPARTURE,
                store_mod.AIRBORNE,
                store_mod.UNSEEN,
            }:
                raise ServiceError(f"unknown outcome {outcome!r}")
            detail["outcome_before"] = landing.outcome
            landing.outcome = outcome
            landing.kind = {
                store_mod.MEASURED: "landing",
                store_mod.SHORT: "landing",
                store_mod.LONG: "landing",
                store_mod.UNSEEN: "landing",
                store_mod.DEPARTURE: "departure",
            }.get(outcome, "pass")
            if outcome in (store_mod.SHORT, store_mod.LONG) and landing.track:
                edge = landing.track[0] if outcome == store_mod.SHORT else landing.track[-1]
                landing.bound_m = landing.direction * edge.world_x
        if frame is not None:
            point = self._track_point(landing, frame)
            detail["frame_before"] = landing.confirmed_frame or landing.frame
            landing.confirmed_frame = point.frame
            landing.confirmed_longitudinal_m = landing.direction * point.world_x
            landing.image_x, landing.image_y = point.u, point.v
            if landing.outcome != store_mod.MEASURED:
                landing.outcome = store_mod.MEASURED
                landing.kind = "landing"
            when = self._frame_utc(landing, point)
            if when:
                landing.touchdown_utc = when
        if reset_frame and landing.confirmed_frame is not None and landing.frame is not None:
            # Back to the automatic touchpoint: the anchor frame, its pixel,
            # and the sub-frame instant the estimator found.
            detail["frame_before"] = landing.confirmed_frame
            landing.confirmed_frame = None
            landing.confirmed_longitudinal_m = None
            point = self._track_point(landing, landing.frame)
            landing.image_x, landing.image_y = point.u, point.v
            when = self._frame_utc(landing, point)
            if when:
                shift = ((landing.subframe or point.frame) - point.frame) / (landing.fps or 60.0)
                landing.touchdown_utc = (
                    datetime.fromisoformat(when) + timedelta(seconds=shift)
                ).isoformat()
        if frame is not None or reset_frame:
            # The proof image has to show the frame that is scored.
            self._render_overlay(landing)
        if landing.status == store_mod.CONFIRMED:
            self._rename_clip(landing)
        return store.update(landing, "edited", **detail)

    def _render_overlay(self, landing: Landing) -> None:
        """Redraw the overlay for the frame the landing is now scored at."""
        if not self.available or landing.frame is None or not landing.overlay_path:
            return
        from touchdown_analyzer.analysis import overlay as overlay_mod
        from touchdown_analyzer.analysis.pipeline import WINDOW_HALF_M
        from touchdown_analyzer.calibration import homography as hg

        frame_no = landing.confirmed_frame if landing.confirmed_frame is not None else landing.frame
        segment = landing.segment or landing.track[0].segment
        try:
            calibration = hg.load(self.capture.calibration_path)
            image = overlay_mod.read_frame(self.root / landing.session / segment, frame_no)
        except (OSError, ValueError, TypeError):
            return
        if image is None:
            return
        anchor = self._track_point(landing, frame_no)
        whole = [p for p in landing.track if not p.clipped]
        scored = landing.scored_longitudinal_m
        judged = landing.confirmed_frame is not None
        unc = "" if judged or not landing.uncertainty_m else f"  +/- {landing.uncertainty_m:.2f} m"
        local = (
            datetime.fromisoformat(landing.touchdown_utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            if landing.touchdown_utc
            else ""
        )
        rendered = overlay_mod.render(
            image,
            inverse=calibration.inverse,
            contact=(anchor.u, anchor.v) if scored is not None else None,
            approach=[(p.u, p.v) for p in whole if p.t <= anchor.t],
            ground=[(p.u, p.v) for p in whole if p.t > anchor.t],
            headline=f"{landing.label()}{unc}",
            caption=(
                f"{landing.id}  {local}  {landing.registration or 'unknown'}  {landing.outcome}"
                f"  ({'frame picked by the judge' if judged else landing.method})"
            ),
            window_m=WINDOW_HALF_M,
        )
        try:
            overlay_mod.save(rendered, Path(landing.overlay_path))
        except OSError as exc:
            log.warning("could not rewrite overlay for %s: %s", landing.id, exc)

    @staticmethod
    def _track_point(landing: Landing, frame: int) -> store_mod.TrackPoint:
        if not landing.track:
            raise ServiceError("this landing has no track to pick a frame from")
        segment = landing.segment or landing.track[0].segment
        same = [p for p in landing.track if p.segment == segment]
        return min(same or landing.track, key=lambda p: abs(p.frame - frame))

    def _frame_utc(self, landing: Landing, point: store_mod.TrackPoint) -> str | None:
        try:
            meta = next(
                s for s in self.capture.segments_of(landing.session) if s["name"] == point.segment
            )
        except (ServiceError, StopIteration):
            return None
        start = datetime.fromisoformat(meta["start_utc"])
        fps = meta["fps"] or landing.fps
        return (start + timedelta(seconds=point.frame / fps)).isoformat()

    def _rename_clip(self, landing: Landing) -> None:
        """``..._UNKNOWN-007.mp4`` becomes ``..._HB-3213.mp4`` on confirmation."""
        if not landing.clip_path or not landing.registration or not landing.touchdown_utc:
            return
        old = Path(landing.clip_path)
        if not old.is_file():
            return
        seq = int(landing.id.lstrip("L") or 0)
        local = datetime.fromisoformat(landing.touchdown_utc).astimezone()
        new = old.with_name(cutter.clip_name(local, landing.registration, seq))
        if new == old:
            return
        try:
            old.rename(new)
        except OSError as exc:
            log.warning("could not rename %s: %s", old, exc)
            return
        landing.history.append({"utc": store_mod.now_utc(), "action": "renamed", "from": str(old)})
        landing.clip_path = str(new)
        if landing.overlay_path:
            old_overlay = Path(landing.overlay_path)
            new_overlay = new.with_name(new.stem + "_overlay.jpg")
            try:
                if old_overlay.is_file():
                    old_overlay.rename(new_overlay)
                    landing.overlay_path = str(new_overlay)
            except OSError:
                pass

    # -- artefacts ----------------------------------------------------------

    def artefact(self, session: str, landing_id: str, kind: str) -> Path:
        landing = self.landing(session, landing_id)
        path = landing.overlay_path if kind == "overlay" else landing.clip_path
        if not path or not Path(path).is_file():
            raise ServiceError(f"no {kind} for {landing_id}")
        return Path(path)

    # -- listing ------------------------------------------------------------

    def sessions_with_results(self) -> list[str]:
        if not self.out_root.is_dir():
            return []
        return sorted(
            (p.name for p in self.out_root.iterdir() if (p / store_mod.STORE_NAME).is_file()),
            reverse=True,
        )

    def summary(self, session: str) -> dict[str, Any]:
        items = self.store(session).all()
        return {
            "session": session,
            "count": len(items),
            "pending": sum(x.status == store_mod.PENDING and x.kind == "landing" for x in items),
            "confirmed": sum(x.status == store_mod.CONFIRMED for x in items),
            "rejected": sum(x.status == store_mod.REJECTED for x in items),
            "landings": [x.to_dict() for x in items],
        }
