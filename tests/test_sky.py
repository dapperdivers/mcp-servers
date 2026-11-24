"""night_sky: the astronomy is checked against published values, the fetched parts are mocked.

Reference for 2026-11-24 at 39.05 N, 95.68 W (US Central time), from the US Naval
Observatory's "one day" service: sunset 17:03, end of civil twilight 17:32, moonrise 16:52,
full moon (100% illuminated).
"""

import datetime
import unittest
import zoneinfo
from unittest import mock

from skyfield.api import wgs84

from mcp_servers import sky, weather
from mcp_servers.httpjson import FetchError

LAT, LON = 39.05, -95.68
TZ = zoneinfo.ZoneInfo("America/Chicago")
DAY = datetime.date(2026, 11, 24)
OBSERVER = wgs84.latlon(LAT, LON)
# ISS elements published by CelesTrak on 2026-11-24.
TLE = ["1 25544U 98067A   26327.91294507  .00005691  00000+0  10921-3 0  9993",
       "2 25544  51.6322 229.6520 0007379  58.6021 301.5689 15.49568558596519"]
POINT = {"timeZone": "America/Chicago", "forecastGridData": "https://api.weather.gov/gridpoints/TOP/81,49"}


def minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


class Astronomy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dark, cls.t0, cls.t1 = sky.darkness(OBSERVER, TZ, DAY)

    def test_darkness_matches_the_naval_observatory(self):
        d = {k: sky._hhmm(v) for k, v in self.dark.items()}
        self.assertLessEqual(abs(minutes(d["sunset"]) - minutes("17:03")), 2)
        self.assertLessEqual(abs(minutes(d["civil_twilight_ends"]) - minutes("17:32")), 2)
        order = ["sunset", "civil_twilight_ends", "nautical_twilight_ends", "astronomical_twilight_ends"]
        self.assertEqual(sorted(order, key=lambda k: self.dark[k]), order)
        self.assertEqual(self.dark["sunrise"].date(), DAY + datetime.timedelta(days=1))

    def test_moon_matches_the_naval_observatory(self):
        m = sky.moon(OBSERVER, TZ, self.dark, self.t0, self.t1)
        self.assertEqual(m["phase"], "Full Moon")
        self.assertGreaterEqual(m["illuminated_pct"], 99)
        self.assertLessEqual(abs(minutes(m["rises"]) - minutes("16:52")), 3)
        self.assertEqual(m["up_during_dark_pct"], 100)  # a full moon is up all night

    def test_planets(self):
        found = {p["planet"]: p for p in sky.planets(OBSERVER, TZ, self.dark)}
        self.assertTrue(found, "no planet up on a night when several are")
        for p in found.values():
            self.assertGreaterEqual(p["highest_altitude_deg"], 10)
            self.assertIn(p["direction_at_highest"], sky.COMPASS)
            self.assertIn(p["when"], {"all night", "early evening", "evening", "before dawn", "late night to dawn", "middle of the night"})
        if "Venus" in found:  # Venus never strays far from the sun: it cannot be up all night
            self.assertNotEqual(found["Venus"]["when"], "all night")
            self.assertLess(found["Venus"]["magnitude"], -3)
        magnitudes = [p["magnitude"] for p in sky.planets(OBSERVER, TZ, self.dark)]
        self.assertEqual(magnitudes, sorted(magnitudes))  # brightest first

    def test_iss_passes_are_only_reported_when_they_can_be_seen(self):
        ts, eph = sky._sky()
        total = seen = 0
        for offset in range(-3, 4):  # a week around the elements' date
            dark, _, _ = sky.darkness(OBSERVER, TZ, DAY + datetime.timedelta(days=offset))
            visible, count = sky.iss_passes(OBSERVER, TZ, dark, TLE)
            total += count
            seen += len(visible)
            self.assertLessEqual(len(visible), count)
            for p in visible:
                self.assertGreaterEqual(p["highest_altitude_deg"], 10)
                self.assertGreater(p["duration_min"], 0)
                # the peak falls in the dark: outside sunset..sunrise it would be daylight
                peak = minutes(p["highest_at"])
                self.assertTrue(peak >= minutes(sky._hhmm(dark["sunset"])) or peak <= minutes(sky._hhmm(dark["sunrise"])))
        self.assertGreater(total, 0, "the ISS passes over this latitude every night")
        self.assertLess(seen, total, "passes in the Earth's shadow must be left out")


class Helpers(unittest.TestCase):
    def test_compass_and_phase(self):
        self.assertEqual([sky.compass(a) for a in (0, 44, 90, 180, 270, 359, 361)], ["N", "NE", "E", "S", "W", "N", "N"])
        self.assertEqual([sky.phase_name(a) for a in (0, 45, 90, 135, 180, 225, 270, 315, 359)],
                         ["New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous", "Full Moon",
                          "Waning Gibbous", "Last Quarter", "Waning Crescent", "New Moon"])

    def test_when_label(self):
        s = datetime.datetime(2026, 11, 24, 18, 0, tzinfo=TZ)
        e = s + datetime.timedelta(hours=12)
        h = lambda n: s + datetime.timedelta(hours=n)  # noqa: E731
        self.assertEqual(sky.when_label(h(0), h(12), s, e), "all night")
        self.assertEqual(sky.when_label(h(0), h(2), s, e), "early evening")
        self.assertEqual(sky.when_label(h(0), h(8), s, e), "evening")
        self.assertEqual(sky.when_label(h(10), h(12), s, e), "before dawn")
        self.assertEqual(sky.when_label(h(6), h(12), s, e), "late night to dawn")
        self.assertEqual(sky.when_label(h(4), h(8), s, e), "middle of the night")

    def test_cloud_cover_uses_only_the_dark_hours(self):
        dark, _, _ = sky.darkness(OBSERVER, TZ, DAY)
        grid = {"skyCover": {"values": [
            {"validTime": "2026-11-24T18:00:00+00:00/PT5H", "value": 100},  # afternoon, local: ignored
            {"validTime": "2026-11-25T00:00:00+00:00/PT3H", "value": 20},   # 18:00-21:00 local
            {"validTime": "2026-11-25T03:00:00+00:00/PT9H", "value": 60},   # 21:00-06:00 local
            {"validTime": "2026-11-25T14:00:00+00:00/PT1H", "value": 100},  # next morning: ignored
        ]}}
        c = sky.cloud_cover(grid, dark)
        self.assertEqual(c["evening_pct"], 20)
        self.assertEqual(c["clearest_pct"], 20)
        self.assertTrue(40 <= c["overnight_pct"] <= 60)
        self.assertEqual(c["sky"], "partly cloudy")
        self.assertIsNone(sky.cloud_cover({"skyCover": {"values": []}}, dark))

    def test_kp_outlook(self):
        dark, _, _ = sky.darkness(OBSERVER, TZ, DAY)
        rows = [
            {"time_tag": "2026-11-24T12:00:00", "kp": 7.0, "noaa_scale": "G3"},  # before sunset, local
            {"time_tag": "2026-11-25T03:00:00", "kp": 5.33, "noaa_scale": "G1"},
            {"time_tag": "2026-11-25T06:00:00", "kp": 2.0, "noaa_scale": None},
            {"time_tag": "2026-11-26T03:00:00", "kp": 9.0, "noaa_scale": "G5"},  # the next night
        ]
        self.assertEqual(sky.kp_outlook(rows, dark), {"kp_max": 5.33, "storm_scale": "G1"})
        legacy = [["time_tag", "kp", "observed", "noaa_scale"], ["2026-11-25 03:00:00", "4.00", "predicted", None]]
        self.assertEqual(sky.kp_outlook(legacy, dark), {"kp_max": 4.0, "storm_scale": None})

    def test_asteroids(self):
        def rock(name, lunar):
            return {"name": name, "nasa_jpl_url": "u", "is_potentially_hazardous_asteroid": False,
                    "estimated_diameter": {"meters": {"estimated_diameter_max": 120.4}},
                    "close_approach_data": [{"miss_distance": {"lunar": str(lunar), "kilometers": "1000.6"}}]}
        feed = {"near_earth_objects": {"2026-11-24": [rock("far", 50), rock("near", 3.21), {"name": "broken"}, rock("mid", 9)]}}
        got = sky.close_asteroids(feed)
        self.assertEqual([a["name"] for a in got], ["near", "mid"])
        self.assertEqual(got[0]["miss_distance_lunar"], 3.2)


class Tool(unittest.TestCase):
    def setUp(self):
        weather._points.clear()
        sky._tle.update(lines=None, fetched=0.0)

    def test_computed_parts_survive_every_fetch_failing(self):
        def get(url, **kw):
            if "/points/" in url:
                return {"properties": POINT}
            raise FetchError("HTTP 503 from somewhere")

        with mock.patch.object(weather, "get_json", get), mock.patch.object(sky, "get_json", get), \
                mock.patch.object(sky, "_iss_elements", side_effect=FetchError("request to celestrak.org failed")):
            r = sky.night_sky(LAT, LON, "2026-11-24")
        self.assertEqual(r["night_of"], "2026-11-24")
        self.assertEqual(r["moon"]["phase"], "Full Moon")
        self.assertTrue(r["planets"])
        self.assertIsNone(r["clouds"])
        self.assertIsNone(r["space_weather"])
        self.assertEqual(r["iss"], {"visible_passes": [], "passes_of_any_kind": None})
        self.assertEqual(sorted(e.split(":")[0] for e in r["errors"]), ["asteroids", "clouds", "iss", "space_weather"])

    def test_all_sections(self):
        def get(url, **kw):
            if "/points/" in url:
                return {"properties": POINT}
            if "gridpoints" in url:
                return {"properties": {"skyCover": {"values": [{"validTime": "2026-11-25T00:00:00+00:00/PT12H", "value": 10}]}}}
            if "swpc" in url:
                return [{"time_tag": "2026-11-25T03:00:00", "kp": 2.33, "noaa_scale": None}]
            return {"near_earth_objects": {}}

        with mock.patch.object(weather, "get_json", get), mock.patch.object(sky, "get_json", get), \
                mock.patch.object(sky, "_iss_elements", return_value=TLE):
            r = sky.night_sky(LAT, LON, "2026-11-24")
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["clouds"]["sky"], "clear")
        self.assertEqual(r["space_weather"]["kp_max"], 2.33)
        self.assertIsInstance(r["iss"]["passes_of_any_kind"], int)
        self.assertEqual(r["time_zone"], "America/Chicago")

    def test_bad_input(self):
        self.assertIn("latitude", sky.night_sky(99, 0)["error"])
        with mock.patch.object(weather, "get_json", return_value={"properties": POINT}):
            self.assertIn("YYYY-MM-DD", sky.night_sky(LAT, LON, "tonight")["error"])
        weather._points.clear()
        with mock.patch.object(weather, "get_json", side_effect=FetchError("HTTP 404 from api.weather.gov")):
            self.assertIn("US only", sky.night_sky(51.5, -0.1)["error"])


if __name__ == "__main__":
    unittest.main()
