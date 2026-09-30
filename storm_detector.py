import math
import pandas as pd

STORM_FILE = "ibtracs_last3years.csv"


def distance_nm(lat1, lon1, lat2, lon2):
    r_nm = 3440.065

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)

    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1)
        * math.cos(p2)
        * math.sin(dl / 2) ** 2
    )

    return r_nm * 2 * math.atan2(
        math.sqrt(a),
        math.sqrt(1 - a),
    )


def bottle_quadrant(storm_lat, storm_lon, bottle_lat, bottle_lon):
    ns = "N" if bottle_lat >= storm_lat else "S"
    ew = "E" if bottle_lon >= storm_lon else "W"
    return ns + ew


_storm_cache = None


def _naive_utc_timestamp(value):
    ts = pd.Timestamp(value)

    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)

    return ts


def load_storms():
    global _storm_cache

    if _storm_cache is not None:
        return _storm_cache

    data = pd.read_csv(
        STORM_FILE,
        skiprows=[1],
        low_memory=False,
        keep_default_na=False,
    )

    data["storm_time"] = pd.to_datetime(
        data["ISO_TIME"],
        errors="coerce",
    )

    _storm_cache = data
    return _storm_cache


def storm_at_bottle(bottle_lat, bottle_lon, bottle_time):
    data = load_storms()

    target_time = _naive_utc_timestamp(bottle_time)

    nearby_time = data[
        (data["storm_time"] - target_time).abs()
        <= pd.Timedelta(hours=3)
    ].copy()

    best = None

    for _, storm in nearby_time.iterrows():
        try:
            storm_lat = float(storm["LAT"])
            storm_lon = float(storm["LON"])
        except (TypeError, ValueError):
            continue

        distance = distance_nm(
            storm_lat,
            storm_lon,
            bottle_lat,
            bottle_lon,
        )

        quadrant = bottle_quadrant(
            storm_lat,
            storm_lon,
            bottle_lat,
            bottle_lon,
        )

        result = {
            "sid": storm["SID"],
            "name": storm["NAME"],
            "time": storm["ISO_TIME"],
            "storm_latitude": storm_lat,
            "storm_longitude": storm_lon,
            "distance_nm": round(distance, 1),
            "quadrant": quadrant,
            "wind_knots": storm["USA_WIND"],
            "category": storm["USA_SSHS"],
            "wind_band": "outside",
        }

        for speed in (64, 50, 34):
            field = f"USA_R{speed}_{quadrant}"

            try:
                radius = float(storm[field])
            except (TypeError, ValueError):
                continue

            if radius > 0 and distance <= radius:
                result["wind_band"] = f"{speed}_kt"
                result["wind_radius_nm"] = radius
                break

        if result["wind_band"] != "outside":
            if best is None or distance < best["distance_nm"]:
                best = result

    return best


def storm_level_for_bottle(bottle_lat, bottle_lon, bottle_time):
    data = load_storms()
    target_time = _naive_utc_timestamp(bottle_time)

    if (
        target_time < data["storm_time"].min()
        or target_time > data["storm_time"].max()
    ):
        return "unknown", None

    storm = storm_at_bottle(
        bottle_lat,
        bottle_lon,
        bottle_time,
    )

    if storm is None:
        return "calm", None

    levels = {
        "34_kt": "storm",
        "50_kt": "severe",
        "64_kt": "hurricane",
    }

    return levels.get(storm["wind_band"], "calm"), storm


if __name__ == "__main__":
    result = storm_at_bottle(
        20.8,
        -63.4,
        "2025-08-16 18:00:00",
    )

    print(result)
