"""FastAPI app for controlling captures from a browser.

Bind to 0.0.0.0 and the page is reachable from a phone on the field WiFi,
which is the point: the recording machine is usually in a shed.

There is no authentication. Serve it on a trusted network only, or leave the
default host (127.0.0.1) alone — the source field accepts an RTSP URL with
credentials in it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from touchdown_analyzer import __version__
from touchdown_analyzer.control.service import CaptureService, ServiceError

STATIC = Path(__file__).parent / "static"


class StartRequest(BaseModel):
    source: str
    session: str
    segment_seconds: int = Field(default=10, ge=1, le=600)
    target_fps: float = Field(default=60.0, gt=0, le=1000)
    rtsp_transport: str = Field(default="tcp", pattern="^(tcp|udp)$")
    min_free_gb: float = Field(default=20.0, ge=0)
    duration_s: float = Field(default=0.0, ge=0)


class ProbeRequest(BaseModel):
    source: str
    seconds: float = Field(default=30.0, gt=0, le=600)
    target_fps: float = Field(default=60.0, gt=0, le=1000)
    rtsp_transport: str = Field(default="tcp", pattern="^(tcp|udp)$")


class FrameRequest(BaseModel):
    source: str
    rtsp_transport: str = Field(default="tcp", pattern="^(tcp|udp)$")


class MarkerModel(BaseModel):
    image_x: float
    image_y: float
    world_x: float
    world_y: float
    label: str = ""


class SolveRequest(BaseModel):
    markers: list[MarkerModel] = Field(min_length=1, max_length=64)
    image_size: tuple[int, int] | None = None
    source: str = ""
    notes: str = ""
    save: bool = False


class WindowRequest(BaseModel):
    session: str
    segment: str
    frame: int = Field(default=0, ge=0)


class MeasureRequest(BaseModel):
    image_x: float
    image_y: float


class AnnotationRequest(BaseModel):
    session: str
    segment: str
    frame: int = Field(ge=0)
    image_x: float
    image_y: float
    aircraft: str = ""
    note: str = ""


def create_app(service: CaptureService) -> FastAPI:
    app = FastAPI(title="touchdown-analyzer capture control", version=__version__)

    @app.exception_handler(ServiceError)
    async def _service_error(_: Any, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        payload = service.status()
        payload["version"] = __version__
        payload["probe"] = service.probe_status()
        return payload

    @app.post("/api/record/start")
    async def start(request: StartRequest) -> dict[str, Any]:
        try:
            service.start(
                request.source,
                request.session,
                segment_seconds=request.segment_seconds,
                target_fps=request.target_fps,
                rtsp_transport=request.rtsp_transport,
                min_free_gb=request.min_free_gb,
                duration_s=request.duration_s,
            )
        except ServiceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return service.status()

    @app.post("/api/record/stop")
    async def stop() -> dict[str, Any]:
        try:
            service.stop()
        except ServiceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return service.status()

    @app.post("/api/probe")
    async def probe(request: ProbeRequest) -> dict[str, Any]:
        try:
            job = service.start_probe(
                request.source,
                seconds=request.seconds,
                target_fps=request.target_fps,
                rtsp_transport=request.rtsp_transport,
            )
        except ServiceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return job.as_dict()

    @app.get("/calibration", include_in_schema=False)
    async def calibration_page() -> FileResponse:
        return FileResponse(STATIC / "calibrate.html")

    @app.post("/api/calibration/frame")
    async def calibration_frame(request: FrameRequest) -> dict[str, Any]:
        try:
            return service.grab_calibration_frame(
                request.source, rtsp_transport=request.rtsp_transport
            )
        except ServiceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/calibration/frame.jpg", include_in_schema=False)
    async def calibration_image() -> FileResponse:
        path = service.calibration_frame_path
        if not path.is_file():
            raise HTTPException(status_code=404, detail="no calibration frame grabbed yet")
        # Never cached: the same URL serves a new still after every grab.
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    @app.post("/api/calibration/solve")
    async def calibration_solve(request: SolveRequest) -> dict[str, Any]:
        try:
            return service.solve_calibration(
                [marker.model_dump() for marker in request.markers],
                image_size=request.image_size,
                source=request.source,
                notes=request.notes,
                save=request.save,
            )
        except ServiceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/calibration")
    async def calibration_current() -> dict[str, Any]:
        return {"calibration": service.load_calibration()}

    @app.get("/viewer", include_in_schema=False)
    async def viewer_page() -> FileResponse:
        return FileResponse(STATIC / "viewer.html")

    @app.get("/api/sessions/{session}/segments")
    async def session_segments(session: str) -> list[dict[str, Any]]:
        try:
            return service.segments_of(session)
        except ServiceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/viewer/window")
    async def viewer_window(request: WindowRequest) -> dict[str, Any]:
        try:
            return service.frame_window(request.session, request.segment, request.frame)
        except ServiceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/viewer/frame/{frame}.jpg", include_in_schema=False)
    async def viewer_frame(frame: int) -> FileResponse:
        try:
            path = service.frame_image(frame)
        except ServiceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    @app.post("/api/viewer/measure")
    async def viewer_measure(request: MeasureRequest) -> dict[str, Any]:
        return {"measurement": service.measure(request.image_x, request.image_y)}

    @app.post("/api/annotations")
    async def add_annotation(request: AnnotationRequest) -> dict[str, Any]:
        try:
            return service.annotate(
                request.session,
                request.segment,
                request.frame,
                image_x=request.image_x,
                image_y=request.image_y,
                aircraft=request.aircraft,
                note=request.note,
            )
        except ServiceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/annotations/{session}")
    async def list_annotations(session: str) -> list[dict[str, Any]]:
        return service.annotations(session)

    @app.get("/api/sessions")
    async def sessions() -> list[dict[str, Any]]:
        return service.sessions()

    @app.get("/api/sessions/{session}")
    async def session_report(session: str) -> dict[str, Any]:
        try:
            return service.session_report(session)
        except ServiceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app


def serve(
    root: Path,
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
) -> None:
    """Run the control UI until interrupted."""
    import uvicorn

    service = CaptureService(root, ffmpeg=ffmpeg, ffprobe=ffprobe)
    uvicorn.run(create_app(service), host=host, port=port, log_level="warning")
