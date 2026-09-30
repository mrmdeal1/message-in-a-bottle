import json
import math
import sys
from datetime import datetime, timedelta, timezone

import copernicusmarine

import bottle_engine as engine


DATASET_ID = "cmems_mod_glo_phy-cur_anfc_0.083deg_P1D-m"
SURFACE_DEPTH = 0.49402499198913574
HISTORICAL_YEAR = 2025


def as_utc_iso(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def main():
    bottle = json.load(sys.stdin)

    # This script is intentionally isolated from persistent storage. The API
    # saves the returned bottle only after the child process completes.
    engine.save_bottle = lambda _bottle: None

    start_date = f"{HISTORICAL_YEAR}-01-01"
    end_date = f"{HISTORICAL_YEAR}-12-31"

    ds = copernicusmarine.open_dataset(
        dataset_id=DATASET_ID,
        variables=["uo", "vo"],
        start_datetime=start_date,
        end_datetime=end_date,
        minimum_depth=SURFACE_DEPTH,
        maximum_depth=SURFACE_DEPTH,
    )

    original_move = engine.move_bottle_live
    original_storm = engine.storm_level_for_bottle

    start_history_len = len(bottle.get("journey_history", []))
    start_coast_rolls = bottle.get("coast_roll_count", 0)
    days_advanced = 0
    stop_event = None

    try:
        for day_index in range(365):
            if bottle.get("opened") or bottle.get("status") != "drifting":
                break

            historical_dt = datetime(
                HISTORICAL_YEAR,
                1,
                1,
                tzinfo=timezone.utc,
            ) + timedelta(days=day_index)
            historical_date = historical_dt.strftime("%Y-%m-%d")

            def historical_move(lat, lon, _date, hours=24, historical_date=historical_date):
                point = ds.sel(
                    time=historical_date,
                    depth=SURFACE_DEPTH,
                    latitude=lat,
                    longitude=lon,
                    method="nearest",
                )

                u = float(point["uo"].values.squeeze())
                v = float(point["vo"].values.squeeze())

                if not math.isfinite(u) or not math.isfinite(v):
                    raise ValueError(
                        f"No usable current data at {lat}, {lon} on {historical_date}"
                    )

                seconds = hours * 3600.0
                east_m = u * seconds
                north_m = v * seconds
                new_lat = lat + north_m / 111320.0
                cos_lat = math.cos(math.radians(lat))
                if abs(cos_lat) < 1e-6:
                    cos_lat = 1e-6
                new_lon = lon + east_m / (111320.0 * cos_lat)
                miles = math.hypot(east_m, north_m) / 1609.344
                return new_lat, new_lon, miles

            def historical_storm(lat, lon, _journey_time, historical_dt=historical_dt):
                return original_storm(
                    lat,
                    lon,
                    as_utc_iso(historical_dt),
                )

            engine.move_bottle_live = historical_move
            engine.storm_level_for_bottle = historical_storm

            previous_history_len = len(bottle.get("journey_history", []))
            bottle = engine.advance_bottle(
                bottle,
                total_hours=24,
                step_hours=24,
            )
            days_advanced += 1

            for event in bottle.get("journey_history", [])[previous_history_len:]:
                event_type = event.get("event")
                if event_type in {"washed_ashore", "lost_at_sea"}:
                    stop_event = event_type
                    break

            if stop_event or bottle.get("status") != "drifting":
                break
    finally:
        engine.move_bottle_live = original_move
        engine.storm_level_for_bottle = original_storm
        close = getattr(ds, "close", None)
        if callable(close):
            close()

    new_history = bottle.get("journey_history", [])[start_history_len:]
    storm_encounters = sum(
        1 for event in new_history if event.get("event") == "storm_encountered"
    )

    if stop_event is None:
        for event in new_history:
            event_type = event.get("event")
            if event_type in {"washed_ashore", "lost_at_sea"}:
                stop_event = event_type
                break

    nearest_coast_miles = engine.nearest_coast_distance_miles(
        bottle["latitude"],
        bottle["longitude"],
    )

    result = {
        "bottle": bottle,
        "historical_year": HISTORICAL_YEAR,
        "days_advanced": days_advanced,
        "stop_event": stop_event,
        "storm_encounters": storm_encounters,
        "coast_rolls_this_run": (
            bottle.get("coast_roll_count", 0) - start_coast_rolls
        ),
        "nearest_coast_miles": (
            round(nearest_coast_miles, 2)
            if nearest_coast_miles is not None
            else None
        ),
    }

    print(json.dumps(result))


if __name__ == "__main__":
    main()
