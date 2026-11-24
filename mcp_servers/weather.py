"""weather: US forecast and active alerts for a point, from the National Weather Service API.

One tool, `weather_outlook`, returns both, so a caller that asks for the forecast can never
skip the alerts. api.weather.gov needs no key; it is a public API meant for programs, but
its robots.txt keeps page fetchers out, which is why this is a tool and not a web fetch.

The caller passes coordinates. Nothing about any location is stored here.
"""

import time

from mcp.server.mcpserver import MCPServer

from .httpjson import FetchError, get_json

API = "https://api.weather.gov"
POINT_TTL = 24 * 3600  # a point's forecast office and grid cell do not move

server = MCPServer("weather")
_points = {}


def _point(lat, lon):
    key = (lat, lon)
    hit = _points.get(key)
    if hit and time.time() - hit[0] < POINT_TTL:
        return hit[1]
    props = get_json(f"{API}/points/{lat},{lon}")["properties"]
    _points[key] = (time.time(), props)
    return props


def _wind_mph(text):
    """'10 to 20 mph' -> 20; the top of the range is what matters for a warning."""
    nums = [int(tok) for tok in (text or "").replace("mph", " ").split() if tok.isdigit()]
    return max(nums) if nums else None


def shape_periods(periods, days):
    """NWS 12-hour forecast periods -> flat records, at most `days` days ahead."""
    out = []
    for p in periods[: days * 2 + 1]:
        out.append(
            {
                "name": p.get("name"),
                "starts": p.get("startTime"),
                "daytime": bool(p.get("isDaytime")),
                "temperature_f": p.get("temperature") if p.get("temperatureUnit") == "F" else None,
                "precipitation_chance_pct": (p.get("probabilityOfPrecipitation") or {}).get("value") or 0,
                "wind": f"{p.get('windDirection') or ''} {p.get('windSpeed') or ''}".strip(),
                "wind_max_mph": _wind_mph(p.get("windSpeed")),
                "summary": p.get("shortForecast"),
                "detail": p.get("detailedForecast"),
            }
        )
    return out


def summarize(periods):
    """The numbers a frost or heat rule is checked against, worked out here."""
    lows = [
        {"night": p["name"], "date": (p["starts"] or "")[:10], "low_f": p["temperature_f"]}
        for p in periods
        if not p["daytime"] and p["temperature_f"] is not None
    ]
    highs = [
        {"day": p["name"], "date": (p["starts"] or "")[:10], "high_f": p["temperature_f"]}
        for p in periods
        if p["daytime"] and p["temperature_f"] is not None
    ]
    coldest = min(lows, key=lambda x: x["low_f"]) if lows else None
    hottest = max(highs, key=lambda x: x["high_f"]) if highs else None
    return {
        "overnight_lows": lows,
        "daytime_highs": highs,
        "coldest_night": coldest,
        "hottest_day": hottest,
        "max_precipitation_chance_pct": max((p["precipitation_chance_pct"] for p in periods), default=0),
        "max_wind_mph": max((p["wind_max_mph"] or 0 for p in periods), default=0),
    }


def shape_alerts(features):
    out = []
    for f in features:
        p = f.get("properties") or {}
        out.append(
            {
                "event": p.get("event"),
                "severity": p.get("severity"),
                "urgency": p.get("urgency"),
                "certainty": p.get("certainty"),
                "headline": p.get("headline"),
                "starts": p.get("onset") or p.get("effective"),
                "ends": p.get("ends") or p.get("expires"),
                "area": p.get("areaDesc"),
                "description": (p.get("description") or "")[:1200],
                "instruction": (p.get("instruction") or "")[:600],
                # The alert's own address in the API; there is no stable public web page per alert.
                "url": f.get("id") or p.get("@id"),
            }
        )
    order = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3}
    out.sort(key=lambda a: order.get(a["severity"], 4))
    return out


@server.tool()
def weather_outlook(latitude: float, longitude: float, days: int = 4) -> dict:
    """Forecast and active National Weather Service alerts for one point in the United States.

    latitude, longitude: decimal degrees (north and east positive, so US longitudes are negative).
    days: how many days ahead to return, 1-7.

    Returns:
      alerts   every active watch, warning and advisory covering the point, most severe first
               (frost and freeze warnings, wind, flood, heat, winter storm ...). An empty list
               is a real "no active alerts": the official feed was checked.
      periods  the forecast in 12-hour periods: temperature in F, chance of precipitation, wind,
               and the forecaster's text.
      summary  each night's low and each day's high as numbers, the coldest night, the hottest
               day, and the highest wind and precipitation chance in the window. Compare frost
               and heat thresholds against these numbers rather than reading them out of text.
    Link "forecast_url" as the source.
    """
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return {"error": "latitude must be -90..90 and longitude -180..180"}
    lat, lon = round(latitude, 4), round(longitude, 4)  # the API rejects more than 4 decimals
    days = max(1, min(int(days), 7))
    try:
        point = _point(lat, lon)
        forecast = get_json(point["forecast"])["properties"]
        alerts = get_json(f"{API}/alerts/active?point={lat},{lon}")
    except FetchError as e:
        # 404 on /points means the point is outside NWS coverage (not in the US, or at sea).
        return {"error": str(e)}
    periods = shape_periods(forecast.get("periods") or [], days)
    place = (point.get("relativeLocation") or {}).get("properties") or {}
    return {
        "location": ", ".join(x for x in (place.get("city"), place.get("state")) if x),
        "forecast_office": point.get("cwa"),
        "time_zone": point.get("timeZone"),
        "forecast_updated": forecast.get("updateTime") or forecast.get("updated"),
        "forecast_url": f"https://forecast.weather.gov/MapClick.php?lat={lat}&lon={lon}",
        "alerts": shape_alerts(alerts.get("features") or []),
        "summary": summarize(periods),
        "periods": periods,
    }
