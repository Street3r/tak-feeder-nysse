# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the Nysse feeder."""

import asyncio
import json
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from tak_feeder_nysse import feeder

MVJ = {
    "lineRef": "3",
    "vehicleLocation": {"longitude": "23.7684740", "latitude": "61.4958800"},
    "bearing": "143.49",
    "delay": "P0Y0M0DT0H2M10.000S",
    "vehicleRef": "56920_13",
    "destinationShortName": "0951",
    "speed": "36.0",
    "onwardCalls": [{"expectedArrivalTime": "2026-10-13T11:18:23Z",
                     "stopPointRef": "https://data.itsfactory.fi/journeys/api/1/stop-points/0001"}],
}
ACTIVITY = {"status": "success", "body": [{"monitoredVehicleJourney": MVJ}]}
STOPS = {"status": "success", "body": [
    {"shortName": "0001", "name": "Keskustori H"}, {"shortName": "0951", "name": "Hervanta"}]}


def fake_fetch(url, headers, timeout):
    body = STOPS if url.endswith("/stop-points") else ACTIVITY
    return json.dumps(body).encode()


class NysseTest(unittest.TestCase):
    def run_events(self, src):
        with mock.patch.object(feeder, "fetch", mock.AsyncMock(side_effect=fake_fetch)) as m:
            events = [ET.fromstring(e) for e in asyncio.run(src.events())]
        return events, [c.args[0] for c in m.call_args_list]

    def test_event(self) -> None:
        events, _ = self.run_events(feeder.NysseSource(feeder.Settings()))
        ev = events[0]
        self.assertEqual(ev.get("uid"), "nysse-56920_13")
        self.assertEqual(ev.get("type"), "a-f-G-E-V-C-M-H")
        self.assertEqual(ev.find("detail/contact").get("callsign"), "NYSSE 3")
        self.assertEqual(ev.find("detail/track").get("speed"), "10.00")  # 36 km/h
        text = ev.find("detail/remarks").text
        self.assertIn("To: Hervanta", text)
        self.assertIn("Next: Keskustori H 14:18", text)  # arrival time, local
        self.assertIn("Delay: +2 min", text)
        self.assertTrue(text.endswith("#NYSSE #bus"))

    def test_default_is_all_lines(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            settings = feeder.Settings.from_env()
        _, urls = self.run_events(feeder.NysseSource(settings))
        self.assertNotIn("lineRef", urls[-1])

    def test_wildcards_filtered_locally(self) -> None:
        src = feeder.NysseSource(feeder.Settings(line_filter="1*"))
        events, urls = self.run_events(src)
        self.assertNotIn("lineRef", urls[-1])
        self.assertEqual(events, [])
        src = feeder.NysseSource(feeder.Settings(line_filter="3,60"))
        events, urls = self.run_events(src)
        self.assertIn("lineRef=3,60", urls[-1])
        self.assertEqual(len(events), 1)

    def run_at(self, src, now):
        with mock.patch.object(feeder, "fetch", mock.AsyncMock(side_effect=fake_fetch)) as m:
            events = asyncio.run(src.events(now))
        return len(events), sum(1 for c in m.call_args_list if "vehicle-activity" in c.args[0])

    def test_send_and_fetch_rates(self) -> None:
        src = feeder.NysseSource(feeder.Settings())  # moving every 5 s, fetch every 5 s
        self.assertEqual(self.run_at(src, 0), (1, 1))
        self.assertEqual(self.run_at(src, 2), (0, 0))
        self.assertEqual(self.run_at(src, 5), (1, 1))  # same position re-sent

    def test_fetch_never_faster_than_minimum(self) -> None:
        settings = feeder.Settings(send=feeder.SendPolicy(moving=1))
        self.assertEqual(settings.api_interval(), 3.0)
        src = feeder.NysseSource(settings)
        self.assertEqual(self.run_at(src, 0), (1, 1))
        self.assertEqual(self.run_at(src, 1), (1, 0))  # sent from the cache
        self.assertEqual(self.run_at(src, 3), (1, 1))

    def test_vehicle_gone_from_api_is_dropped(self) -> None:
        src = feeder.NysseSource(feeder.Settings())
        self.run_at(src, 0)
        empty = json.dumps({"body": []}).encode()
        with mock.patch.object(feeder, "fetch", mock.AsyncMock(return_value=empty)):
            self.assertEqual(asyncio.run(src.events(10)), [])
        self.assertEqual(src.vehicles, {})

    def test_duration(self) -> None:
        self.assertEqual(feeder.iso_duration_seconds("-P0Y0M0DT0H0M36.000S"), -36.0)
        self.assertIsNone(feeder.iso_duration_seconds("garbage"))

    def test_bad_vehicle_does_not_stop_round(self) -> None:
        src = feeder.NysseSource(feeder.Settings())
        bad = dict(MVJ, vehicleRef="x", speed="fast")
        payload = {"body": [{"monitoredVehicleJourney": bad}, {"monitoredVehicleJourney": MVJ}]}
        with mock.patch.object(feeder, "fetch", mock.AsyncMock(
                side_effect=lambda u, h, timeout=None: json.dumps(payload).encode())):
            events = asyncio.run(src.events())
        self.assertEqual(len(events), 2)  # unparsable speed -> 0, not a crash


if __name__ == "__main__":
    unittest.main()
