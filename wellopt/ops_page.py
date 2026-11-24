"""Operations support page: what to do with each pump this hour, and where each pump runs on its curve.

It reads the newest plan per section from the plan store (written by `python -m wellopt live` or
`python -m wellopt plan`) and the readings the plan was made from. Picking a pump in the list shows it
on its pump curve: the maker curve when config/pump_curves.csv has one, the hours it has run, its usual
range, and where it is now. A pump can be marked out of service here: the mark is kept in the store, the
section is re-planned at once, and later planning runs leave the pump out until it is returned to service.

The page itself is plain HTML, CSS and JavaScript in wellopt/ops_static/, and loads nothing from the
internet. Flask serves it with two JSON routes around OpsData:

    GET  /api/views    the newest plan per section, as the page shows it
    POST /api/marks    {"pump": "PMP_3", "out_of_service": true, "note": "bearing noise"}

    python ops_app.py                         http://127.0.0.1:8051
    python -m wellopt.ops_page --port 8051    (--store and --history point it at other files)
"""
from __future__ import annotations

import argparse
import json
import math
import mimetypes
from datetime import datetime
from pathlib import Path

import numpy as np
from flask import Flask, Response, request

from .config import SECTIONS, CurvesConfig, SiteConfig, load_config
from .ops_view import OpsData

STATIC = Path(__file__).with_name("ops_static")
NOTE_MAX = 200         # characters in an out-of-service reason
REFRESH_MINUTES = 5    # the page reloads the plan this often
# Everything the page uses comes from this server. Operator notes are shown as text; this also stops an
# injected tag from running if one ever slipped through.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'")


# ----------------------------------------------------------------------------- what the page is sent
def axis_range(values) -> list[float]:
    """An axis range around `values` with a 12% margin, never below zero."""
    lo, hi = min(values), max(values)
    span = max(hi - lo, 0.25 * abs(hi), 1.0)
    return [max(0.0, lo - 0.12 * span), hi + 0.12 * span]


def chart_frame(pv: dict, settings: CurvesConfig | None = None) -> dict:
    """Where the pump curve chart looks: {"x": [lo, hi], "y": [lo, hi], "band", "bep"}.

    The axes frame the preferred range, the pump's usual running hours, the best efficiency point, the
    reading now and the point expected if it is started, not the whole maker curve, so the running point is
    not squeezed into a corner; the maker curve carries on past the frame. A maker curve with nothing else
    to frame (no history and no efficiencies) is shown whole. x and y are None when there is nothing to
    draw. band is the preferred range in gpm and bep the best efficiency point [gpm, ft], when known."""
    s = settings or CurvesConfig()
    env, maker = pv.get("env"), pv.get("maker")
    xs: list[float] = []  # flows and heads the axes must show
    ys: list[float] = []
    band = bep = None
    if maker and maker.get("bep"):
        band = [maker["bep"] * s.por_low_pct / 100, maker["bep"] * s.por_high_pct / 100]
        bep = [maker["bep"], float(np.interp(maker["bep"], maker["flow"], maker["head"]))]
        xs += [*band, bep[0]]
        ys.append(bep[1])
    if env:
        xs += [env["flow"]["p1"], env["flow"]["p99"]]
        ys += [env["head"]["p1"], env["head"]["p99"]]
    if pv.get("flow") is not None and pv.get("head") is not None:
        xs.append(pv["flow"])
        ys.append(pv["head"])
    if pv.get("expected") and None not in pv["expected"]:
        xs.append(pv["expected"][0])
        ys.append(pv["expected"][1])
    if maker:
        if not xs:  # no history and no best-efficiency point: show the whole curve
            xs += maker["flow"]
        # keep the maker curve in view across the framed flows
        q = np.linspace(*axis_range(xs), 60)
        mq, mh = np.asarray(maker["flow"], float), np.asarray(maker["head"], float)
        ys += np.interp(q[(q >= mq[0]) & (q <= mq[-1])], mq, mh).tolist()
    if not xs or not ys:
        return {"x": None, "y": None, "band": band, "bep": bep}
    return {"x": axis_range(xs), "y": axis_range(ys), "band": band, "bep": bep}


def plan_age(created_at: str | None, now: datetime | None = None) -> tuple[str, float]:
    """('35 min ago', minutes) for a store timestamp."""
    if not created_at:
        return "time unknown", float("nan")
    minutes = ((now or datetime.now()) - datetime.fromisoformat(str(created_at))).total_seconds() / 60
    if minutes < 90:
        return f"{max(minutes, 0):.0f} min ago", minutes
    if minutes < 48 * 60:
        return f"{minutes / 60:.0f} h ago", minutes
    return f"{minutes / 1440:.0f} days ago", minutes


def _plain(v):
    """A JSON-ready copy: numpy values as Python numbers and lists, NaN and infinity as null, times as text."""
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, np.ndarray)):
        return [_plain(x) for x in v]
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    return v


def page_views(data: OpsData, cfg: SiteConfig, now: datetime | None = None) -> dict:
    """What GET /api/views returns: OpsData.views() with each plan's age, each pump's chart frame, the
    section tabs and the page's settings."""
    now = now or datetime.now()
    try:
        views = data.views()
    except Exception as exc:  # a locked or damaged store: say so instead of a blank page
        views = {"sections": {}, "message": f"The plan store could not be read ({type(exc).__name__}: {exc})."}
    stale_after = cfg.live.stale_after_minutes
    sections = {}
    for sec, view in views["sections"].items():
        age, minutes = plan_age(view.get("created_at"), now)
        sections[sec] = dict(view, age=age, stale=bool(minutes > stale_after),
                             rows=[dict(r, chart=chart_frame(r, cfg.curves)) for r in view["rows"]])
    return _plain({
        "sections": sections,
        "section_names": list(SECTIONS) + [s for s in sections if s not in SECTIONS],
        "message": views.get("message"),
        "loaded_at": now.isoformat(sep=" ", timespec="seconds"),
        "settings": {"por_low_pct": cfg.curves.por_low_pct, "por_high_pct": cfg.curves.por_high_pct,
                     "stale_after_minutes": stale_after, "refresh_minutes": REFRESH_MINUTES, "note_max": NOTE_MAX},
    })


def _find(views: dict, pump: str) -> dict | None:
    """The pump's row in the newest plan that lists it."""
    return next((r for view in views["sections"].values() for r in view["rows"] if r["pump"] == pump), None)


def _json(payload, status: int = 200) -> Response:
    return Response(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), status=status,
                    mimetype="application/json")


def _error(message: str, status: int) -> Response:
    return _json({"error": message}, status)


# ----------------------------------------------------------------------------- app
def create_app(config_path: str | Path | None = None, *, store_path: str | Path | None = None,
               history_path: str | Path | None = None) -> Flask:
    """Build the page. Paths default to the site config's plan store and history CSV."""
    cfg = load_config(config_path)
    data = OpsData(cfg, store_path=store_path, history_path=history_path)
    # The Windows registry can map .js to text/plain, and with nosniff a browser then refuses to run the script.
    for kind, ext in (("text/javascript", ".js"), ("text/css", ".css"), ("font/woff2", ".woff2")):
        mimetypes.add_type(kind, ext)
    app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # a mark is a few hundred bytes
    app.extensions["wellopt_ops"] = data

    @app.after_request
    def headers(resp: Response) -> Response:
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(413)
    def too_large(_exc):
        return _error("The mark is too large.", 413)

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/views")
    def views():
        return _json(page_views(data, cfg))

    @app.post("/api/marks")
    def mark():
        # JSON only: a form on another site cannot send it without the browser asking this server first, and
        # this server never says yes. Browsers also say when a request comes from another site.
        if not request.is_json:
            return _error("Send the mark as JSON.", 415)
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return _error("Pumps can only be marked from this page.", 403)
        body = request.get_json(silent=True)
        pump = body.get("pump") if isinstance(body, dict) else None
        out_of_service = body.get("out_of_service") if isinstance(body, dict) else None
        note = (body.get("note") or "") if isinstance(body, dict) else ""
        if not isinstance(pump, str) or not pump or not isinstance(out_of_service, bool) or not isinstance(note, str):
            return _error('Send {"pump": "PMP_3", "out_of_service": true, "note": "reason"}.', 400)
        if len(note) > NOTE_MAX:
            return _error(f"Keep the reason to {NOTE_MAX} characters.", 400)
        try:
            pv = _find(data.views(), pump)
        except Exception as exc:
            return _error(f"The plan store could not be read ({type(exc).__name__}: {exc}).", 503)
        if pv is None:
            return _error(f"{pump[:40]} is not in the newest plan; refresh the page.", 409)
        if out_of_service and pv["oos"] and not pv.get("mark"):
            return _error(f"{pump} cannot be planned ({pv['why']})", 409)
        if not out_of_service and not pv.get("mark"):
            return _error(f"{pump} is not marked out of service; refresh the page.", 409)
        try:
            result = data.mark(pump, out_of_service, note)
        except Exception as exc:
            return _error(f"The mark was not saved: {type(exc).__name__}: {exc}", 500)
        return _json({"message": result.message, "pump": result.pump, "out_of_service": result.out_of_service,
                      "section": result.section, "replanned": result.replanned, "views": page_views(data, cfg)})

    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wellopt.ops_page", description="Run the operations support page.")
    ap.add_argument("--config", help="site config (default config/site.toml)")
    ap.add_argument("--store", help="plan store (default: [live] store_path in the config)")
    ap.add_argument("--history", help="history CSV (default: [site] history_csv in the config)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8051)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)
    create_app(args.config, store_path=args.store, history_path=args.history).run(host=args.host, port=args.port,
                                                                                  debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
