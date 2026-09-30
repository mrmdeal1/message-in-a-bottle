import json
import math
import os
import sys
import types
from datetime import datetime, timezone

import xarray as xr

# The production engine imports copernicusmarine at module import time.
# Local calibration does not need that package because currents come from
# downloaded NetCDF files, so provide a harmless placeholder for Python 3.9.
if "copernicusmarine" not in sys.modules:
    sys.modules["copernicusmarine"] = types.ModuleType("copernicusmarine")

import bottle_engine as engine


DEFAULT_FILE = "copernicus_data/2025/currents_2025_01.nc"
RESULT_DIR = "local_results"
SURFACE_DEPTH = 0.49402499198913574


def as_iso(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def main():
    data_file = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FILE

    if not os.path.exists(data_file):
        raise SystemExit(f"Local current file not found: {data_file}")

    os.makedirs(RESULT_DIR, exist_ok=True)

    ds = xr.open_dataset(data_file)

    times = ds["time"].values
    if len(times) == 0:
        raise SystemExit("The local current file contains no dates.")

    first_day = str(times[0])[:10]
    last_day = str(times[-1])[:10]

    # Fresh calibration bottle. This does not modify the app's saved bottle.
    bottle = engine.create_bottle(
        29.0,
        -88.0,
        start_time=f"{first_day}T00:00:00+00:00",
        sender_id="local-calibration",
        message="Local calibration bottle",
    )

    engine.save_bottle = lambda _bottle: None

    original_move = engine.move_bottle_live
    original_storm = engine.storm_level_for_bottle

    start_history_len = len(bottle.get("journey_history", []))
    start_coast_rolls = bottle.get("coast_roll_count", 0)
    days_advanced = 0
    stop_event = None

    lat_min = float(ds["latitude"].min())
    lat_max = float(ds["latitude"].max())
    lon_min = float(ds["longitude"].min())
    lon_max = float(ds["longitude"].max())

    try:
        for raw_time in times:
            if bottle.get("opened") or bottle.get("status") != "drifting":
                break

            historical_date = str(raw_time)[:10]

            lat = float(bottle["latitude"])
            lon = float(bottle["longitude"])

            if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
                stop_event = "left_local_data_area"
                break

            def local_move(move_lat, move_lon, _date, hours=24, historical_date=historical_date):
                point = ds.sel(
                    time=historical_date,
                    depth=SURFACE_DEPTH,
                    latitude=move_lat,
                    longitude=move_lon,
                    method="nearest",
                )

                u = float(point["uo"].values.squeeze())
                v = float(point["vo"].values.squeeze())

                if not math.isfinite(u) or not math.isfinite(v):
                    raise ValueError(
                        f"No usable local current at {move_lat:.4f}, {move_lon:.4f} on {historical_date}"
                    )

                seconds = hours * 3600.0
                east_m = u * seconds
                north_m = v * seconds

                new_lat = move_lat + north_m / 111320.0
                cos_lat = math.cos(math.radians(move_lat))
                if abs(cos_lat) < 1e-6:
                    cos_lat = 1e-6
                new_lon = move_lon + east_m / (111320.0 * cos_lat)
                miles = math.hypot(east_m, north_m) / 1609.344

                return new_lat, new_lon, miles

            def local_storm(storm_lat, storm_lon, _journey_time, historical_date=historical_date):
                return original_storm(
                    storm_lat,
                    storm_lon,
                    f"{historical_date}T00:00:00+00:00",
                )

            engine.move_bottle_live = local_move
            engine.storm_level_for_bottle = local_storm

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
        ds.close()

    new_history = bottle.get("journey_history", [])[start_history_len:]
    storm_encounters = sum(
        1 for event in new_history if event.get("event") == "storm_encountered"
    )

    nearest_coast = engine.nearest_coast_distance_miles(
        bottle["latitude"],
        bottle["longitude"],
    )

    result = {
        "data_file": data_file,
        "data_start": first_day,
        "data_end": last_day,
        "days_advanced": days_advanced,
        "stop_event": stop_event,
        "status": bottle.get("status"),
        "latitude": round(float(bottle["latitude"]), 6),
        "longitude": round(float(bottle["longitude"]), 6),
        "total_miles_traveled": round(float(bottle.get("total_miles_traveled", 0.0)), 2),
        "journey_areas": bottle.get("journey_areas", []),
        "coast_rolls_this_run": bottle.get("coast_roll_count", 0) - start_coast_rolls,
        "coast_roll_count": bottle.get("coast_roll_count", 0),
        "nearest_coast_miles": round(nearest_coast, 2) if nearest_coast is not None else None,
        "storm_encounters": storm_encounters,
        "bottle": bottle,
    }

    result_path = os.path.join(RESULT_DIR, "latest.json")
    with open(result_path, "w") as f:
        json.dump(result, f, indent=2)

    print("LOCAL CALIBRATION COMPLETE")
    print(f"Data: {first_day} through {last_day}")
    print(f"Days simulated: {days_advanced}")
    print(f"Status: {bottle.get('status')}")
    print(f"Stop event: {stop_event}")
    print(f"Miles traveled: {result['total_miles_traveled']}")
    print(f"Position: {result['latitude']}, {result['longitude']}")
    print(f"Regions: {', '.join(result['journey_areas'])}")
    print(f"Coast rolls: {result['coast_rolls_this_run']}")
    print(f"Nearest coast: {result['nearest_coast_miles']} miles")
    print(f"Storm encounters: {storm_encounters}")
    print(f"Full result saved to: {result_path}")


if __name__ == "__main__":
    main()
