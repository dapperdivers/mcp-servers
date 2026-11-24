import unittest
from unittest import mock

from mcp_servers import weather
from mcp_servers.httpjson import FetchError

POINT = {"properties": {"forecast": "https://api.weather.gov/gridpoints/XXX/1,2/forecast", "cwa": "XXX",
                        "timeZone": "America/Chicago",
                        "relativeLocation": {"properties": {"city": "Sampletown", "state": "KS"}}}}
PERIODS = [
    {"name": "Today", "startTime": "2026-11-24T06:00:00-06:00", "isDaytime": True, "temperature": 52, "temperatureUnit": "F",
     "probabilityOfPrecipitation": {"value": None}, "windSpeed": "5 to 10 mph", "windDirection": "NW",
     "shortForecast": "Sunny", "detailedForecast": "Sunny, with a high near 52."},
    {"name": "Tonight", "startTime": "2026-11-24T18:00:00-06:00", "isDaytime": False, "temperature": 34, "temperatureUnit": "F",
     "probabilityOfPrecipitation": {"value": 20}, "windSpeed": "10 mph", "windDirection": "N",
     "shortForecast": "Clear", "detailedForecast": "Clear, with a low around 34."},
    {"name": "Wednesday", "startTime": "2026-11-25T06:00:00-06:00", "isDaytime": True, "temperature": 61, "temperatureUnit": "F",
     "probabilityOfPrecipitation": {"value": 70}, "windSpeed": "15 to 30 mph", "windDirection": "E",
     "shortForecast": "Showers", "detailedForecast": "Showers likely."},
    {"name": "Wednesday Night", "startTime": "2026-11-25T18:00:00-06:00", "isDaytime": False, "temperature": 27, "temperatureUnit": "F",
     "probabilityOfPrecipitation": {"value": 0}, "windSpeed": "5 mph", "windDirection": "N",
     "shortForecast": "Cold", "detailedForecast": "Low around 27."},
    {"name": "Thursday", "startTime": "2026-11-26T06:00:00-06:00", "isDaytime": True, "temperature": 45, "temperatureUnit": "F",
     "probabilityOfPrecipitation": {"value": 0}, "windSpeed": "5 mph", "windDirection": "N",
     "shortForecast": "Sunny", "detailedForecast": "Sunny."},
]
ALERTS = {"features": [
    {"id": "https://api.weather.gov/alerts/urn:oid:1", "properties": {"event": "Wind Advisory", "severity": "Moderate",
     "headline": "Wind Advisory until 6 PM", "onset": "2026-11-25T06:00:00-06:00", "ends": "2026-11-25T18:00:00-06:00",
     "areaDesc": "Sample County", "description": "x" * 5000, "instruction": "Secure outdoor objects."}},
    {"id": "https://api.weather.gov/alerts/urn:oid:2", "properties": {"event": "Freeze Warning", "severity": "Severe",
     "headline": "Freeze Warning", "effective": "2026-11-25T22:00:00-06:00", "expires": "2026-11-26T08:00:00-06:00"}},
]}


def fake_api(alerts=ALERTS):
    def get(url, **kw):
        if "/points/" in url:
            return POINT
        if "/alerts/active" in url:
            return alerts
        return {"properties": {"periods": PERIODS, "updateTime": "2026-11-24T10:00:00+00:00"}}
    return get


class Outlook(unittest.TestCase):
    def setUp(self):
        weather._points.clear()

    def test_forecast_summary_and_alerts(self):
        with mock.patch.object(weather, "get_json", fake_api()) as _:
            r = weather.weather_outlook(39.123456, -95.654321, days=2)
        self.assertEqual(r["location"], "Sampletown, KS")
        self.assertEqual(len(r["periods"]), 5)  # two days of 12-hour periods plus the next
        s = r["summary"]
        self.assertEqual(s["coldest_night"], {"night": "Wednesday Night", "date": "2026-11-25", "low_f": 27})
        self.assertEqual(s["hottest_day"]["high_f"], 61)
        self.assertEqual([n["low_f"] for n in s["overnight_lows"]], [34, 27])
        self.assertEqual(s["max_wind_mph"], 30)  # the top of "15 to 30 mph"
        self.assertEqual(s["max_precipitation_chance_pct"], 70)
        self.assertEqual(r["periods"][0]["precipitation_chance_pct"], 0)  # null means none
        # most severe first; times fall back to effective/expires; long text is cut
        self.assertEqual([a["event"] for a in r["alerts"]], ["Freeze Warning", "Wind Advisory"])
        self.assertEqual(r["alerts"][0]["ends"], "2026-11-26T08:00:00-06:00")
        self.assertEqual(r["alerts"][0]["url"], "https://api.weather.gov/alerts/urn:oid:2")
        self.assertEqual(len(r["alerts"][1]["description"]), 1200)

    def test_coordinates_are_rounded_and_point_is_cached(self):
        seen = []

        def get(url, **kw):
            seen.append(url)
            return fake_api({"features": []})(url)

        with mock.patch.object(weather, "get_json", get):
            first = weather.weather_outlook(39.123456, -95.654321)
            weather.weather_outlook(39.123456, -95.654321)
        self.assertEqual(first["alerts"], [])
        self.assertTrue(seen[0].endswith("/points/39.1235,-95.6543"))
        self.assertEqual(sum("/points/" in u for u in seen), 1)
        self.assertIn("alerts/active?point=39.1235,-95.6543", seen[2])
        self.assertIn("lat=39.1235&lon=-95.6543", first["forecast_url"])

    def test_errors_are_returned(self):
        self.assertIn("latitude", weather.weather_outlook(123, 0)["error"])
        with mock.patch.object(weather, "get_json", side_effect=FetchError("HTTP 404 from api.weather.gov: x")):
            self.assertIn("404", weather.weather_outlook(51.5, -0.1)["error"])

    def test_wind_parsing(self):
        self.assertEqual(weather._wind_mph("10 to 20 mph"), 20)
        self.assertEqual(weather._wind_mph("5 mph"), 5)
        self.assertIsNone(weather._wind_mph(None))


if __name__ == "__main__":
    unittest.main()
