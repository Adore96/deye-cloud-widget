"""Settings in %APPDATA%\\DeyeWidget\\settings.json; secrets in Windows Credential Manager."""
import json
import os
import threading
from pathlib import Path

import keyring

SERVICE = "DeyeWidget"
DIR = Path(os.environ.get("APPDATA", Path.home())) / "DeyeWidget"
FILE = DIR / "settings.json"
MIN_REFRESH = 30  # seconds; faster polling only burns API quota, the inverter uploads every ~5 min
MIN_SIZE = (300, 260)

DEFAULTS = {
    "region": "EU",
    "appId": "",
    "email": "",
    "stationId": "",
    "refreshSeconds": 60,
    "width": 340,
    "height": 500,
    "x": None,
    "y": None,
    "onTop": True,
}

_lock = threading.Lock()


def _int(value, default, lo=None, hi=None):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _sanitise(s):
    s["refreshSeconds"] = _int(s["refreshSeconds"], DEFAULTS["refreshSeconds"], MIN_REFRESH, 3600)
    s["width"] = _int(s["width"], DEFAULTS["width"], MIN_SIZE[0], 4000)
    s["height"] = _int(s["height"], DEFAULTS["height"], MIN_SIZE[1], 4000)
    s["x"] = _int(s["x"], None)
    s["y"] = _int(s["y"], None)
    s["onTop"] = bool(s["onTop"])
    s["stationId"] = str(s["stationId"] or "").strip()
    return s


def load():
    try:
        data = json.loads(FILE.read_text("utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    return _sanitise({**DEFAULTS, **data})


def save(settings):
    """Atomic write (temp file + replace) under a lock: the move/resize debounce timers and the UI all save."""
    with _lock:
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(settings), indent=2), "utf-8")
        os.replace(tmp, FILE)


def get_secret(name):
    return keyring.get_password(SERVICE, name)


def set_secret(name, value):
    keyring.set_password(SERVICE, name, value)


def is_configured(settings):
    return bool(settings["appId"] and settings["email"] and settings["stationId"]
                and get_secret("appSecret") and get_secret("passwordSha256"))
