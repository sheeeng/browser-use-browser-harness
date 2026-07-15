"""Session recording: one screenshot + one trace line per action.

A recording is just a folder:

    <BH_AGENT_WORKSPACE>/recordings/<name>/
      meta.json      # {name, title, started}
      events.jsonl   # one JSON object per action: helper, coords/text,
                     # url, viewport, focused-element box, frame filename
      0001.jpg ...   # viewport screenshot after each action

start_recording()/stop_recording() toggle it. A marker file carries the
active state across CLI invocations (the daemon is untouched). run.py calls
observe() after every traced helper; only helpers in ACTIONS produce a
frame. Recording failures are swallowed — they must never break the run.

By default every session is auto-recorded without an explicit
start_recording(): recordings are named session-<timestamp> and roll over
to a fresh one after BH_RECORD_IDLE seconds (default 180) without actions.
BH_RECORD=0 turns auto-recording off.

Turning a recording into a video is the make-video skill's job:
interaction-skills/make-video.md.
"""
import base64, json, os, re, time
from pathlib import Path

from . import paths

# Helpers that change what's on screen. Read-only helpers (js, page_info,
# capture_screenshot, ...) don't get frames — they'd bloat recordings of
# inspection-heavy sessions without adding visual beats.
ACTIONS = {
    "goto_url", "click_at_xy", "type_text", "fill_input", "press_key",
    "scroll", "dispatch_key", "upload_file", "new_tab", "switch_tab",
    "close_tab", "ensure_real_tab",
    "wait", "wait_for_load", "wait_for_element", "wait_for_network_idle",
}

_TEXT_LIMIT = 500
_SETTLE_SECONDS = 0.15  # let the page paint before the post-action frame

# Credential-bearing query/fragment params (OAuth codes, tokens, session
# state) are scrubbed from every URL written to events.jsonl — auth redirects
# otherwise land real secrets in a folder people share.
_URL_SECRETS = re.compile(
    r"([?&#](?:code|access_token|id_token|refresh_token|token|assertion"
    r"|client_secret|client_info|session_state|api_?key|sig|signature"
    r"|auth|authorization|password|secret)=)[^&#]+",
    re.IGNORECASE,
)


def _scrub_url(url):
    return _URL_SECRETS.sub(r"\1REDACTED", str(url))

# Page context per event. The focused-element box is what lets a video zoom
# in on the input the agent is typing into; `input` flags password fields
# so their text can be masked in the trace.
_CTX_JS = (
    "(()=>{const o={url:location.href,title:document.title,"
    "w:innerWidth,h:innerHeight,sx:scrollX,sy:scrollY,dpr:devicePixelRatio};"
    "const e=document.activeElement;"
    "if(e&&e!==document.body&&e!==document.documentElement){"
    "const r=e.getBoundingClientRect();"
    "if(r.width||r.height)o.box={x:r.x,y:r.y,w:r.width,h:r.height};"
    "o.input=String(e.type||e.tagName||'').toLowerCase();}"
    "return o})()"
)


def _recordings_root():
    return paths.workspace_dir() / "recordings"


def _marker():
    return _recordings_root() / f".active-{os.environ.get('BU_NAME', 'default')}"


def start_recording(name=None, title=None):
    """Record the session: one screenshot + trace line per action, until
    stop_recording(). Survives across CLI invocations. Returns the recording
    directory. `title` is used later as the video title.
    See interaction-skills/make-video.md to turn the recording into a video."""
    name = name or time.strftime("rec-%Y%m%d-%H%M%S")
    d = _recordings_root() / name
    d.mkdir(parents=True, exist_ok=True)
    meta = {"name": name, "title": title, "started": round(time.time(), 3)}
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    _marker().write_text(str(d), encoding="utf-8")
    _capture(d, "start_recording")
    print(f"recording to {d}")
    return str(d)


def stop_recording():
    """Stop the active recording. Returns its directory, or None if idle."""
    d = recording_dir()
    if d is None:
        print("no active recording")
        return None
    _capture(Path(d), "stop_recording")
    _marker().unlink(missing_ok=True)
    frames = sum(1 for _ in Path(d).glob("*.jpg"))
    print(f"recording saved: {d} ({frames} frames)")
    return d


def recording_dir():
    """Directory of the active recording, or None."""
    m = _marker()
    if not m.exists():
        return None
    d = m.read_text(encoding="utf-8").strip()
    return d if Path(d).is_dir() else None


def _auto_enabled():
    """Auto-record every session unless BH_RECORD is set falsy. On by default —
    recording is cheap (one jpg per action) and making a video stays a separate,
    deliberate step; BH_RECORD=0 opts out entirely."""
    return os.environ.get("BH_RECORD", "1").strip().lower() not in ("0", "false", "no", "off")


# Auto-recordings roll over after this idle gap — a pause since the last action
# marks the end of one task and the start of the next, so a naive always-on
# recording doesn't merge unrelated sessions or grow forever.
def _auto_idle_gap():
    try:
        return float(os.environ.get("BH_RECORD_IDLE", "180"))
    except ValueError:
        return 180.0


def _auto_start():
    """Begin an auto-recording silently — no stdout (agents parse it)."""
    stamp = time.strftime("session-%Y%m%d-%H%M%S")
    name, n = stamp, 2
    while (_recordings_root() / name).exists():  # avoid same-second collisions
        name = f"{stamp}-{n}"; n += 1
    d = _recordings_root() / name
    d.mkdir(parents=True, exist_ok=True)
    meta = {"name": name, "title": None, "started": round(time.time(), 3), "auto": True}
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    _marker().write_text(str(d), encoding="utf-8")
    return d


def _auto_is_stale(d):
    """True if the active auto-recording has gone idle past the rollover gap."""
    try:
        if not json.loads((Path(d) / "meta.json").read_text()).get("auto"):
            return False  # explicit start_recording() never auto-rolls
        frames = list(Path(d).glob("*.jpg"))
        if not frames:
            return False
        newest = max(f.stat().st_mtime for f in frames)
        return (time.time() - newest) > _auto_idle_gap()
    except Exception:
        return False


def observe(name, args, kwargs, duration=None):
    """Called by run.py after each traced helper succeeds. Never raises."""
    if name not in ACTIONS:
        return
    try:
        d = recording_dir()
        if d is not None and _auto_is_stale(d):
            _marker().unlink(missing_ok=True)
            d = None
        if d is None:
            if not _auto_enabled():
                return
            d = str(_auto_start())
        time.sleep(_SETTLE_SECONDS)
        _capture(Path(d), name, args, kwargs, duration)
    except Exception:
        pass


def _capture(d, helper, args=(), kwargs=None, duration=None):
    from . import helpers  # late: helpers imports recorder at its bottom

    event = {"ts": round(time.time(), 3), "helper": helper}
    if duration is not None:
        event["duration"] = duration
    try:
        event.update(helpers.js(_CTX_JS) or {})
    except Exception:
        pass
    event.update(_details(helper, args, kwargs or {}, event))
    for k in ("url", "to"):
        if k in event:
            event[k] = _scrub_url(event[k])
    try:
        shot = helpers.cdp("Page.captureScreenshot", format="jpeg", quality=80)
        frame = f"{sum(1 for _ in d.glob('*.jpg')) + 1:04d}.jpg"
        (d / frame).write_bytes(base64.b64decode(shot["data"]))
        event["frame"] = frame
    except Exception:
        pass
    with (d / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _details(helper, args, kwargs, ctx):
    def arg(i, name, default=None):
        return args[i] if len(args) > i else kwargs.get(name, default)

    d = {}
    if helper == "click_at_xy":
        d["x"], d["y"] = arg(0, "x"), arg(1, "y")
    elif helper == "scroll":
        d["x"], d["y"] = arg(0, "x"), arg(1, "y")
        d["dy"], d["dx"] = arg(2, "dy", -300), arg(3, "dx", 0)
    elif helper in ("goto_url", "new_tab"):
        d["to"] = arg(0, "url")
    elif helper == "type_text":
        d["text"] = _mask(arg(0, "text", ""), ctx)
    elif helper == "fill_input":
        d["selector"] = arg(0, "selector")
        d["text"] = _mask(arg(1, "text", ""), ctx)
    elif helper == "press_key":
        d["key"] = arg(0, "key")
    elif helper == "dispatch_key":
        d["selector"], d["key"] = arg(0, "selector"), arg(1, "key", "Enter")
    elif helper == "wait_for_element":
        d["selector"] = arg(0, "selector")
    return {k: v for k, v in d.items() if v is not None}


def _mask(text, ctx):
    text = str(text)
    if ctx.get("input") == "password":
        return "•" * len(text)
    return text[:_TEXT_LIMIT]
