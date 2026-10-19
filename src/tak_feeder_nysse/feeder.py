# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright Stefan Gofferje (tak-feeder-nysse, trakbridge-plugin-nysse)
# Copyright (C) 2026 Street3r (changes, see README)
"""Nysse (Tampere regional transport) Journeys API -> CoT."""

from __future__ import annotations

import fnmatch
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from . import __version__
from .common import (Health, SendPolicy, SendSchedule, as_float, cot_event, env, env_float,
                     fetch, remarks_text)

LOG = logging.getLogger("tak_feeder_nysse")

API_URL = "https://data.itsfactory.fi/journeys/api/1"
BUS_TYPE = "a-f-G-E-V-C-M-H"
STOPS_INTERVAL = 24 * 3600
MIN_FETCH_INTERVAL = 3.0  # the API has no documented limit; Eetu's choice 2026-10-18
MAX_AGE = 120.0  # vehicles missing from the API this long are dropped
_DURATION = re.compile(
    r"^(-)?P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?$"
)


@dataclass
class Settings:
    """Feeder settings from the environment (see .env.example)."""

    line_filter: str = ""
    api_url: str = API_URL
    send: SendPolicy = field(default_factory=SendPolicy)
    fetch_interval: float = 0.0  # 0 = SEND_MOVING_INTERVAL, at least MIN_FETCH_INTERVAL
    timezone: str = "Europe/Helsinki"
    source_line: str = "Source: Journeys API (ITS Factory, City of Tampere), CC BY 4.0"

    @classmethod
    def from_env(cls) -> "Settings":
        """Read settings."""
        d = cls()
        return cls(
            line_filter=env("NYSSE_LINE_FILTER", "", "NYSSE_LINE_REF"),
            api_url=env("NYSSE_API_URL", d.api_url).rstrip("/"),
            send=SendPolicy.from_env(),
            fetch_interval=env_float("FETCH_INTERVAL", d.fetch_interval),
            timezone=env("TIMEZONE", d.timezone),
            source_line=env("SOURCE_LINE", d.source_line),
        )

    def api_interval(self) -> float:
        """Seconds between API requests: FETCH_INTERVAL, or the moving send
        interval, never below MIN_FETCH_INTERVAL."""
        return max(MIN_FETCH_INTERVAL, self.fetch_interval or self.send.moving)


def patterns(line_filter: str) -> list[str]:
    """Line filter patterns."""
    return [p.strip() for p in line_filter.split(",") if p.strip()]


def has_wildcards(pats: list[str]) -> bool:
    """True if any pattern needs local matching."""
    return any(ch in p for p in pats for ch in "*?[")


def matches(line: str, pats: list[str]) -> bool:
    """Line matches the filter (empty filter = all lines)."""
    return not pats or any(fnmatch.fnmatch(line, p) for p in pats)


def iso_duration_seconds(value: Any) -> Optional[float]:
    """ISO 8601 duration such as -P0Y0M0DT0H0M36.000S -> -36.0 (None if invalid)."""
    m = _DURATION.match(str(value or ""))
    if not m:
        return None
    sign, years, months, days, hours, minutes, seconds = m.groups()
    if years and int(years) or months and int(months):
        return None
    total = (int(days or 0) * 86400 + int(hours or 0) * 3600 + int(minutes or 0) * 60
             + float(seconds or 0))
    return -total if sign else total


def ref_id(ref: Any) -> str:
    """Stop id from a stop-point URL or a plain id."""
    return str(ref or "").rstrip("/").rsplit("/", 1)[-1]


class NysseSource:
    """Polls the vehicle-activity API every api_interval() seconds and sends the
    latest state of each vehicle by the send policy."""

    def __init__(self, settings: Settings, health: Optional[Health] = None) -> None:
        self.s = settings
        self.health = health
        self.pats = patterns(settings.line_filter)
        self.tz = ZoneInfo(settings.timezone)
        self.headers = {"User-Agent": f"tak-feeder-nysse/{__version__}"}
        self.stops: dict[str, str] = {}
        self.stops_at = 0.0
        self.vehicles: dict[str, tuple[dict[str, Any], float]] = {}  # uid -> (journey, seen)
        self.fetched_at: Optional[float] = None
        self.schedule = SendSchedule(settings.send)

    # ---------------------------------------------------------------- API
    async def _get_json(self, url: str) -> Any:
        return json.loads(await fetch(url, self.headers, timeout=15))

    async def refresh_stops(self) -> None:
        """All stop names in one request, refreshed daily."""
        try:
            data = await self._get_json(f"{self.s.api_url}/stop-points")
        except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
            LOG.warning("Nysse stop list failed: %s", exc)
            return
        stops = {}
        for item in data.get("body", []) if isinstance(data, dict) else []:
            name = item.get("name")
            if name and item.get("shortName"):
                stops[str(item["shortName"])] = str(name)
        if stops:
            self.stops = stops
            self.stops_at = time.monotonic()
            LOG.info("Nysse: %d stop names", len(stops))

    def activity_url(self) -> str:
        """vehicle-activity, filtered on the server only for exact line numbers."""
        url = f"{self.s.api_url}/vehicle-activity?exclude-fields=recordedAtTime"
        if self.pats and not has_wildcards(self.pats):
            url += "&lineRef=" + quote(",".join(self.pats), safe=",")
        return url

    # -------------------------------------------------------------- output
    def _time(self, value: Any) -> Optional[str]:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
                self.tz).strftime("%H:%M")
        except ValueError:
            return None

    @staticmethod
    def vehicle_uid(mvj: dict[str, Any]) -> Optional[str]:
        """UID of a journey's vehicle (None without a vehicle reference)."""
        vehicle = mvj.get("vehicleRef")
        return None if vehicle is None else f"nysse-{vehicle}"

    @staticmethod
    def speed(mvj: dict[str, Any]) -> float:
        """Speed in m/s (the API gives km/h)."""
        return (as_float(mvj.get("speed"), 0.0) or 0.0) / 3.6

    def vehicle_event(self, mvj: dict[str, Any]) -> Optional[bytes]:
        """CoT for one vehicle, or None if it has no position or is filtered out."""
        vehicle, line = mvj.get("vehicleRef"), mvj.get("lineRef")
        loc = mvj.get("vehicleLocation") or {}
        lat, lon = as_float(loc.get("latitude")), as_float(loc.get("longitude"))
        if vehicle is None or line is None or lat is None or lon is None:
            return None
        line = str(line)
        if not matches(line, self.pats):
            return None
        uid = f"nysse-{vehicle}"

        dest = self.stops.get(ref_id(mvj.get("destinationShortName")))
        next_line = None
        calls = mvj.get("onwardCalls") or []
        if calls and isinstance(calls[0], dict):
            call = calls[0]
            name = self.stops.get(ref_id(call.get("stopPointRef")))
            at = self._time(call.get("expectedArrivalTime") or call.get("expectedDepartureTime"))
            if name:
                next_line = f"Next: {name} {at}" if at else f"Next: {name}"
        delay = iso_duration_seconds(mvj.get("delay"))
        delay_line = None
        if delay is not None:
            minutes = round(delay / 60)
            delay_line = f"Delay: {minutes:+d} min" if minutes else "On time"

        lines = [f"Line: {line}", f"To: {dest}" if dest else None, next_line, delay_line,
                 f"Vehicle: {vehicle}"]
        return cot_event(
            uid=uid,
            cot_type=BUS_TYPE,
            lat=lat, lon=lon,
            callsign=f"NYSSE {line}",
            remarks=remarks_text(lines, self.s.source_line, ("NYSSE", "bus")),
            stale=self.s.send.stale_for(self.s.send.interval(self.speed(mvj))),
            course=as_float(mvj.get("bearing"), 0.0),
            speed=self.speed(mvj),
        )

    async def poll(self, now: float) -> None:
        """Fetch the vehicle activity and update the cache. Vehicles missing
        from a successful answer are dropped, so they stale out in ATAK."""
        if not self.stops or now - self.stops_at > STOPS_INTERVAL:
            await self.refresh_stops()
        try:
            data = await self._get_json(self.activity_url())
        except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
            LOG.warning("Nysse vehicle activity failed: %s", exc)
            if self.health is not None:
                self.health.error()
            return
        seen = {}
        for activity in data.get("body", []) if isinstance(data, dict) else []:
            mvj = activity.get("monitoredVehicleJourney") if isinstance(activity, dict) else None
            uid = self.vehicle_uid(mvj) if isinstance(mvj, dict) else None
            if uid is not None:
                seen[uid] = (mvj, now)
        self.vehicles = seen
        self.schedule.keep_only(seen)
        if self.health is not None:
            self.health.ok()

    async def events(self, now: Optional[float] = None) -> list[bytes]:
        """Source protocol: poll when api_interval() has passed, then send the
        vehicles that are due."""
        now = time.monotonic() if now is None else now
        if self.fetched_at is None or now - self.fetched_at >= self.s.api_interval() - 0.2:
            self.fetched_at = now
            await self.poll(now)
        events = []
        for uid, (mvj, seen) in list(self.vehicles.items()):
            if now - seen > MAX_AGE:
                del self.vehicles[uid]
                self.schedule.forget(uid)
                continue
            speed = self.speed(mvj)
            if not self.schedule.due(uid, speed, now):
                continue
            try:
                event = self.vehicle_event(mvj)
            except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
                LOG.debug("Nysse: skipped vehicle %s: %s", mvj.get("vehicleRef"), exc)
                continue
            self.schedule.mark(uid, speed, now)
            if event:
                events.append(event)
        return events

    async def start(self) -> None:
        """Nothing runs in the background."""
