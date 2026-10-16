import io
import json
import logging
import os
import tempfile
import threading
from datetime import date, datetime

from PIL import Image, ImageDraw

import i18n
import render as R

log = logging.getLogger("dashink.layout")

# Next to this file by default, so everything stays inside the dashink folder.
PATH = os.environ.get("DASHINK_LAYOUT",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)), "layout.json"))
W, H = R.W, R.H

MAX_ROWS, MAX_CELLS = 12, 4
RULES = ("none", "light", "dark")

# name -> label and the options the editor offers for it.
WIDGETS = {
    "header":        {"label": "Header (node + date)", "opts": ["text"]},
    "kuma":          {"label": "Monitors tile", "opts": []},
    "media":         {"label": "Storage tile", "opts": []},
    "host":          {"label": "Host tile (CPU/RAM)", "opts": []},
    "weather_today": {"label": "Weather: today", "opts": []},
    "weather_days":  {"label": "Weather: forecast days", "opts": ["days"]},
    "clock":         {"label": "Clock", "opts": ["size"]},
    "text":          {"label": "Custom text", "opts": ["text", "size", "align", "bold"]},
    "footer":        {"label": "Footer (uptime + updated)", "opts": []},
    "spacer":        {"label": "Empty space", "opts": []},
}


# --- default, mirroring what render.render() draws today --------------------

def default_layout(state):
    """The built-in panel, as rows. Top-row tiles follow the same priority as
    render.render(): monitors, storage, then host if a slot is free."""
    tiles = []
    if state.get("kuma"):
        tiles.append("kuma")
    if state.get("media"):
        tiles.append("media")
    if len(tiles) < 2:
        tiles.append("host")
    rows = [
        {"height": 64, "rule": "dark", "cells": [{"widget": "header"}]},
        {"height": 236, "rule": "dark", "divider": len(tiles) > 1,
         "cells": [{"widget": t} for t in tiles]},
    ]
    if state.get("weather"):
        rows += [
            {"height": 210, "rule": "light", "cells": [{"widget": "weather_today"}]},
            {"height": 216, "rule": "dark",
             "cells": [{"widget": "weather_days", "days": 3}]},
        ]
    else:
        rows.append({"height": 426, "rule": "dark", "cells": [{"widget": "spacer"}]})
    rows.append({"height": 74, "rule": "none", "cells": [{"widget": "footer"}]})
    return {"version": 1, "rows": rows}


# --- validation -------------------------------------------------------------

def validate(raw):
    """Return a cleaned layout or raise ValueError with a readable reason."""
    if not isinstance(raw, dict) or not isinstance(raw.get("rows"), list):
        raise ValueError("expected {\"rows\": [...]}")
    rows = raw["rows"]
    if not 1 <= len(rows) <= MAX_ROWS:
        raise ValueError(f"need 1 to {MAX_ROWS} rows")
    out, total = [], 0
    for i, row in enumerate(rows, 1):
        try:
            height = int(row["height"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"row {i}: height must be a number")
        if not 16 <= height <= H:
            raise ValueError(f"row {i}: height must be 16 to {H}")
        total += height
        rule = row.get("rule", "none")
        if rule not in RULES:
            raise ValueError(f"row {i}: rule must be one of {', '.join(RULES)}")
        cells_in = row.get("cells")
        if not isinstance(cells_in, list) or not 1 <= len(cells_in) <= MAX_CELLS:
            raise ValueError(f"row {i}: need 1 to {MAX_CELLS} cells")
        cells = []
        for j, cell in enumerate(cells_in, 1):
            name = cell.get("widget") if isinstance(cell, dict) else None
            if name not in WIDGETS:
                raise ValueError(f"row {i} cell {j}: unknown widget {name!r}")
            c = {"widget": name}
            try:
                c["w"] = max(1, min(int(cell.get("w", 1)), 12))
            except (TypeError, ValueError):
                raise ValueError(f"row {i} cell {j}: w must be a number")
            if "text" in cell:
                c["text"] = str(cell["text"])[:200]
            if "size" in cell:
                c["size"] = max(8, min(int(cell["size"]), 240))
            if "days" in cell:
                c["days"] = max(1, min(int(cell["days"]), 5))
            if cell.get("align") in ("left", "center", "right"):
                c["align"] = cell["align"]
            if "bold" in cell:
                c["bold"] = bool(cell["bold"])
            cells.append(c)
        out.append({"height": height, "rule": rule,
                    "divider": bool(row.get("divider", False)), "cells": cells})
    if total > H:
        raise ValueError(f"rows add up to {total}px; the panel is {H}px tall")
    return {"version": 1, "rows": out}


# --- storage ----------------------------------------------------------------

_lock = threading.Lock()
_cache = {"mtime": None, "layout": None}


def load():
        try:
        mtime = os.stat(PATH).st_mtime
    except OSError:
        return None
    with _lock:
        if _cache["mtime"] != mtime:
            try:
                with open(PATH) as f:
                    _cache["layout"] = validate(json.load(f))
            except (OSError, ValueError) as exc:
                log.warning("ignoring %s: %s", PATH, exc)
                _cache["layout"] = None
            _cache["mtime"] = mtime
        return _cache["layout"]


def save(layout):
    data = json.dumps(layout, indent=1)
    folder = os.path.dirname(PATH) or "."
    try:
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data)
            os.replace(tmp, PATH)  # atomic: a render never reads half a file
            return
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        log.info("atomic save failed (%s); writing in place", exc)
    with open(PATH, "w") as f:  # raises OSError if the path is truly unwritable
        f.write(data)


def clear():
    try:
        os.remove(PATH)
    except FileNotFoundError:
        pass
    except OSError:  # e.g. a bind-mounted file cannot be unlinked: blank it instead
        open(PATH, "w").close()


# --- drawing ----------------------------------------------------------------

class _Pen:

    def __init__(self, d, dy=0, img=None):
        self.d, self.dy, self.img = d, dy, img

    def _pts(self, xy):
        return [v + (self.dy if i % 2 else 0) for i, v in enumerate(xy)]

    def text(self, xy, *a, **k):
        self.d.text((xy[0], xy[1] + self.dy), *a, **k)

    def line(self, xy, *a, **k):
        self.d.line(self._pts(xy), *a, **k)

    def rectangle(self, xy, *a, **k):
        self.d.rectangle(self._pts(xy), *a, **k)


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _fmt(text, ctx):
    now = ctx["now"]
    return str(text).format_map(_Safe(
        time=now.strftime("%H:%M"), date=now.strftime(ctx["s"]["date"]),
        weekday=ctx["t"]["weekdays_long"][now.weekday()], node=ctx["state"].get("node", "")))


def _placeholder(pen, w, h, label, ctx):
    pen.text((w // 2, h // 2), f"{label}: not configured", font=R.regular(18),
             fill=R.LIGHT, anchor="mm")


def _tile(fn, key):
    def draw(pen, w, h, o, ctx):
        src = ctx["state"].get(key)
        if src is None and key != "host":
            return _placeholder(pen, w, h, key, ctx)
        cw = max(w, 300)
        native = Image.new("L", (cw, 236), R.WHITE)
        fn(_Pen(ImageDraw.Draw(native), -64), cw // 2, src or {}, ctx["t"])
        scale = min(1.0, w / cw, h / 236)
        if scale < 1.0:
            native = native.resize((max(1, round(cw * scale)), max(1, round(236 * scale))),
                                   Image.HAMMING)
        pen.img.paste(native, ((w - native.width) // 2, (h - native.height) // 2))
    return draw


def _header(pen, w, h, o, ctx):
    s, t, now = ctx["s"], ctx["t"], ctx["now"]
    left = _fmt(o["text"], ctx) if o.get("text") else ctx["state"].get("node", "")
    pen.text((R.MARGIN, h // 2), left, font=R.bold(24), fill=R.BLACK, anchor="lm")
    pen.text((w - R.MARGIN, h // 2),
             f"{t['weekdays_long'][now.weekday()]}, {now.strftime(s['date'])}",
             font=R.regular(22), fill=R.BLACK, anchor="rm")


def _weather_today(pen, w, h, o, ctx):
    s, t, wx = ctx["s"], ctx["t"], ctx["state"].get("weather")
    if wx is None:
        return _placeholder(pen, w, h, "weather", ctx)
    top = (h - 210) // 2
    m = R.MARGIN
    label = ctx["state"].get("label", "")
    head = s["weather"] + (f" · {label}" if label else "") + (" !" if wx.get("stale") else "")
    pen.text((m, top + 36), head, font=R.bold(17), fill=R.GREY, anchor="la")
    if not wx.get("ok"):
        pen.text((m, top + 80), s["no_weather"], font=R.regular(24), fill=R.GREY, anchor="la")
        return
    full, _ = t["wmo"].get(wx.get("code"), ("—", "—"))
    pen.text((m, top + 72), f"{round(wx['temp'])}°", font=R.bold(72), fill=R.BLACK, anchor="la")
    pen.text((w - m, top + 78), s["today"], font=R.bold(22), fill=R.GREY, anchor="ra")
    pen.text((w - m, top + 112), full, font=R.regular(26), fill=R.BLACK, anchor="ra")
    if wx.get("tmin") is not None and wx.get("tmax") is not None:
        pen.text((w - m, top + 160), f"{round(wx['tmin'])}° / {round(wx['tmax'])}°",
                 font=R.regular(22), fill=R.GREY, anchor="ra")
    rise, set_ = wx.get("sunrise"), wx.get("sunset")
    if rise and set_:
        pen.text((m, top + 160), f"☀ {rise} ☾ {set_}", font=R.regular(22), fill=R.GREY, anchor="la")


def _weather_days(pen, w, h, o, ctx):
    t, wx = ctx["t"], ctx["state"].get("weather")
    if not wx or not wx.get("ok"):
        return
    n = o.get("days", 3)
    days = (wx.get("days") or [])[:n]
    top, m = (h - 216) // 2, R.MARGIN
    for i, day in enumerate(days):
        cx = m + (w - 2 * m) * (i + 0.5) / n
        name = t["weekdays"][date.fromisoformat(day["date"]).weekday()]
        _, short = t["wmo"].get(day["code"], ("—", "—"))
        pen.text((cx, top + 42), name, font=R.bold(26), fill=R.BLACK, anchor="ma")
        pen.text((cx, top + 88), f"{round(day['tmin'])}° / {round(day['tmax'])}°",
                 font=R.regular(24), fill=R.BLACK, anchor="ma")
        pen.text((cx, top + 130), short, font=R.regular(20), fill=R.GREY, anchor="ma")
        pop = day.get("pop")
        if pop is not None and pop >= 20:
            pen.text((cx, top + 170), f"☂ {round(pop)}%", font=R.bold(20),
                     fill=R.BLACK, anchor="ma")


def _footer(pen, w, h, o, ctx):
    s, st, now = ctx["s"], ctx["state"], ctx["now"]
    host, media, wx, kuma = st.get("host") or {}, st.get("media"), st.get("weather"), st.get("kuma")
    named = [(s["host"], host)]
    for label, src in ((s["media"], media), (s["weather"], wx), (s["services"], kuma)):
        if src is not None:
            named.append((label, src))
    stale = [n for n, src in named if not src.get("ok") or src.get("stale")]
    right = (f"{s['stale']} " + ", ".join(stale) if stale
             else f"{s['updated']} {now.strftime('%H:%M')}")
    smart = (media or {}).get("smart") or {}
    bad = smart.get("bad") or 0
    y = h // 2
    if bad:
        extra = f" +{bad - 1}" if bad > 1 else ""
        pen.text((R.MARGIN, y), f"SMART: {smart.get('worst') or '?'} {smart.get('errors') or 0} "
                 f"{s['errors']}{extra}", font=R.bold(18), fill=R.BLACK, anchor="lm")
    elif host.get("ok"):
        pen.text((R.MARGIN, y), f"{s['uptime']} {R._uptime(host['uptime'])}",
                 font=R.regular(18), fill=R.GREY, anchor="lm")
    pen.text((w - R.MARGIN, y), right, font=R.regular(18),
             fill=R.BLACK if stale else R.GREY, anchor="rm")


def _clock(pen, w, h, o, ctx):
    size = o.get("size") or min(h - 8, 96)
    pen.text((w // 2, h // 2), ctx["now"].strftime("%H:%M"), font=R.bold(size),
             fill=R.BLACK, anchor="mm")


def _text(pen, w, h, o, ctx):
    size = o.get("size") or 28
    font = R.bold(size) if o.get("bold") else R.regular(size)
    align = o.get("align", "center")
    x = {"left": R.MARGIN, "center": w // 2, "right": w - R.MARGIN}[align]
    anchor = {"left": "lm", "center": "mm", "right": "rm"}[align]
    lines = _fmt(o.get("text", ""), ctx).split("\n")
    gap = int(size * 1.25)
    y0 = h / 2 - gap * (len(lines) - 1) / 2
    for i, line in enumerate(lines):
        pen.text((x, y0 + i * gap), line, font=font, fill=R.BLACK, anchor=anchor)


_DRAW = {
    "header": _header, "kuma": _tile(R._kuma_tile, "kuma"),
    "media": _tile(R._media_tile, "media"), "host": _tile(R._host_tile, "host"),
    "weather_today": _weather_today, "weather_days": _weather_days,
    "footer": _footer, "clock": _clock, "text": _text,
    "spacer": lambda *a: None,
}


def render_layout(state, layout):
    """state + layout -> PNG bytes."""
    t = i18n.catalogue(state.get("lang", i18n.DEFAULT))
    ctx = {"state": state, "t": t, "s": t["s"], "now": state.get("now") or datetime.now()}
    img = Image.new("L", (W, H), R.WHITE)
    y = 0
    for row in layout["rows"]:
        h = row["height"]
        weights = [c["w"] for c in row["cells"]]
        x, acc = 0, 0
        for i, cell in enumerate(row["cells"]):
            acc += weights[i]
            x2 = round(W * acc / sum(weights))
            w = x2 - x
            tile = Image.new("L", (w, h), R.WHITE)  # own canvas: clips overflow
            try:
                _DRAW[cell["widget"]](_Pen(ImageDraw.Draw(tile), 0, tile), w, h, cell, ctx)
            except Exception as exc:  # one broken widget must not blank the panel
                log.warning("widget %s failed: %s", cell["widget"], exc)
            img.paste(tile, (x, y))
            if row.get("divider") and i:
                d = ImageDraw.Draw(img)
                d.line([x, y + 24, x, y + h - 24], fill=R.LIGHT, width=2)
            x = x2
        y += h
        if row["rule"] != "none":
            d = ImageDraw.Draw(img)
            d.line([R.MARGIN, y - 1, W - R.MARGIN, y - 1],
                   fill=R.BLACK if row["rule"] == "dark" else R.LIGHT, width=2)
    img = img.point(lambda p: (p // 17) * 17)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_current(state):
    """What /dash.png calls: the saved layout, else the original renderer."""
    layout = load()
    return render_layout(state, layout) if layout else R.render(state)


def screen_size():
    """DASHINK_SCREEN=1072x1448 -> (1072, 1448), or None for the native 600x800."""
    spec = os.environ.get("DASHINK_SCREEN", "").lower().replace(" ", "")
    try:
        w, h = (int(v) for v in spec.split("x"))
    except ValueError:
        return None
    if (w, h) == (W, H) or not (100 <= w <= 4000 and 100 <= h <= 4000):
        return None
    return w, h


def fit_to_screen(png):
    size = screen_size()
    if not size:
        return png
    sw, sh = size
    img = Image.open(io.BytesIO(png)).convert("L")
    scale = min(sw / img.width, sh / img.height)
    img = img.resize((round(img.width * scale), round(img.height * scale)), Image.BICUBIC)
    canvas = Image.new("L", size, R.WHITE)
    canvas.paste(img, ((sw - img.width) // 2, (sh - img.height) // 2))
    canvas = canvas.point(lambda p: (p // 17) * 17)
    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
