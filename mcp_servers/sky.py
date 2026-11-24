"""night_sky: what is worth looking up at tonight, for one point. A tool on the weather server.

Computed here with Skyfield from JPL's DE421 ephemeris (baked into the image):
  darkness   sunset, the end of each twilight, and dawn
  moon       phase, illumination, rise and set, how much of the dark it is up for
  planets    which naked-eye planets are up in the dark, when, where and how bright
  ISS        visible passes (sky dark at the observer, station in sunlight)

Fetched, each optional (a source that fails is listed in "errors" and the rest still comes back):
  clouds          National Weather Service hourly sky cover for the dark hours
  space weather   NOAA SWPC planetary Kp forecast
  ISS orbit       CelesTrak
  asteroids       NASA NeoWs close approaches (NASA_API_KEY, else the shared DEMO_KEY)

Environment (optional):
  SKYFIELD_DATA  Folder holding de421.bsp. Default /opt/skyfield (where the image puts it).
  NASA_API_KEY   An api.data.gov key.
"""

import datetime
import os
import re
import time
import urllib.error
import urllib.request
import zoneinfo

from skyfield import almanac
from skyfield.api import EarthSatellite, Loader, wgs84

from .httpjson import USER_AGENT, FetchError, get_json
from .weather import _point, server

TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE"
KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json"
NEO_URL = "https://api.nasa.gov/neo/rest/v1/feed"
TLE_TTL = 6 * 3600  # orbit elements age quickly; a pass computed from week-old ones is minutes off

MIN_ALTITUDE = 10.0  # degrees above the horizon before something counts as up
SUN_DARK = -6.0  # sun this far below the horizon: bright planets and the ISS show
PLANETS = {  # name -> DE421 target
    "Mercury": "mercury",
    "Venus": "venus",
    "Mars": "mars",
    "Jupiter": "jupiter barycenter",
    "Saturn": "saturn barycenter",
}
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
PHASES = ["New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous",
          "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent"]  # fmt: skip

_load = Loader(os.environ.get("SKYFIELD_DATA", "/opt/skyfield"), verbose=False)
_state = {}
_tle = {"lines": None, "fetched": 0.0}


def _sky():
    """(timescale, ephemeris), loaded on first use. The timescale is Skyfield's built-in one."""
    if not _state:
        _state.update(ts=_load.timescale(builtin=True), eph=_load("de421.bsp"))
    return _state["ts"], _state["eph"]


def compass(azimuth_degrees):
    return COMPASS[int((azimuth_degrees % 360) / 22.5 + 0.5) % 16]


def phase_name(angle_degrees):
    """Moon phase from the sun-moon ecliptic angle (0 new, 90 first quarter, 180 full)."""
    return PHASES[int((angle_degrees % 360) / 45 + 0.5) % 8]


def _local(t, tz):
    return t.utc_datetime().astimezone(tz).replace(microsecond=0)


def _hhmm(dt):
    return dt.strftime("%H:%M")


def darkness(observer, tz, day):
    """Sunset to sunrise for the night that starts on `day` (a local date).

    Returns local datetimes for each boundary, or None for one that does not happen
    (high latitudes in summer), plus the Skyfield times bounding the search.
    """
    ts, eph = _sky()
    noon = datetime.datetime.combine(day, datetime.time(12), tz)
    t0, t1 = ts.from_datetime(noon), ts.from_datetime(noon + datetime.timedelta(days=1))
    times, levels = almanac.find_discrete(t0, t1, almanac.dark_twilight_day(eph, observer))
    # levels: 4 day, 3 civil, 2 nautical, 1 astronomical twilight, 0 night
    names = {
        (4, 3): "sunset", (3, 2): "civil_twilight_ends", (2, 1): "nautical_twilight_ends",
        (1, 0): "astronomical_twilight_ends", (0, 1): "astronomical_twilight_begins",
        (1, 2): "nautical_twilight_begins", (2, 3): "civil_twilight_begins", (3, 4): "sunrise",
    }  # fmt: skip
    out = dict.fromkeys(names.values())
    previous = int(almanac.dark_twilight_day(eph, observer)(t0))
    for t, level in zip(times, levels):
        key = names.get((previous, int(level)))
        if key and out[key] is None:
            out[key] = _local(t, tz)
        previous = int(level)
    return out, t0, t1


def _grid(start, end, minutes=5):
    """Skyfield times every `minutes` from start to end (aware datetimes), inclusive of start."""
    ts, _ = _sky()
    n = max(2, int((end - start).total_seconds() // (minutes * 60)) + 1)
    return ts.from_datetimes([start + datetime.timedelta(minutes=minutes * i) for i in range(n)])


def moon(observer, tz, dark, t0, t1):
    ts, eph = _sky()
    at = dark["sunset"] or _local(t0, tz)
    t = ts.from_datetime(at + datetime.timedelta(hours=3))  # mid-evening
    angle = almanac.moon_phase(eph, t).degrees
    out = {
        "phase": phase_name(angle),
        "waxing": bool(angle % 360 < 180),
        "illuminated_pct": round(100 * float(almanac.fraction_illuminated(eph, "moon", t))),
        "rises": None,
        "sets": None,
        "up_during_dark_pct": None,
    }
    for key, find in (("rises", almanac.find_risings), ("sets", almanac.find_settings)):
        times, real = find(eph["earth"] + observer, eph["moon"], t0, t1)
        hits = [_local(x, tz) for x, ok in zip(times, real) if ok]
        if hits:
            out[key] = _hhmm(hits[0])
    start, end = dark["civil_twilight_ends"], dark["civil_twilight_begins"]
    if start and end:
        grid = _grid(start, end, 10)
        alt, _, _ = (eph["earth"] + observer).at(grid).observe(eph["moon"]).apparent().altaz()
        out["up_during_dark_pct"] = round(100 * float((alt.degrees > 0).mean()))
    return out


def when_label(first, last, start, end):
    """A plain word for the part of the dark (start..end) that first..last covers."""
    slack = datetime.timedelta(minutes=10)
    quarter = (end - start) / 4
    from_dusk, to_dawn = first <= start + slack, last >= end - slack
    if from_dusk and to_dawn:
        return "all night"
    if from_dusk:
        return "early evening" if last <= start + quarter else "evening"
    if to_dawn:
        return "before dawn" if first >= end - quarter else "late night to dawn"
    return "middle of the night"


def planets(observer, tz, dark):
    """Naked-eye planets at least MIN_ALTITUDE up while the sun is at least 6 degrees down."""
    _, eph = _sky()
    start, end = dark["civil_twilight_ends"], dark["civil_twilight_begins"]
    if not (start and end):
        return []
    grid = _grid(start, end, 5)
    here = (eph["earth"] + observer).at(grid)
    whens = [_local(t, tz) for t in grid]
    out = []
    for name, target in PLANETS.items():
        apparent = here.observe(eph[target]).apparent()
        alt, az, _ = apparent.altaz()
        up = [i for i, a in enumerate(alt.degrees) if a >= MIN_ALTITUDE]
        if not up:
            continue
        best = max(up, key=lambda i: alt.degrees[i])
        first, last = up[0], up[-1]
        when = when_label(whens[first], whens[last], start, end)
        try:
            magnitude = round(float(_magnitude(apparent, best)), 1)
        except Exception:  # magnitude is a nicety; never lose the planet over it
            magnitude = None
        out.append(
            {
                "planet": name,
                "when": when,
                "visible_from": _hhmm(whens[first]),
                "visible_until": _hhmm(whens[last]),
                "highest_at": _hhmm(whens[best]),
                "highest_altitude_deg": round(float(alt.degrees[best])),
                "direction_at_highest": compass(float(az.degrees[best])),
                "direction_at_start": compass(float(az.degrees[first])),
                "magnitude": magnitude,  # lower is brighter; Sirius is -1.5
            }
        )
    out.sort(key=lambda p: (p["magnitude"] is None, p["magnitude"] if p["magnitude"] is not None else 0))
    return out


def _magnitude(apparent, index):
    from skyfield.magnitudelib import planetary_magnitude

    return planetary_magnitude(apparent)[index]


def _iss_elements():
    if _tle["lines"] and time.time() - _tle["fetched"] < TLE_TTL:
        return _tle["lines"]
    req = urllib.request.Request(TLE_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("ascii", "replace")
    except (urllib.error.URLError, TimeoutError) as e:
        raise FetchError(f"request to celestrak.org failed: {e}") from None
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    pair = [ln for ln in lines if re.match(r"^[12] 25544", ln)]
    if len(pair) != 2:
        raise FetchError("celestrak.org did not return ISS orbit elements")
    _tle.update(lines=pair, fetched=time.time())
    return pair


def iss_passes(observer, tz, dark, elements):
    """Visible ISS passes in the dark: (visible passes, number of passes of any kind)."""
    ts, eph = _sky()
    start, end = dark["sunset"], dark["sunrise"]
    if not (start and end):
        return [], 0
    sat = EarthSatellite(elements[0], elements[1], "ISS", ts)
    times, events = sat.find_events(observer, ts.from_datetime(start), ts.from_datetime(end), altitude_degrees=MIN_ALTITUDE)
    passes, current = [], {}
    for t, event in zip(times, events):
        current[int(event)] = t
        if int(event) == 2:  # set: the pass is complete if we saw its rise and peak
            if 0 in current and 1 in current:
                passes.append(current)
            current = {}
    difference = sat - observer
    here = eph["earth"] + observer
    visible = []
    for p in passes:
        peak = p[1]
        sun_alt = here.at(peak).observe(eph["sun"]).apparent().altaz()[0].degrees
        if float(sun_alt) > SUN_DARK or not bool(sat.at(peak).is_sunlit(eph)):
            continue
        alt, az, _ = difference.at(peak).altaz()
        visible.append(
            {
                "starts": _hhmm(_local(p[0], tz)),
                "starts_direction": compass(float(difference.at(p[0]).altaz()[1].degrees)),
                "highest_at": _hhmm(_local(peak, tz)),
                "highest_altitude_deg": round(float(alt.degrees)),
                "highest_direction": compass(float(az.degrees)),
                "ends": _hhmm(_local(p[2], tz)),
                "ends_direction": compass(float(difference.at(p[2]).altaz()[1].degrees)),
                "duration_min": round((p[2].utc_datetime() - p[0].utc_datetime()).total_seconds() / 60, 1),
                # the station can fade out mid-pass when it enters the Earth's shadow
                "sunlit_until_end": bool(sat.at(p[2]).is_sunlit(eph)),
            }
        )
    return visible, len(passes)


def _valid_hours(valid_time):
    """An NWS validTime ('2026-11-24T06:00:00+00:00/PT2H') -> the hours it covers, as aware datetimes."""
    stamp, _, duration = valid_time.partition("/")
    start = datetime.datetime.fromisoformat(stamp)
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?)?", duration or "PT1H")
    hours = (int(m.group(1) or 0) * 24 + int(m.group(2) or 0)) if m else 1
    return [start + datetime.timedelta(hours=i) for i in range(max(1, hours))]


def cloud_cover(grid_properties, dark):
    """Mean forecast sky cover (percent) over the dark hours and over the first three of them."""
    start, end = dark["civil_twilight_ends"], dark["civil_twilight_begins"]
    if not (start and end):
        return None
    night, evening = [], []
    for item in (grid_properties.get("skyCover") or {}).get("values") or []:
        if item.get("value") is None:
            continue
        for hour in _valid_hours(item["validTime"]):
            if start - datetime.timedelta(minutes=30) <= hour <= end:
                night.append(item["value"])
                if hour <= start + datetime.timedelta(hours=3):
                    evening.append(item["value"])
    if not night:
        return None
    mean = round(sum(night) / len(night))
    return {
        "overnight_pct": mean,
        "evening_pct": round(sum(evening) / len(evening)) if evening else None,
        "clearest_pct": round(min(night)),
        "sky": "clear" if mean < 20 else "mostly clear" if mean < 40 else "partly cloudy" if mean < 70 else "cloudy",
    }


def kp_outlook(rows, dark):
    """Highest forecast planetary Kp between sunset and sunrise."""
    start, end = dark["sunset"], dark["sunrise"]
    if not (start and end):
        return None
    best = None
    for row in rows:
        if isinstance(row, dict):
            stamp, kp, scale = row.get("time_tag"), row.get("kp"), row.get("noaa_scale")
        else:  # older shape: a header row then lists
            stamp, kp, scale = row[0], row[1], row[3] if len(row) > 3 else None
        try:
            when = datetime.datetime.fromisoformat(str(stamp)).replace(tzinfo=datetime.timezone.utc)
            kp = float(kp)
        except (TypeError, ValueError):
            continue
        # each row covers three hours from its time
        if when + datetime.timedelta(hours=3) > start and when < end and (best is None or kp > best["kp_max"]):
            best = {"kp_max": round(kp, 2), "storm_scale": scale or None}
    return best


def close_asteroids(feed, limit=2):
    """The nearest asteroid flybys in a NeoWs feed, closest first."""
    out = []
    for objects in (feed.get("near_earth_objects") or {}).values():
        for obj in objects:
            try:
                approach = obj["close_approach_data"][0]
                out.append(
                    {
                        "name": obj["name"],
                        "miss_distance_lunar": round(float(approach["miss_distance"]["lunar"]), 1),
                        "miss_distance_km": round(float(approach["miss_distance"]["kilometers"])),
                        "diameter_m_max": round(obj["estimated_diameter"]["meters"]["estimated_diameter_max"]),
                        "potentially_hazardous": bool(obj.get("is_potentially_hazardous_asteroid")),
                        "url": obj.get("nasa_jpl_url"),
                    }
                )
            except (KeyError, IndexError, TypeError, ValueError):
                continue
    out.sort(key=lambda a: a["miss_distance_lunar"])
    return out[:limit]


@server.tool()
def night_sky(latitude: float, longitude: float, date: str = "") -> dict:
    """What can be seen in the night sky from one point in the United States.

    latitude, longitude: decimal degrees (US longitudes are negative).
    date: the local date the night starts on, YYYY-MM-DD. Empty = tonight.

    All times are local to the point, 24-hour. Returns:
      darkness   sunset, when each twilight ends (the sky is fully dark after
                 astronomical_twilight_ends), and the same in reverse before sunrise.
      moon       phase, percent illuminated, rise and set, and the share of the dark hours it is
                 up. A bright moon that is up washes out faint stars and meteors.
      clouds     forecast sky cover for the dark hours, percent, with a one-word "sky".
      planets    naked-eye planets that get at least 10 degrees up in the dark, brightest first:
                 when, the hours, how high and in which compass direction.
      iss        passes of the International Space Station that will actually be visible (dark
                 sky, station sunlit): start, peak and end times with directions. It looks like
                 a bright star moving steadily, with no blinking lights.
      space_weather  the highest planetary Kp index forecast for the night (0-9) and NOAA's
                 storm scale if any. Aurora is rarely seen from the southern half of the US
                 below about Kp 8.
      asteroids  the closest asteroid flybys of the day; these are facts of interest, not
                 things to look for (none is visible to the eye).
    A section that could not be worked out is null or empty and its reason is in "errors":
    report it as not checked. Computed with Skyfield from JPL's DE421 ephemeris.
    """
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return {"error": "latitude must be -90..90 and longitude -180..180"}
    lat, lon = round(latitude, 4), round(longitude, 4)
    errors = []
    point = None
    try:
        point = _point(lat, lon)
        tz = zoneinfo.ZoneInfo(point["timeZone"])
    except (FetchError, KeyError, zoneinfo.ZoneInfoNotFoundError) as e:
        return {"error": f"could not work out the time zone for this point (US only): {e}"}
    try:
        day = datetime.date.fromisoformat(date) if date else datetime.datetime.now(tz).date()
    except ValueError:
        return {"error": "date must be YYYY-MM-DD"}

    observer = wgs84.latlon(lat, lon)
    _, eph = _sky()
    dark, t0, t1 = darkness(observer, tz, day)
    result = {
        "night_of": day.isoformat(),
        "time_zone": point["timeZone"],
        "darkness": {k: (_hhmm(v) if v else None) for k, v in dark.items()},
        "moon": moon(observer, tz, dark, t0, t1),
        "planets": planets(observer, tz, dark),
        "clouds": None,
        "iss": {"visible_passes": [], "passes_of_any_kind": None},
        "space_weather": None,
        "asteroids": [],
    }
    try:
        result["clouds"] = cloud_cover(get_json(point["forecastGridData"], timeout=60)["properties"], dark)
        if result["clouds"] is None:
            errors.append("clouds: the forecast does not reach this night")
    except (FetchError, KeyError) as e:
        errors.append(f"clouds: {e}")
    try:
        visible, total = iss_passes(observer, tz, dark, _iss_elements())
        result["iss"] = {"visible_passes": visible, "passes_of_any_kind": total}
    except (FetchError, ValueError) as e:
        errors.append(f"iss: {e}")
    try:
        result["space_weather"] = kp_outlook(get_json(KP_URL), dark)
    except FetchError as e:
        errors.append(f"space_weather: {e}")
    try:
        key = os.environ.get("NASA_API_KEY") or "DEMO_KEY"
        feed = get_json(f"{NEO_URL}?start_date={day}&end_date={day}&api_key={key}")
        result["asteroids"] = close_asteroids(feed)
    except FetchError as e:
        errors.append(f"asteroids: {e}")
    result["errors"] = errors
    return result
