"""This Fish Does Not Exist.

Every `generate_interval_s` a new procedural fish is generated (ported from
https://github.com/LingDong-/fishdraw), laid out as a textbook page, and
revealed on a Waveshare 7.5" e-paper panel stroke by stroke using fast
partial refreshes.

Three layout modes (see layout.py):
  plate  - landscape specimen plate, name + discovery date
  figure - landscape textbook figure with a classification block
  page   - portrait textbook page whose prose types out word by word, written
           by the local LLM brick (with a template fallback)

The raster is produced here with PIL rather than on the MCU: the sketch just
holds a 1-bit framebuffer, so detail is not bounded by MCU memory and each
reveal step only ships the bytes that changed.
"""

import base64
import datetime
import io
import json
import logging
import os
import tempfile
import threading
import time
import zoneinfo

from arduino.app_utils import App, Bridge
from arduino.app_bricks.web_ui import WebUI
from PIL import Image, ImageDraw

import description
import fishdraw
import layout

logger = logging.getLogger(__name__)

PANEL_W, PANEL_H = 800, 480
ROW_BYTES = (PANEL_W + 7) // 8

MAX_CHUNK_BYTES = 88               # Bridge payloads must stay under ~96 bytes

# The driver's stock partial waveform is 70 frames (~1.6s); the sketch
# installs a shorter one. Starting estimate only - the reveal loop re-measures
# and adapts so it finishes on schedule.
PARTIAL_REFRESH_S = 0.7
FULL_REFRESH_S = 4.2

# Partial-update waveform frame counts (T1 charge-balance, T2 extension,
# T3 colour-change, T4 extension), replacing the driver's 30/5/30/5. Scaled by
# the `waveform` setting: fewer frames is faster but fainter.
BASE_LUT = (8, 2, 14, 2)

# Longest stroke drawn as one reveal unit, so long outline curves don't pop
# into view in a single step.
MAX_UNIT_POINTS = 10

# Fish generation measured at a few seconds; 20 is a safe allowance for a
# dense scale mesh on a big body.
GEN_ESTIMATE_S = 20


def _min_interval(s):
    """Shortest period the current settings can actually sustain."""
    base = GEN_ESTIMATE_S + FULL_REFRESH_S + 10
    if s.get("use_llm"):
        base += description.LLM_TIMEOUT_S      # worst case: the model times out
    if s.get("progressive"):
        base += max(PARTIAL_REFRESH_S * 4, float(s.get("draw_duration_s", 0)))
    return int(round(base))


# Settings and the specimen tally are persisted here so they survive restarts.
# First writable path wins.
STATE_PATHS = (
    "/app/.fishclock_state.json",
    os.path.expanduser("~/.fishclock_state.json"),
    os.path.join(tempfile.gettempdir(), "fishclock_state.json"),
)

settings = {
    "mode": "page",
    # False shows each finished page in one refresh instead of drawing it on
    # progressively - much shorter cycles, no reveal.
    "progressive": False,
    "generate_interval_s": 120,
    # only used when progressive is on; a smooth reveal needs minutes
    "draw_duration_s": 240,
    "waveform": 1.0,
    "use_llm": True,
    "portrait_flip": False,        # flip portrait 180 if the panel is mounted the other way
    # IANA name, e.g. "Europe/London". Empty uses the board's local time.
    "timezone": "Europe/London",
}

state = {
    "status": "starting",
    "fish_count": 0,        # replaced with the persisted tally below
    "last_name": "",
    "last_generated_at": "",
    "last_text_source": "",
    "last_error": "",
}

_lock = threading.Lock()
_last_preview_png = None
_generate_now = threading.Event()
_state_path = None

web_ui = WebUI()


def _coerce_setting(key, value):
    """Coerce an incoming value to the type of the existing default. bool is
    checked before int because bool is a subclass of int."""
    current = settings[key]
    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, int):
        return int(value)
    return str(value)


def _load_state():
    """Restore saved settings and the specimen tally, remembering the first
    writable path for later saves. Falls back to defaults in memory."""
    global _state_path
    loaded = None
    for path in STATE_PATHS:
        try:
            with open(path) as fh:
                loaded = json.load(fh)
            _state_path = path
            break
        except FileNotFoundError:
            if _state_path is None:
                try:    # claim it if we can write here
                    with open(path, "w") as fh:
                        json.dump({"fish_count": 0, "settings": {}}, fh)
                    _state_path = path
                except OSError:
                    continue
        except Exception:
            logger.warning("could not read %s; ignoring", path)

    if not loaded:
        logger.info("no saved state; using defaults (state file: %s)", _state_path)
        return 0

    for key, value in (loaded.get("settings") or {}).items():
        if key not in settings:
            continue
        try:
            settings[key] = _coerce_setting(key, value)
        except (TypeError, ValueError):
            logger.warning("ignoring saved setting %r=%r", key, value)
    count = int(loaded.get("fish_count", 0))
    logger.info("restored settings and specimen count %d from %s", count, _state_path)
    return count


def _tzinfo(name):
    """Resolve an IANA timezone name, or None for the board's local time."""
    if not name:
        return None
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception:
        logger.warning("unknown timezone %r; using local time", name)
        return None


def _format_now(tz_name):
    """Timestamp for the specimen record. Caller must already hold _lock if it
    is reading tz_name out of settings - the lock is not reentrant."""
    tz = _tzinfo(tz_name)
    now = datetime.datetime.now(tz)
    # %Z is only meaningful on an aware datetime
    return now.strftime("%Y-%m-%d %H:%M %Z").strip() if tz else now.strftime("%Y-%m-%d %H:%M")


def _now_string():
    with _lock:
        tz_name = settings["timezone"]
    return _format_now(tz_name)


def _save_state(count, settings_snapshot):
    """Persist tally + settings. Takes a snapshot rather than reading globals
    so callers already holding _lock can use it safely."""
    if _state_path is None:
        return
    try:
        with open(_state_path, "w") as fh:
            json.dump({"fish_count": count, "settings": settings_snapshot}, fh)
    except OSError:
        logger.warning("could not persist state to %s", _state_path)


state["fish_count"] = _load_state()
# a restored period might predate a settings change that made it too short
_floor = _min_interval(settings)
if settings["generate_interval_s"] < _floor:
    logger.info("raising restored period %ss to the %ss floor",
                settings["generate_interval_s"], _floor)
    settings["generate_interval_s"] = _floor


# --------------------------------------------------------------- rendering --
def _render(ops, n_show, canvas):
    """Rasterise the first n_show ops into a 1-bit logical-space frame. PIL
    mode "1" is MSB-first with bit 1 = white, matching the sketch's buffer."""
    w, h, _portrait = canvas
    img = Image.new("1", (w, h), 1)
    draw = ImageDraw.Draw(img)
    f = layout.fonts()
    for op in ops[:n_show]:
        if op[0] == "line":
            pts = op[1]
            if len(pts) >= 2:
                draw.line([(x, y) for x, y in pts], fill=0, width=1)
        else:
            _, x, y, s, key, oblique = op
            layout.draw_text(img, draw, x, y, s, f[key], oblique)
    return img


def _to_panel(img, canvas, flip):
    """Logical canvas -> panel orientation (the panel is physically 800x480)."""
    if not canvas[2]:
        return img
    out = img.transpose(Image.ROTATE_270 if flip else Image.ROTATE_90)
    return out


def _diff_runs(prev, cur, max_gap=MAX_CHUNK_BYTES):
    """Byte runs where cur differs from prev. Equal gaps up to a whole payload
    are absorbed into the run: a Bridge round-trip costs the same whether it
    carries 3 bytes or 88, and round-trips - not the refresh - turned out to
    dominate the step time (~6500 writes per fish at ~10ms each)."""
    runs = []
    n = len(cur)
    i = 0
    while i < n:
        if cur[i] == prev[i]:
            i += 1
            continue
        j = i + 1
        gap = 0
        while j < n and gap <= max_gap:
            if cur[j] != prev[j]:
                gap = 0
            else:
                gap += 1
            j += 1
        while j > i and cur[j - 1] == prev[j - 1]:
            j -= 1
        runs.append((i, cur[i:j]))
        i = j
    return runs


def _runs_bbox(runs):
    """Pixel bbox covering changed bytes, x/w snapped to byte boundaries."""
    min_row, max_row = PANEL_H, -1
    min_col, max_col = ROW_BYTES, -1
    for offset, payload in runs:
        start_row, start_col = divmod(offset, ROW_BYTES)
        end_row, end_col = divmod(offset + len(payload) - 1, ROW_BYTES)
        min_row = min(min_row, start_row)
        max_row = max(max_row, end_row)
        if end_row > start_row:          # run wraps rows, so it spans them fully
            min_col, max_col = 0, ROW_BYTES - 1
        else:
            min_col = min(min_col, start_col)
            max_col = max(max_col, end_col)
    if max_row < 0:
        return None
    return min_col * 8, min_row, (max_col - min_col + 1) * 8, max_row - min_row + 1


# ------------------------------------------------------------------ bridge --
def _bridge_call(name, *a, timeout=None):
    try:
        if timeout is not None:
            return Bridge.call(name, *a, timeout=timeout)
        return Bridge.call(name, *a)
    except Exception as exc:
        logger.warning("Bridge.call(%s) failed: %s", name, exc)
        return None


def _send_runs(runs):
    calls = 0
    for offset, payload in runs:
        for k in range(0, len(payload), MAX_CHUNK_BYTES):
            piece = payload[k:k + MAX_CHUNK_BYTES]
            if _bridge_call("epd_fb_write", offset + k, piece, timeout=10) is None:
                raise RuntimeError("epd_fb_write failed")
            calls += 1
    return calls


# ------------------------------------------------------------------- build --
def _fish_ops(mode):
    region = layout.fish_area(mode)
    polylines, name, traits = fishdraw.generate(region[2], region[3], pad=8)
    units = []
    for pl in polylines:
        if len(pl) < 2:
            continue
        pts = [(x + region[0], y + region[1]) for x, y in pl]
        if len(pts) <= MAX_UNIT_POINTS:
            units.append(("line", pts))
            continue
        i = 0
        while i < len(pts) - 1:
            piece = pts[i:i + MAX_UNIT_POINTS]
            if len(piece) < 2:
                break
            units.append(("line", piece))
            i += len(piece) - 1
    return units, name, traits, region


def _build_page(mode, use_llm, number):
    fish_ops, name, traits, region = _fish_ops(mode)
    when = _now_string()
    body_start = None
    source = ""

    if mode == "page":
        text, source = description.describe(name, traits, use_llm=use_llm)
        ops, body_start = layout.page(name, when, region, fish_ops, text, number)
    elif mode == "figure":
        ops = layout.figure(name, when, region, fish_ops, number)
    else:
        ops = layout.plate(name, when, region, fish_ops)
    return ops, name, when, body_start, source


# ------------------------------------------------------------------ reveal --
def _reveal(ops, phases, canvas, flip, prev, deadline):
    """Reveal ops progressively. `phases` is a list of (end_index, seconds):
    each gets its own time budget so the prose isn't crowded out by the
    thousands of fish strokes ahead of it. Within a phase the batch size is
    re-planned every step from the time left and the measured cost of a step,
    staying as fine-grained as possible while finishing on schedule.

    `deadline` is a hard wall-clock stop: past it the remainder is drawn in a
    single step, so a cycle can't run on into the next one."""
    shown = 0
    steps = 0
    calls = 0
    step_cost = PARTIAL_REFRESH_S
    last_frame = None

    for end, seconds in phases:
        if end <= shown:
            continue
        t_phase = time.monotonic()
        while shown < end:
            if time.monotonic() >= deadline:
                shown = end          # out of time: show the rest at once
            else:
                left = seconds - (time.monotonic() - t_phase)
                steps_left = max(1, int(left / step_cost))
                batch = max(1, -(-(end - shown) // steps_left))
                shown = min(end, shown + batch)

            t_step = time.monotonic()
            frame = _render(ops, shown, canvas)
            cur = _to_panel(frame, canvas, flip).tobytes()
            runs = _diff_runs(prev, cur)
            if runs:
                calls += _send_runs(runs)
                box = _runs_bbox(runs)
                if box and _bridge_call("epd_refresh_window", *box, timeout=60) is None:
                    raise RuntimeError("epd_refresh_window failed")
            prev = cur
            last_frame = frame
            steps += 1
            measured = time.monotonic() - t_step
            step_cost = step_cost * 0.7 + measured * 0.3
    return last_frame, prev, steps, calls, step_cost


def _run_cycle():
    t_cycle = time.monotonic()
    with _lock:
        mode = settings["mode"] if settings["mode"] in layout.MODES else "plate"
        interval = max(30.0, float(settings["generate_interval_s"]))
        want_draw = float(settings["draw_duration_s"])
        waveform = max(0.3, min(3.0, float(settings["waveform"])))
        use_llm = bool(settings["use_llm"])
        flip = bool(settings["portrait_flip"])
        progressive = bool(settings["progressive"])

    canvas = layout.CANVAS[mode]
    with _lock:
        number = state["fish_count"] + 1

    ops, name, when, body_start, source = _build_page(mode, use_llm, number)

    total = len(ops)
    blank = _to_panel(Image.new("1", (canvas[0], canvas[1]), 1), canvas, flip).tobytes()

    if not progressive:
        # Show the finished page in one go: clear, stream the whole frame,
        # single full refresh. The only cost is generation plus ~4s of panel.
        final = _render(ops, total, canvas)
        if _bridge_call("epd_fb_clear", timeout=10) is None:
            raise RuntimeError("epd_fb_clear failed")
        calls = _send_runs(_diff_runs(blank, _to_panel(final, canvas, flip).tobytes()))
        if _bridge_call("epd_refresh_full", timeout=90) is None:
            raise RuntimeError("epd_refresh_full failed")
        logger.warning(
            "showed %r [%s] in %.1fs: %d ops, %d bridge writes, text=%s",
            name, mode, time.monotonic() - t_cycle, total, calls, source or "n/a",
        )
    else:
        # Generation (and the LLM) can take a while; shrink the reveal so the
        # cycle still fits in the period rather than running into the next one.
        spent = time.monotonic() - t_cycle
        available = interval - spent - FULL_REFRESH_S - 8
        draw_duration = max(PARTIAL_REFRESH_S * 4, min(want_draw, available))

        # One full refresh per fish, up front: wipes the previous specimen and
        # gives a clean white page. Everything after is partial, so the drawing
        # builds up without the screen flashing again.
        if _bridge_call("epd_fb_clear", timeout=10) is None:
            raise RuntimeError("epd_fb_clear failed")
        if _bridge_call("epd_refresh_full", timeout=90) is None:
            raise RuntimeError("epd_refresh_full failed")

        lut = tuple(max(1, int(round(v * waveform))) for v in BASE_LUT)
        if _bridge_call("epd_fast_partial", *lut, timeout=60) is None:
            logger.warning("fast partial waveform unavailable; using driver default")

        # Split the budget: prose gets roughly one step per word so it reads
        # as typing, and the fish gets whatever is left.
        if body_start is not None and body_start < total:
            words = total - body_start
            prose_time = min(draw_duration * 0.55, words * PARTIAL_REFRESH_S)
            phases = [(body_start, max(1.0, draw_duration - prose_time)),
                      (total, prose_time)]
        else:
            phases = [(total, draw_duration)]

        logger.warning(
            "drawing %r [%s]: %d ops (%s), ~%.0fs, waveform %s (%d frames), text=%s",
            name, mode, total,
            "fish %d + prose %d" % (body_start, total - body_start) if body_start else "fish only",
            draw_duration, lut, sum(lut), source or "n/a",
        )

        deadline = time.monotonic() + draw_duration
        final, _prev, steps, calls, step_cost = _reveal(
            ops, phases, canvas, flip, blank, deadline)
        if final is None:
            final = _render(ops, total, canvas)

        logger.warning(
            "drew %r in %.1fs: %d steps, %d bridge writes, %.2fs/step",
            name, time.monotonic() - t_cycle, steps, calls, step_cost,
        )

    buf = io.BytesIO()
    final.convert("L").save(buf, format="PNG")

    global _last_preview_png
    with _lock:
        _last_preview_png = buf.getvalue()
        state["fish_count"] = number
        state["last_name"] = name
        state["last_generated_at"] = when
        state["last_text_source"] = source
        state["last_error"] = ""
        snapshot = dict(settings)
    _save_state(number, snapshot)


def _worker():
    time.sleep(3.0)  # let the sketch come up and clear the panel
    while True:
        started = time.monotonic()
        try:
            state["status"] = "drawing"
            _run_cycle()
        except Exception as exc:
            logger.exception("generation cycle failed")
            with _lock:
                state["last_error"] = str(exc)
        finally:
            state["status"] = "ready"

        with _lock:
            interval = max(30.0, float(settings["generate_interval_s"]))
        _generate_now.wait(timeout=max(1.0, interval - (time.monotonic() - started)))
        _generate_now.clear()


threading.Thread(target=_worker, daemon=True).start()


# --------------------------------------------------------------- web API --
def api_status():
    with _lock:
        return dict(state)


def api_get_settings():
    with _lock:
        out = dict(settings)
        out["min_interval_s"] = _min_interval(settings)
        out["now"] = _format_now(settings["timezone"])
        return out


def api_llm():
    """Prompt sent to the model and its raw reply, for inspection in the UI."""
    return description.last_exchange()


def api_timezones():
    """IANA zones the board knows about, for the settings picker."""
    try:
        return {"zones": sorted(zoneinfo.available_timezones())}
    except Exception:
        logger.exception("could not enumerate timezones")
        return {"zones": ["UTC"]}


def api_set_settings(payload: dict):
    with _lock:
        for key, value in (payload or {}).items():
            if key not in settings:
                continue
            try:
                settings[key] = _coerce_setting(key, value)
            except (TypeError, ValueError):
                continue
        if settings["mode"] not in layout.MODES:
            settings["mode"] = "plate"
        tz_name = settings["timezone"].strip()
        if tz_name and _tzinfo(tz_name) is None:
            tz_name = ""        # unknown zone: fall back to local rather than failing
        settings["timezone"] = tz_name
        # the period has to cover generation, the LLM, the opening refresh and
        # (if drawing progressively) the reveal itself
        floor = _min_interval(settings)
        if settings["generate_interval_s"] < floor:
            settings["generate_interval_s"] = floor
        snapshot = dict(settings)
        count = state["fish_count"]
    _save_state(count, snapshot)        # settings persist across restarts
    out = dict(snapshot)
    out["min_interval_s"] = floor
    out["now"] = _format_now(snapshot["timezone"])
    return out


def api_preview():
    with _lock:
        png = _last_preview_png
    if png is None:
        return {"image": None}
    return {"image": "data:image/png;base64," + base64.b64encode(png).decode()}


def api_generate():
    _generate_now.set()
    return {"ok": True}


web_ui.expose_api("GET", "/status", api_status)
web_ui.expose_api("GET", "/settings", api_get_settings)
web_ui.expose_api("GET", "/timezones", api_timezones)
web_ui.expose_api("GET", "/llm", api_llm)
web_ui.expose_api("POST", "/settings", api_set_settings)
web_ui.expose_api("GET", "/preview", api_preview)
web_ui.expose_api("POST", "/generate", api_generate)

App.run()
