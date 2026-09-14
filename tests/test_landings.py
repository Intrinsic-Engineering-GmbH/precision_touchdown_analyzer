"""The landing store, clip naming, OGN parsing / matching and the review API."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from touchdown_analyzer.capture import ffmpeg as ff
from touchdown_analyzer.clips import cutter
from touchdown_analyzer.control.app import create_app
from touchdown_analyzer.control.review import ReviewService
from touchdown_analyzer.control.service import CaptureService
from touchdown_analyzer.identify import ogn
from touchdown_analyzer.store import landings as store_mod
from touchdown_analyzer.store.landings import Landing, LandingStore, TrackPoint

TD = "2026-09-13T11:58:37.500000+00:00"


def landing(**overrides) -> Landing:
    track = [
        TrackPoint(
            segment="s.mp4",
            frame=80 + i,
            t=i / 60,
            u=100 + 25 * i,
            v=730,
            world_x=-10 + 0.4 * i,
            world_y=3.0,
            clipped=False,
            reach_px=40 - i,
        )
        for i in range(40)
    ]
    base = dict(
        id="L0001",
        session="2026-09-13",
        created_utc=store_mod.now_utc(),
        kind="landing",
        outcome=store_mod.MEASURED,
        touchdown_utc=TD,
        segment="s.mp4",
        frame=100,
        subframe=100.3,
        direction=1,
        longitudinal_m=-1.9,
        lateral_m=3.0,
        uncertainty_m=0.4,
        speed_mps=24.0,
        image_x=600.0,
        image_y=730.0,
        track=track,
    )
    base.update(overrides)
    return Landing(**base)


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------


def test_store_round_trips_and_labels(tmp_path: Path) -> None:
    store = LandingStore(tmp_path)
    store.add(landing())
    store.add(landing(id="L0002", outcome=store_mod.SHORT, bound_m=-17.4, longitudinal_m=None))
    store.add(landing(id="L0003", outcome=store_mod.LONG, bound_m=18.2, longitudinal_m=None))

    again = LandingStore(tmp_path)
    ids = [x.id for x in again.all()]
    assert ids == ["L0001", "L0002", "L0003"]
    assert again.get("L0001").label() == "-1.9 m"
    assert again.get("L0002").label() == "< -17 m"
    assert again.get("L0003").label() == "> +18 m"
    assert again.next_id() == "L0004"
    assert again.has_track("s.mp4", 81, 200)
    assert not again.has_track("other.mp4", 81, 200)


def test_store_update_keeps_history_and_clear_removes_artefacts(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    store = LandingStore(tmp_path)
    x = store.add(landing(clip_path=str(clip)))
    x.registration = "HB-3213"
    store.update(x, "edited", registration_before="")
    assert json.loads(store.path.read_text())["landings"][0]["history"][0]["action"] == "edited"
    store.clear()
    assert not clip.exists()
    assert store.all() == []


def test_store_picks_up_a_rewrite_by_another_process(tmp_path: Path) -> None:
    mine = LandingStore(tmp_path)
    mine.add(landing())
    other = LandingStore(tmp_path)  # a second process holding the same file
    other.add(landing(id="L0002", outcome=store_mod.LONG, bound_m=18.0, longitudinal_m=None))
    # the first store sees the newcomer instead of overwriting it
    assert [x.id for x in mine.all()] == ["L0001", "L0002"]
    assert mine.next_id() == "L0003"


def test_confirmed_value_wins_over_measured() -> None:
    x = landing(confirmed_longitudinal_m=-3.2)
    assert x.scored_longitudinal_m == -3.2
    assert x.label() == "-3.2 m"


# --------------------------------------------------------------------------
# clips
# --------------------------------------------------------------------------


def test_clip_name_uses_touchdown_time_and_registration() -> None:
    when = datetime(2026, 7, 18, 14, 32, 7)
    assert cutter.clip_name(when, "", 7) == "2026-07-18_14-32-07_UNKNOWN-007.mp4"
    assert cutter.clip_name(when, "hb-3213", 7) == "2026-07-18_14-32-07_HB-3213.mp4"
    assert cutter.clip_name(when, "../x", 7) == "2026-07-18_14-32-07_..X.mp4".replace("..X", "..X")


def test_clip_window_is_pre_and_post_roll() -> None:
    when = datetime(2026, 7, 18, 14, 32, 7, tzinfo=UTC)
    start, end = cutter.window(when)
    assert when - start == timedelta(seconds=cutter.PRE_ROLL_S)
    assert end - when == timedelta(seconds=cutter.POST_ROLL_S)


def test_cut_refuses_when_no_segment_covers_the_window(tmp_path: Path) -> None:
    when = datetime(2026, 7, 18, 14, 32, 7, tzinfo=UTC)
    piece = cutter.Piece(
        tmp_path / "a.mp4", when + timedelta(minutes=5), when + timedelta(minutes=6)
    )
    with pytest.raises(cutter.ClipError):
        cutter.cut([piece], when, when + timedelta(seconds=8), "ffmpeg", tmp_path / "out.mp4")


# --------------------------------------------------------------------------
# OGN
# --------------------------------------------------------------------------

LIVE_XML = """<?xml version="1.0"?><markers>
<m a="46.977000,7.127500,DKU,HB-3380,450,11:58:30,7,90,95,-1.2,1,LSTB1,0,DD1234"/>
<m a="46.951351,7.430420,_16,11f4f816,1567,11:52:22,33,0,0,0.0,7,NAVITER3,0,11f4f816"/>
</markers>"""


def test_live_feed_parses_and_matches_the_low_close_aircraft() -> None:
    now = datetime.fromisoformat(TD) + timedelta(seconds=7)
    fixes = ogn.parse_live(LIVE_XML, now)
    assert len(fixes) == 2
    assert fixes[0].registration == "HB-3380" and fixes[0].competition_number == "DKU"
    assert fixes[0].utc.startswith("2026-09-13T11:58:37")

    field = ogn.Field(airfield="LSTB", lat=46.9769, lon=7.1269, elevation_m=434, enabled=True)
    match = ogn.match_fixes(datetime.fromisoformat(TD), fixes, field)
    assert match is not None
    assert match.registration == "HB-3380"
    assert match.candidates == 1  # the high, far one is not a candidate
    assert match.probability > 0.8


def test_logbook_parses_masked_and_real_times() -> None:
    payload = {
        "sorties": [
            {
                "cs": "HB-3213",
                "cn": "F7",
                "actype": "LS4",
                "date": "2026-09-13",
                "tkof": {"time": "12:10", "loc": "LSTB"},
                "ldg": {"time": "13:58", "loc": "LSTB"},
            },
            {
                "cs": "HBXXXX",
                "cn": "-",
                "actype": "?",
                "date": "2026-09-13",
                "tkof": {"time": "10:XX", "loc": "LSTB"},
                "ldg": {"time": "11:XX", "loc": "LSTB"},
            },
        ]
    }
    sorties = ogn.parse_logbook(payload, 2.0)
    assert sorties[0].landing_utc == "2026-09-13T11:58:00+00:00"
    assert sorties[1].landing_utc is None
    field = ogn.Field(airfield="LSTB")
    match = ogn.match_logbook(datetime.fromisoformat(TD), sorties, field)
    assert match is not None and match.registration == "HB-3213" and match.source == "logbook"


FLIGHTBOOK = {
    "code": "LSTB",
    "airfield": {"code": "LSTB"},
    "devices": [
        {
            "address": "4B5134",
            "aircraft": "LS-8 18",
            "competition": "F7",
            "registration": "HB-3213",
        },
        {"address": "4B26C8", "aircraft": "PA-18", "competition": "RW", "registration": "HB-ORW"},
    ],
    "flights": [
        # HB-3213 lands 11:58 UTC (the epoch of TD rounded down to the minute)
        {"device": 0, "start_tsp": 1789300200, "stop_tsp": 1789300680, "towing": False},
        # the tug takes off at 11:57 and is still up
        {"device": 1, "start_tsp": 1789300620, "stop_tsp": None, "towing": True},
    ],
}


def test_flightbook_parses_devices_and_flights() -> None:
    sorties = ogn.parse_flightbook(FLIGHTBOOK)
    assert [s.registration for s in sorties] == ["HB-3213", "HB-ORW"]
    assert sorties[0].landing_utc == "2026-09-13T11:58:00+00:00"
    assert sorties[1].landing_utc is None and sorties[1].aircraft_type == "PA-18 (tug)"
    assert sorties[0].raw["flarm_id"] == "4B5134"


def test_logbook_matches_a_landing_or_a_takeoff_and_says_which() -> None:
    sorties = ogn.parse_flightbook(FLIGHTBOOK)
    field = ogn.Field(airfield="LSTB")
    landing = ogn.match_logbook(datetime.fromisoformat(TD), sorties, field)
    assert landing is not None and landing.registration == "HB-3213" and landing.event == "landing"
    takeoff = ogn.match_logbook(datetime.fromisoformat("2026-09-13T11:57:10+00:00"), sorties, field)
    assert takeoff is not None and takeoff.registration == "HB-ORW" and takeoff.event == "takeoff"
    # both events within a minute of each other: less sure
    assert takeoff.probability < landing.probability or takeoff.candidates > 1


def test_field_config_round_trip(tmp_path: Path) -> None:
    field = ogn.Field(airfield="LSTB", lat=46.9769, lon=7.1269, enabled=True)
    ogn.save_field(tmp_path, field)
    assert ogn.load_field(tmp_path) == field
    assert ogn.load_field(tmp_path / "missing") == ogn.Field()


# --------------------------------------------------------------------------
# review API
# --------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(ff, "find_tool", lambda name, override=None: name)
    capture = CaptureService(tmp_path / "raw", config_dir=tmp_path / "config")
    review = ReviewService(capture, out_root=tmp_path / "landings")
    store = review.store("2026-09-13")
    clip = tmp_path / "landings" / "2026-09-13" / "2026-09-13_13-58-37_UNKNOWN-001.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"mp4")
    store.add(landing(clip_path=str(clip)))
    store.add(landing(id="L0002", kind="pass", outcome=store_mod.ON_GROUND, longitudinal_m=None))
    return TestClient(create_app(capture, review))


def test_listing_counts_only_landings_as_pending(client: TestClient) -> None:
    assert client.get("/api/landings").json() == ["2026-09-13"]
    summary = client.get("/api/landings/2026-09-13").json()
    assert summary["count"] == 2 and summary["pending"] == 1
    assert summary["landings"][0]["label"] == "-1.9 m"


def test_confirm_renames_the_clip_and_records_the_judge(client: TestClient) -> None:
    r = client.post("/api/landings/2026-09-13/L0001/confirm", json={"registration": "hb-3213"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "confirmed"
    assert body["registration"] == "HB-3213" and body["identified_by"] == "judge"
    assert body["clip_path"].endswith("2026-09-13_13-58-37_HB-3213.mp4")
    assert Path(body["clip_path"]).is_file()
    assert [h["action"] for h in body["history"]] == ["renamed", "confirmed"]


def test_judge_can_move_the_contact_frame(client: TestClient) -> None:
    r = client.post("/api/landings/2026-09-13/L0001/edit", json={"frame": 90})
    body = r.json()
    assert body["confirmed_frame"] == 90
    # track point 90 is index 10: world_x = -10 + 0.4 * 10
    assert body["confirmed_longitudinal_m"] == pytest.approx(-6.0)
    assert body["label"] == "-6.0 m"


def test_judge_can_go_back_to_the_automatic_touchpoint(client: TestClient) -> None:
    client.post("/api/landings/2026-09-13/L0001/edit", json={"frame": 90})
    body = client.post("/api/landings/2026-09-13/L0001/edit", json={"reset_frame": True}).json()
    assert body["confirmed_frame"] is None and body["confirmed_longitudinal_m"] is None
    assert body["label"] == "-1.9 m"
    # back on the automatic anchor pixel (track index 20 = frame 100)
    assert body["image_x"] == pytest.approx(100 + 25 * 20)


def test_outcome_can_be_corrected_to_a_bound(client: TestClient) -> None:
    body = client.post("/api/landings/2026-09-13/L0001/edit", json={"outcome": "long"}).json()
    assert body["outcome"] == "long" and body["label"].startswith("> ")
    assert body["bound_m"] == pytest.approx(-10 + 0.4 * 39)


def test_reject_and_reopen(client: TestClient) -> None:
    assert (
        client.post("/api/landings/2026-09-13/L0001/reject", json={"note": "bird"}).json()["status"]
        == "rejected"
    )
    assert client.post("/api/landings/2026-09-13/L0001/reopen").json()["status"] == "pending"
    assert client.get("/api/landings/2026-09-13/L0009").status_code == 409


def test_analysis_refuses_without_calibration(client: TestClient) -> None:
    r = client.post("/api/analysis/start", json={"session": "2026-09-13"})
    assert r.status_code == 409
    assert "calibrat" in r.json()["detail"] or "session" in r.json()["detail"]
    status = client.get("/api/analysis/status").json()
    assert status["running"] is False
