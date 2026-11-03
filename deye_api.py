"""Thin client for the DeyeCloud OpenAPI (https://developer.deyecloud.com/api)."""
import hashlib
import logging
import time
from datetime import date, timedelta

import requests

log = logging.getLogger(__name__)

BASE_URLS = {
    "EU": "https://eu1-developer.deyecloud.com/v1.0",
    "US": "https://us1-developer.deyecloud.com/v1.0",
    "INDIA": "https://india-developer.deyecloud.com/v1.0",
}


class DeyeError(Exception):
    pass


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _num(d, *keys):
    """First numeric value found under any of `keys`, else None."""
    for k in keys:
        v = d.get(k)
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    return None


class DeyeClient:
    def __init__(self, region, app_id, app_secret, email, password_sha256):
        self.base = BASE_URLS.get(region, BASE_URLS["EU"])
        self.app_id = app_id
        self.app_secret = app_secret
        self.email = email
        self.password_sha256 = password_sha256
        self.token = None
        self._device_sn = None
        self._hist = None
        self.session = requests.Session()  # reuse the TLS connection between polls

    # --- transport -------------------------------------------------------
    def login(self):
        r = self.session.post(
            f"{self.base}/account/token",
            params={"appId": self.app_id},
            json={"appSecret": self.app_secret, "email": self.email,
                  "password": self.password_sha256},
            timeout=20,
        )
        r.raise_for_status()
        body = r.json()
        token = body.get("accessToken") or (body.get("data") or {}).get("accessToken")
        if not token:
            raise DeyeError(body.get("msg") or "Login failed")
        self.token = token

    def post(self, path, payload, _retry=True):
        if not self.token:
            self.login()
        r = self.session.post(
            f"{self.base}{path}", json=payload, timeout=20,
            headers={"Authorization": f"bearer {self.token}"},
        )
        if r.status_code == 401 and _retry:
            self.token = None
            return self.post(path, payload, _retry=False)
        r.raise_for_status()
        body = r.json()
        if body.get("success") is False:
            if _retry and "token" in str(body.get("msg", "")).lower():
                self.token = None
                return self.post(path, payload, _retry=False)
            raise DeyeError(body.get("msg") or f"{path} failed")
        return body

    # --- raw endpoints ---------------------------------------------------
    def station_list(self):
        return self.post("/station/list", {"page": 1, "size": 20})

    def station_latest(self, station_id):
        return self.post("/station/latest", {"stationId": int(station_id)})

    def station_history(self, station_id, granularity, start, end=None):
        payload = {"stationId": int(station_id), "granularity": granularity, "startAt": start}
        if end:
            payload["endAt"] = end
        return self.post("/station/history", payload)

    def station_devices(self, station_id):
        return self.post("/station/device", {"page": 1, "size": 20, "stationIds": [int(station_id)]})

    def device_latest(self, sns):
        return self.post("/device/latest", {"deviceList": list(sns)})

    # --- normalised snapshot for the widget ------------------------------
    HISTORY_TTL = 300  # daily totals and curve only change every ~5 min; don't spend quota on them every poll

    def _inverter(self, station_id):
        """Live inverter reading as {'t', 'pv', 'load', 'grid', 'battery', 'soc', 'gridVoltage'}, or None."""
        try:
            if self._device_sn is None:
                devs = self.station_devices(station_id)
                self._device_sn = ""
                for st in devs.get("deviceListItems") or devs.get("stationDeviceList") or []:
                    if "INVERTER" in str(st.get("deviceType", "")).upper():
                        self._device_sn = st.get("deviceSn", "")
                        break
            if not self._device_sn:
                return None
            dev = (self.device_latest([self._device_sn]).get("deviceDataList") or [{}])[0]
            vals = {str(i.get("key", "")): i.get("value") for i in dev.get("dataList") or []}
            t = _num(dev, "collectionTime")
            if t and t > 1e12:
                t /= 1000
            volt = _num(vals, "gridVoltageL1l2", "gridVoltageL1", "gridVoltage")
            if volt is None:
                for k, v in vals.items():
                    if "grid" in k.lower() and "voltage" in k.lower():
                        volt = _num({k: v}, k)
                        break
            return {"t": t, "pv": _num(vals, "totalDcInputPower"), "load": _num(vals, "totalConsumptionPower", "upsLoadPower"),
                    "grid": _num(vals, "totalGridPower"), "battery": _num(vals, "batteryPower"),
                    "soc": _num(vals, "soc", "bmsSoc"), "gridVoltage": volt}
        except Exception as e:
            log.warning("inverter reading failed: %s", e)
            return None

    @staticmethod
    def _station_reading(latest):
        buy, sell, g = _num(latest, "purchasePower"), _num(latest, "wirePower"), _num(latest, "gridPower")
        chg, dis = _num(latest, "chargePower"), _num(latest, "dischargePower")
        t = _num(latest, "lastUpdateTime")
        if t and t > 1e12:  # ms -> s
            t /= 1000
        return {"t": t, "pv": _num(latest, "generationPower"), "load": _num(latest, "consumptionPower"),
                "grid": g if g is not None else (buy if buy is not None else sell),
                "battery": (dis or 0) - (chg or 0) if (chg is not None or dis is not None) else _num(latest, "batteryPower"),
                "soc": _num(latest, "batterySOC")}

    def _history(self, station_id):
        today = date.today()
        tomorrow = (today + timedelta(days=1)).isoformat()
        totals, curve, ok = {}, [], True
        try:
            items = self.station_history(station_id, 2, today.isoformat(), tomorrow).get("stationDataItems") or []
            if items:
                it = items[-1]
                totals = {
                    "generated": _num(it, "generationValue"),
                    "consumed": _num(it, "consumptionValue"),
                    "imported": _num(it, "purchaseValue"),
                    "exported": _num(it, "gridValue"),
                    "charged": _num(it, "chargeValue"),
                    "discharged": _num(it, "dischargeValue"),
                }
        except Exception as e:
            log.warning("daily totals failed: %s", e)
            ok = False
        try:  # granularity 1 (frame) needs an explicit end date or the API answers "invalid param"
            for it in self.station_history(station_id, 1, today.isoformat(), tomorrow).get("stationDataItems") or []:
                ts = _num(it, "timeStamp")
                if ts and ts > 1e12:
                    ts /= 1000
                if ts:
                    curve.append({"t": ts, "pv": _num(it, "generationPower") or 0,
                                  "load": _num(it, "consumptionPower") or 0, "soc": _num(it, "batterySOC")})
        except Exception as e:
            log.warning("day curve failed: %s", e)
            ok = False
        return totals, curve, ok

    def snapshot(self, station_id):
        """Newest of station/latest and inverter device/latest. The two clouds endpoints update independently,
        and the DeyeCloud site can show either, so take whichever reading is more recent."""
        station, station_error = None, None
        try:
            station = self._station_reading(self.station_latest(station_id))
        except Exception as e:  # fall back to the inverter reading alone, if it answers
            station_error = e
        inv = self._inverter(station_id)
        if station is None:
            if inv is None:
                raise station_error
            log.warning("station/latest failed, using inverter data only: %s", station_error)
            station = {"t": None}
        newest, other = (inv, station) if inv and (inv["t"] or 0) >= (station["t"] or 0) else (station, inv or {})
        pick = lambda k: newest.get(k) if newest.get(k) is not None else other.get(k)

        day = date.today()
        if self._hist is None or self._hist[0] != day or time.time() - self._hist[1] > self.HISTORY_TTL:
            totals, curve, ok = self._history(station_id)
            if ok:
                self._hist = (day, time.time(), totals, curve)
            elif self._hist and self._hist[0] == day:
                self._hist = (day, 0, *self._hist[2:])  # keep what we had; retry on the next poll
            else:
                self._hist = (day, 0, totals, curve)
        _, _, totals, curve = self._hist

        return {
            "pv": pick("pv") or 0.0, "load": pick("load") or 0.0, "grid": pick("grid") or 0.0,
            "battery": pick("battery") or 0.0, "soc": pick("soc"),
            "updated": newest.get("t") or time.time(),
            "source": "device" if newest is inv else "station",
            "gridVoltage": (inv or {}).get("gridVoltage"),
            "today": totals, "curve": curve,
        }
