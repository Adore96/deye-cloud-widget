"""DeyeCloud desktop widget. Run with `pythonw app.py`; `python app.py --dump` prints raw API JSON."""
import json
import logging
import logging.handlers
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import requests
import webview

import config
import winutil
from deye_api import DeyeClient, DeyeError, hash_password

HERE = Path(__file__).parent
MIN_SIZE = config.MIN_SIZE
TITLE = "Deye"
log = logging.getLogger("deyewidget")


def setup_logging():
    """Rotating log in %APPDATA%\\DeyeWidget. Under pythonw there is no console, so this is the only place errors show up."""
    try:
        config.DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(config.DIR / "widget.log", maxBytes=200_000, backupCount=2, encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    sys.excepthook = lambda t, v, tb: log.critical("uncaught", exc_info=(t, v, tb))
    threading.excepthook = lambda a: log.critical("uncaught in thread %s", a.thread and a.thread.name,
                                                  exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def short_error(e):
    """A label that fits the footer; the full text goes in the tooltip."""
    if isinstance(e, requests.Timeout):
        return "Timed out"
    if isinstance(e, requests.ConnectionError):
        return "Offline"
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code}"
    return str(e)[:24] if isinstance(e, DeyeError) else "Error"


def toast(title, message):
    """Native Windows toast via PowerShell, so no extra dependency is needed."""
    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace("'", "''")
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]>$null;"
        "$x=[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom,ContentType=WindowsRuntime]::new();"
        f"$x.LoadXml('<toast><visual><binding template=\"ToastGeneric\"><text>{esc(title)}</text>"
        f"<text>{esc(message)}</text></binding></visual></toast>');"
        "$n='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($n).Show("
        "[Windows.UI.Notifications.ToastNotification]::new($x))"
    )
    subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def make_client(s):
    return DeyeClient(s["region"], s["appId"], config.get_secret("appSecret"),
                      s["email"], config.get_secret("passwordSha256"))


class Api:
    """Methods exposed to the page as window.pywebview.api.*"""

    def __init__(self):
        self.settings = config.load()
        self.client = make_client(self.settings) if config.is_configured(self.settings) else None
        self._window = None
        self._hwnd = None
        self.grid_down = False
        self._lock = threading.Lock()  # one cloud call sequence at a time (timer, manual refresh and wake-up can overlap)

    def get_state(self):
        return {"configured": self.client is not None, "settings": {
            k: self.settings[k] for k in ("region", "appId", "email", "stationId", "onTop", "refreshSeconds")}}

    def fetch(self):
        if not self.client:
            return {"error": "Not configured"}
        with self._lock:
            try:
                snap = self.client.snapshot(self.settings["stationId"])
            except Exception as e:  # network down, bad creds, API change
                log.warning("fetch failed: %s", e, exc_info=not isinstance(e, requests.RequestException))
                return {"error": short_error(e), "detail": str(e)}
            self._check_outage(snap)
            return snap

    def _check_outage(self, snap):
        v = snap.get("gridVoltage")
        if v is not None:
            down = v < 50
        else:  # fallback heuristic: no grid flow while the house is drawing from the battery
            down = abs(snap["grid"]) < 5 and snap["battery"] > 50 and snap["load"] > 50
        if down and not self.grid_down:
            toast("Grid outage", "Grid power is gone. The house is running on battery/solar.")
        elif self.grid_down and not down:
            toast("Grid restored", "Grid power is back.")
        self.grid_down = down
        snap["gridDown"] = down

    def save_setup(self, form):
        s = dict(self.settings)
        s.update(region=form["region"], appId=form["appId"].strip(), email=form["email"].strip(),
                 stationId=str(form.get("stationId", "")).strip())
        secret = form["appSecret"].strip() or config.get_secret("appSecret")
        pw = hash_password(form["password"]) if form["password"] else config.get_secret("passwordSha256")
        client = DeyeClient(s["region"], s["appId"], secret, s["email"], pw)
        try:
            client.login()
            if not s["stationId"]:
                stations = client.station_list().get("stationList") or []
                if not stations:
                    return {"error": "Logged in, but no stations were found on this account."}
                s["stationId"] = str(stations[0]["id"])
            client.station_latest(s["stationId"])
        except Exception as e:
            log.warning("setup failed: %s", e)
            return {"error": f"Could not connect: {e}"}
        config.set_secret("appSecret", secret)
        config.set_secret("passwordSha256", pw)
        config.save(s)
        with self._lock:
            self.settings, self.client, self.grid_down = s, client, False
        return {"ok": True}

    def _hwnd_or_none(self):
        if self._hwnd is None:
            try:
                self._hwnd = int(self._window.native.Handle.ToInt64())
            except Exception:
                self._hwnd = winutil.user32.FindWindowW(None, TITLE) or None
        return self._hwnd

    def toggle_on_top(self):
        want = not self.settings["onTop"]
        hwnd = self._hwnd_or_none()
        if hwnd and winutil.set_topmost(hwnd, want):
            self.settings["onTop"] = want
            config.save(self.settings)
        else:
            log.error("could not change always-on-top")
        return self.settings["onTop"]

    def open_site(self):
        webbrowser.open(f"https://www.deyecloud.com/station/new-main?id={self.settings['stationId']}")

    def open_url(self, url):
        if url.startswith("https://"):
            webbrowser.open(url)

    def resize(self, width, height):
        hwnd = self._hwnd_or_none()
        if hwnd:
            winutil.resize(hwnd, max(MIN_SIZE[0], int(width)), max(MIN_SIZE[1], int(height)))

    def minimize(self):
        self._window.minimize()

    def close(self):
        self._window.destroy()


def dump():
    s = config.load()
    c = make_client(s)
    sid = s["stationId"]
    from datetime import date
    for name, fn in [("station/latest", lambda: c.station_latest(sid)),
                     ("station/history day", lambda: c.station_history(sid, 2, date.today().isoformat(),
                                                                       date.fromordinal(date.today().toordinal() + 1).isoformat())),
                     ("station/device", lambda: c.station_devices(sid)),
                     ("snapshot", lambda: c.snapshot(sid))]:
        try:
            print(f"== {name}\n{json.dumps(fn(), indent=2)[:4000]}")
        except Exception as e:
            print(f"== {name} FAILED: {e}")


class DemoClient:
    """Fake data for `--demo`, to preview the UI without API keys."""

    def snapshot(self, _sid):
        import math
        import random
        import time
        now = time.time()
        day0 = now - (now - time.timezone) % 86400
        curve = []
        for i in range(0, int((now - day0) / 300)):
            t = day0 + i * 300
            h = (t - day0) / 3600
            curve.append({"t": t, "pv": max(0, 4200 * math.sin(math.pi * (h - 6) / 12)) * random.uniform(.85, 1),
                          "load": 450 + 900 * math.exp(-((h - 19) ** 2) / 4) + random.uniform(0, 250),
                          "soc": min(100, 30 + max(0, h - 7) * 8) if h < 17 else max(20, 100 - (h - 17) * 14)})
        return {"pv": 3120, "load": 1240, "grid": -620, "battery": -1260, "soc": 76,
                "updated": now - 120, "gridVoltage": 231.0, "source": "device",
                "today": {"generated": 14.6, "consumed": 9.8, "imported": 1.2, "exported": 3.9},
                "curve": curve}


class SettingsSaver:
    """Debounced persistence of window position/size: dragging fires many events, and the last one must not be lost on exit."""

    def __init__(self, api):
        self.api, self.pending, self.timer, self.lock = api, {}, None, threading.Lock()

    def update(self, **values):
        with self.lock:
            self.pending.update(values)
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(0.8, self.flush)
            self.timer.daemon = True
            self.timer.start()

    def flush(self):
        with self.lock:
            values, self.pending = self.pending, {}
            if self.timer:
                self.timer.cancel()
                self.timer = None
        if values:
            self.api.settings.update(values)
            try:
                config.save(self.api.settings)
            except OSError as e:
                log.warning("could not save settings: %s", e)


def main():
    if "--dump" in sys.argv:
        return dump()
    demo = "--demo" in sys.argv
    setup_logging()
    if not demo and not winutil.acquire_single_instance("Local\\DeyeWidget.SingleInstance"):
        winutil.focus_window(TITLE)  # already running: show it instead of opening a second copy
        return
    api = Api()
    if demo:
        api.client = DemoClient()
    s = api.settings
    has_pos = s["x"] is not None and s["y"] is not None and winutil.is_on_screen(s["x"], s["y"])
    window = webview.create_window(
        TITLE, str(HERE / "web" / "index.html"), js_api=api,
        width=s["width"], height=s["height"], x=s["x"] if has_pos else None, y=s["y"] if has_pos else None,
        frameless=True, easy_drag=False, resizable=True, min_size=MIN_SIZE,
        on_top=s["onTop"], background_color="#0d1117",
    )
    api._window = window
    saver = SettingsSaver(api)

    def on_moved(x, y):
        if x > -30000 and y > -30000:  # Windows parks minimised windows at -32000
            saver.update(x=x, y=y)

    def on_resized(width, height):
        if width >= MIN_SIZE[0] and height >= MIN_SIZE[1]:
            saver.update(width=int(width), height=int(height))

    def set_paused(on):
        # WebView2 keeps animating a minimised window, so tell the page to stop
        try:
            window.evaluate_js(f"window.setPaused && setPaused({'true' if on else 'false'})")
        except Exception as e:
            log.debug("pause toggle failed: %s", e)

    window.events.moved += on_moved
    window.events.resized += on_resized
    window.events.minimized += lambda: set_paused(True)
    window.events.restored += lambda: set_paused(False)
    try:
        webview.start(private_mode=False, storage_path=str(config.DIR / "webview"))  # keeps the chart-mode choice
    finally:
        saver.flush()


if __name__ == "__main__":
    main()
