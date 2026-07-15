import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from browser_harness import recorder


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_AGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("BU_NAME", "testrec")
    monkeypatch.setenv("BH_RECORD", "0")  # opt out of auto-record; tests drive it explicitly
    monkeypatch.setattr(recorder, "_SETTLE_SECONDS", 0)
    return tmp_path


def _ctx(**extra):
    return {"url": "https://example.com", "title": "Example", "w": 1280, "h": 800,
            "sx": 0, "sy": 0, "dpr": 2, **extra}


def _events(rec_dir):
    lines = (Path(rec_dir) / "events.jsonl").read_text().splitlines()
    return [json.loads(l) for l in lines]


def _record(fake_png, ctx=None):
    return (
        patch("browser_harness.helpers.js", return_value=ctx or _ctx()),
        patch("browser_harness.helpers.cdp", return_value={"data": fake_png(64, 32)}),
    )


def test_full_recording_cycle(workspace, fake_png):
    js_p, cdp_p = _record(fake_png)
    with js_p, cdp_p:
        rec = recorder.start_recording("session", title="My run")
        recorder.observe("click_at_xy", (640, 412), {}, 0.1)
        recorder.observe("wait_for_load", (), {}, 1.2)
        stopped = recorder.stop_recording()

    assert stopped == rec
    assert recorder.recording_dir() is None
    meta = json.loads((Path(rec) / "meta.json").read_text())
    assert meta["title"] == "My run"
    events = _events(rec)
    helpers_seen = [e["helper"] for e in events]
    assert helpers_seen == ["start_recording", "click_at_xy", "wait_for_load", "stop_recording"]
    click = events[1]
    assert (click["x"], click["y"]) == (640, 412)
    assert click["url"] == "https://example.com"
    assert click["frame"] == "0002.jpg"
    frames = sorted(p.name for p in Path(rec).glob("*.jpg"))
    assert frames == ["0001.jpg", "0002.jpg", "0003.jpg", "0004.jpg"]


def test_observe_ignores_readonly_helpers(workspace, fake_png):
    js_p, cdp_p = _record(fake_png)
    with js_p, cdp_p:
        rec = recorder.start_recording("session")
        recorder.observe("page_info", (), {})
        recorder.observe("capture_screenshot", (), {})
    assert [e["helper"] for e in _events(rec)] == ["start_recording"]


def test_observe_is_noop_by_default(workspace, monkeypatch):
    monkeypatch.delenv("BH_RECORD", raising=False)
    recorder.observe("click_at_xy", (1, 2), {})
    assert not (workspace / "recordings").exists() or not list((workspace / "recordings").glob("*/events.jsonl"))


def test_observe_auto_records_when_bh_record_set(workspace, monkeypatch, fake_png):
    monkeypatch.setenv("BH_RECORD", "1")
    js_p, cdp_p = _record(fake_png)
    with js_p, cdp_p:
        recorder.observe("click_at_xy", (640, 412), {}, 0.1)
        recorder.observe("goto_url", ("https://a.example",), {})
    recs = list((workspace / "recordings").glob("session-*"))
    assert len(recs) == 1
    events = _events(recs[0])
    assert [e["helper"] for e in events] == ["click_at_xy", "goto_url"]
    assert json.loads((recs[0] / "meta.json").read_text())["auto"] is True


def test_auto_record_accumulates_across_calls(workspace, monkeypatch, fake_png):
    monkeypatch.setenv("BH_RECORD", "1")
    js_p, cdp_p = _record(fake_png)
    with js_p, cdp_p:
        recorder.observe("click_at_xy", (1, 2), {})
        first = recorder.recording_dir()
        recorder.observe("click_at_xy", (3, 4), {})
    # same recording reused, not a second dir
    assert len(list((workspace / "recordings").glob("session-*"))) == 1
    assert len(_events(first)) == 2


def test_auto_record_rolls_over_after_idle_gap(workspace, monkeypatch, fake_png):
    monkeypatch.setenv("BH_RECORD", "1")
    monkeypatch.setenv("BH_RECORD_IDLE", "0")  # any elapsed time counts as stale
    js_p, cdp_p = _record(fake_png)
    with js_p, cdp_p:
        recorder.observe("click_at_xy", (1, 2), {})
        first = recorder.recording_dir()
        # backdate the frame so the next observe sees the recording as idle
        for f in Path(first).glob("*.jpg"):
            os.utime(f, (0, 0))
        recorder.observe("click_at_xy", (3, 4), {})
        second = recorder.recording_dir()
    assert second != first
    assert len(list((workspace / "recordings").glob("session-*"))) == 2


def test_explicit_recording_never_rolls_over(workspace, monkeypatch, fake_png):
    monkeypatch.setenv("BH_RECORD", "1")
    monkeypatch.setenv("BH_RECORD_IDLE", "0")
    js_p, cdp_p = _record(fake_png)
    with js_p, cdp_p:
        rec = recorder.start_recording("keeper")
        for f in Path(rec).glob("*.jpg"):
            os.utime(f, (0, 0))
        recorder.observe("click_at_xy", (3, 4), {})
    # explicit recording is not auto, so it stays put — no session-* sibling
    assert recorder.recording_dir() == rec
    assert not list((workspace / "recordings").glob("session-*"))


def test_typing_records_focused_element_box_and_masks_passwords(workspace, fake_png):
    ctx = _ctx(box={"x": 100, "y": 200, "w": 300, "h": 40}, input="password")
    js_p, cdp_p = _record(fake_png, ctx=ctx)
    with js_p, cdp_p:
        rec = recorder.start_recording("session")
        recorder.observe("fill_input", ("#pw", "hunter2"), {})
    event = _events(rec)[1]
    assert event["box"] == {"x": 100, "y": 200, "w": 300, "h": 40}
    assert event["text"] == "•" * 7
    assert "hunter2" not in json.dumps(event)


def test_capture_failures_still_append_event(workspace, fake_png):
    js_p, cdp_p = _record(fake_png)
    with js_p, cdp_p:
        rec = recorder.start_recording("session")
    with patch("browser_harness.helpers.js", side_effect=RuntimeError("boom")), \
         patch("browser_harness.helpers.cdp", side_effect=RuntimeError("boom")):
        recorder.observe("click_at_xy", (5, 6), {})
    events = _events(rec)
    assert events[-1]["helper"] == "click_at_xy"
    assert "frame" not in events[-1]
    assert "url" not in events[-1]


def test_kwargs_and_defaults_in_details(workspace, fake_png):
    js_p, cdp_p = _record(fake_png)
    with js_p, cdp_p:
        rec = recorder.start_recording("session")
        recorder.observe("scroll", (), {"x": 10, "y": 20})
        recorder.observe("goto_url", ("https://a.example",), {})
    scroll, nav = _events(rec)[1:]
    assert (scroll["x"], scroll["y"], scroll["dy"], scroll["dx"]) == (10, 20, -300, 0)
    assert nav["to"] == "https://a.example"
