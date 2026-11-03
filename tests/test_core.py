"""Run from the project folder: python -m unittest discover tests"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

import app  # noqa: E402
import config  # noqa: E402
from deye_api import DeyeClient, DeyeError  # noqa: E402


class StubClient(DeyeClient):
    """DeyeClient with the network replaced by canned readings."""

    def __init__(self, station, inverter, history_ok=True):
        super().__init__("EU", "id", "secret", "a@b.c", "pw")
        self._station, self._inv, self._history_ok = station, inverter, history_ok

    def station_latest(self, _):
        if isinstance(self._station, Exception):
            raise self._station
        return self._station

    def _inverter(self, _):
        return self._inv

    def _history(self, _):
        return ({"generated": 1.0}, [{"t": 1, "pv": 0, "load": 0, "soc": 50}], self._history_ok)


def inv(t, **kw):
    return {"t": t, "pv": 0, "load": 0, "grid": 0, "battery": 0, "soc": None, "gridVoltage": 230.0, **kw}


class Snapshot(unittest.TestCase):
    def test_newest_reading_wins(self):
        station = {"lastUpdateTime": 1000, "consumptionPower": 100, "generationPower": 0, "batterySOC": 50}
        snap = StubClient(station, inv(1300, load=250, soc=48)).snapshot("1")
        self.assertEqual((snap["load"], snap["soc"], snap["updated"], snap["source"]), (250, 48, 1300, "device"))
        snap = StubClient(station, inv(900, load=250)).snapshot("1")
        self.assertEqual((snap["load"], snap["updated"], snap["source"]), (100, 1000, "station"))

    def test_millisecond_timestamps_are_normalised(self):
        snap = StubClient({"lastUpdateTime": 1_700_000_000_000, "consumptionPower": 1}, None).snapshot("1")
        self.assertEqual(snap["updated"], 1_700_000_000)

    def test_missing_fields_come_from_the_other_source(self):
        station = {"lastUpdateTime": 2000, "consumptionPower": 100, "batterySOC": 61}
        snap = StubClient(station, inv(1000, soc=None)).snapshot("1")
        self.assertEqual(snap["soc"], 61)

    def test_station_outage_falls_back_to_inverter(self):
        snap = StubClient(requests.ConnectionError("down"), inv(1500, load=300)).snapshot("1")
        self.assertEqual((snap["load"], snap["updated"]), (300, 1500))

    def test_both_sources_down_raises(self):
        with self.assertRaises(requests.ConnectionError):
            StubClient(requests.ConnectionError("down"), None).snapshot("1")

    def test_failed_history_is_retried_not_cached(self):
        c = StubClient({"lastUpdateTime": 1, "consumptionPower": 1}, None, history_ok=False)
        c.snapshot("1")
        self.assertEqual(c._hist[1], 0)  # timestamp 0 => stale => refetched on the next poll
        c._history_ok = True
        c.snapshot("1")
        self.assertGreater(c._hist[1], 0)


class Outage(unittest.TestCase):
    def check(self, snap):
        a = app.Api.__new__(app.Api)
        a.grid_down = False
        with mock.patch.object(app, "toast") as toast:
            a._check_outage(snap)
        return snap["gridDown"], toast.call_count

    def test_zero_voltage_is_an_outage(self):
        self.assertEqual(self.check({"gridVoltage": 0.0, "grid": 0, "battery": 0, "load": 0}), (True, 1))

    def test_normal_voltage_is_fine(self):
        self.assertEqual(self.check({"gridVoltage": 231.0, "grid": 0, "battery": 300, "load": 300}), (False, 0))

    def test_fallback_heuristic_without_voltage(self):
        self.assertEqual(self.check({"gridVoltage": None, "grid": 0, "battery": 300, "load": 300})[0], True)


class Settings(unittest.TestCase):
    def load_from(self, text):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "settings.json"
            if text is not None:
                f.write_text(text, "utf-8")
            with mock.patch.object(config, "FILE", f):
                return config.load()

    def test_missing_and_corrupt_files_give_defaults(self):
        self.assertEqual(self.load_from(None), self.load_from("{not json"))
        self.assertEqual(self.load_from("[1, 2]")["width"], config.DEFAULTS["width"])

    def test_values_are_clamped(self):
        s = self.load_from(json.dumps({"refreshSeconds": 1, "width": 10, "height": "x", "x": "abc"}))
        self.assertEqual((s["refreshSeconds"], s["width"], s["height"], s["x"]),
                         (config.MIN_REFRESH, config.MIN_SIZE[0], config.DEFAULTS["height"], None))

    def test_save_is_atomic_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "sub" / "settings.json"
            with mock.patch.object(config, "FILE", f), mock.patch.object(config, "DIR", f.parent):
                config.save({**config.DEFAULTS, "width": 400})
                self.assertEqual(config.load()["width"], 400)
                self.assertFalse(f.with_suffix(".tmp").exists())


class Errors(unittest.TestCase):
    def test_short_error_labels(self):
        self.assertEqual(app.short_error(requests.ConnectTimeout()), "Timed out")
        self.assertEqual(app.short_error(requests.ConnectionError()), "Offline")
        self.assertEqual(app.short_error(DeyeError("auth invalid")), "auth invalid")
        self.assertEqual(app.short_error(ValueError("x")), "Error")


if __name__ == "__main__":
    unittest.main()
