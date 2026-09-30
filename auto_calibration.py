import csv
import glob
import json
import math
import os
import random
import sys
import types

import numpy as np
import xarray as xr

# Production imports copernicusmarine, but local calibration reads downloaded NetCDF.
if "copernicusmarine" not in sys.modules:
    sys.modules["copernicusmarine"] = types.ModuleType("copernicusmarine")

import bottle_engine as engine

DATA_GLOB = "copernicus_data/2025/currents_2025_*.nc"
RESULT_ROOT = "local_results/auto"
RUN_DIR = os.path.join(RESULT_ROOT, "runs")
SUMMARY_CSV = os.path.join(RESULT_ROOT, "summary.csv")
SURFACE_DEPTH = 0.49402499198913574
CURRENT_FALLBACK_CELLS = 6


def bounds_for_ds(ds):
    return (
        float(ds["latitude"].min()),
        float(ds["latitude"].max()),
        float(ds["longitude"].min()),
        float(ds["longitude"].max()),
    )


def inside(ds, lat, lon):
    lat_min, lat_max, lon_min, lon_max = bounds_for_ds(ds)
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def load_local_year():
    paths = sorted(glob.glob(DATA_GLOB))
    if not paths:
        raise SystemExit(f"No local current files found: {DATA_GLOB}")

    datasets = []
    day_sources = {}

    for path in paths:
        ds = xr.open_dataset(path)
        datasets.append((path, ds))
        for raw_time in ds["time"].values:
            day = str(raw_time)[:10]
            day_sources.setdefault(day, []).append((path, ds))

    days = sorted(day_sources)
    if not days:
        raise SystemExit("Local current files contain no dates.")

    return datasets, day_sources, days


def close_datasets(datasets):
    for _path, ds in datasets:
        ds.close()


def point_current(ds, day, lat, lon):
    """Return nearest usable current, tolerating masked coastal/shallow cells."""
    snapshot = ds.sel(time=day, depth=SURFACE_DEPTH, method="nearest")
    point = snapshot.sel(latitude=lat, longitude=lon, method="nearest")
    u = float(point["uo"].values.squeeze())
    v = float(point["vo"].values.squeeze())

    if math.isfinite(u) and math.isfinite(v):
        return u, v

    lat_values = np.asarray(snapshot["latitude"].values, dtype=float)
    lon_values = np.asarray(snapshot["longitude"].values, dtype=float)
    lat_center = int(np.abs(lat_values - lat).argmin())
    lon_center = int(np.abs(lon_values - lon).argmin())

    for radius in range(1, CURRENT_FALLBACK_CELLS + 1):
        lat_start = max(0, lat_center - radius)
        lat_stop = min(len(lat_values), lat_center + radius + 1)
        lon_start = max(0, lon_center - radius)
        lon_stop = min(len(lon_values), lon_center + radius + 1)

        u_block = np.asarray(snapshot["uo"].isel(
            latitude=slice(lat_start, lat_stop),
            longitude=slice(lon_start, lon_stop),
        ).values, dtype=float).squeeze()
        v_block = np.asarray(snapshot["vo"].isel(
            latitude=slice(lat_start, lat_stop),
            longitude=slice(lon_start, lon_stop),
        ).values, dtype=float).squeeze()

        finite = np.isfinite(u_block) & np.isfinite(v_block)
        if not np.any(finite):
            continue

        candidates = np.argwhere(finite)
        best = None
        best_distance = float("inf")
        lon_scale = max(abs(math.cos(math.radians(lat))), 0.1)

        for row, col in candidates:
            global_row = lat_start + int(row)
            global_col = lon_start + int(col)
            candidate_lat = lat_values[global_row]
            candidate_lon = lon_values[global_col]
            distance = math.hypot(
                candidate_lat - lat,
                (candidate_lon - lon) * lon_scale,
            )
            if distance < best_distance:
                best_distance = distance
                best = (float(u_block[row, col]), float(v_block[row, col]))

        if best is not None:
            return best

    return float("nan"), float("nan")


def choose_source(day_sources, day, lat, lon):
    """Choose a local tile containing the bottle with usable current data."""
    for path, ds in day_sources.get(day, []):
        if not inside(ds, lat, lon):
            continue
        try:
            u, v = point_current(ds, day, lat, lon)
        except Exception:
            continue
        if math.isfinite(u) and math.isfinite(v):
            return path, ds, u, v
    return None, None, float("nan"), float("nan")


def random_ocean_start(first_day, first_sources, max_tries=20000):
    if not first_sources:
        raise RuntimeError("No current tiles are available for the first historical day.")

    for _ in range(max_tries):
        _path, ds = random.choice(first_sources)
        lat = float(random.choice(ds["latitude"].values))
        lon = float(random.choice(ds["longitude"].values))

        if engine.point_is_on_land(lat, lon):
            continue

        try:
            u, v = point_current(ds, first_day, lat, lon)
        except Exception:
            continue

        if math.isfinite(u) and math.isfinite(v):
            return lat, lon

    raise RuntimeError("Could not find a valid random ocean starting point.")


def write_summary_row(record):
    os.makedirs(RESULT_ROOT, exist_ok=True)
    exists = os.path.exists(SUMMARY_CSV)
    fields = [
        "run_number", "bottle_id", "start_latitude", "start_longitude",
        "end_latitude", "end_longitude", "stop_event", "status",
        "replayed_years", "days_simulated", "total_miles", "coast_rolls",
        "storm_encounters", "nearest_coast_miles", "regions",
    ]
    with open(SUMMARY_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({key: record.get(key) for key in fields})


def save_run(record):
    os.makedirs(RUN_DIR, exist_ok=True)
    path = os.path.join(RUN_DIR, f"run_{record['run_number']:06d}.json")
    with open(path, "w") as f:
        json.dump(record, f, indent=2)
    write_summary_row(record)
    return path


def simulate_one(run_number, day_sources, days):
    first_day = days[0]
    start_lat, start_lon = random_ocean_start(first_day, day_sources[first_day])

    bottle = engine.create_bottle(
        start_lat,
        start_lon,
        start_time=f"{first_day}T00:00:00+00:00",
        sender_id="auto-calibration",
        message="Automated calibration bottle",
    )

    engine.save_bottle = lambda _bottle: None
    original_move = engine.move_bottle_live
    original_storm = engine.storm_level_for_bottle

    days_simulated = 0
    replayed_years = 0
    stop_event = None
    storm_encounters = 0
    start_history_len = len(bottle.get("journey_history", []))

    try:
        while bottle.get("status") == "drifting" and not bottle.get("opened"):
            replayed_years += 1

            for historical_day in days:
                if bottle.get("status") != "drifting" or bottle.get("opened"):
                    break

                lat = float(bottle["latitude"])
                lon = float(bottle["longitude"])
                _path, ds, _u, _v = choose_source(
                    day_sources, historical_day, lat, lon
                )

                if ds is None:
                    stop_event = "left_local_data_area"
                    break

                def local_move(move_lat, move_lon, _date, hours=24, day=historical_day):
                    _p, source, u, v = choose_source(
                        day_sources, day, move_lat, move_lon
                    )
                    if source is None or not (math.isfinite(u) and math.isfinite(v)):
                        raise ValueError(
                            f"No usable local current near {move_lat:.4f}, {move_lon:.4f} on {day}"
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

                def local_storm(storm_lat, storm_lon, _journey_time, day=historical_day):
                    return original_storm(
                        storm_lat,
                        storm_lon,
                        f"{day}T00:00:00+00:00",
                    )

                engine.move_bottle_live = local_move
                engine.storm_level_for_bottle = local_storm

                previous_history_len = len(bottle.get("journey_history", []))
                bottle = engine.advance_bottle(bottle, total_hours=24, step_hours=24)
                days_simulated += 1

                for event in bottle.get("journey_history", [])[previous_history_len:]:
                    event_type = event.get("event")
                    if event_type == "storm_encountered":
                        storm_encounters += 1
                    if event_type in {"washed_ashore", "lost_at_sea"}:
                        stop_event = event_type

                if stop_event or bottle.get("status") != "drifting":
                    break

            if stop_event:
                break

    finally:
        engine.move_bottle_live = original_move
        engine.storm_level_for_bottle = original_storm

    nearest = engine.nearest_coast_distance_miles(
        bottle["latitude"], bottle["longitude"]
    )

    new_history = bottle.get("journey_history", [])[start_history_len:]
    if stop_event is None:
        for event in reversed(new_history):
            if event.get("event") in {"washed_ashore", "lost_at_sea"}:
                stop_event = event.get("event")
                break

    return {
        "run_number": run_number,
        "bottle_id": bottle.get("bottle_id"),
        "start_latitude": round(start_lat, 6),
        "start_longitude": round(start_lon, 6),
        "end_latitude": round(float(bottle["latitude"]), 6),
        "end_longitude": round(float(bottle["longitude"]), 6),
        "stop_event": stop_event,
        "status": bottle.get("status"),
        "replayed_years": replayed_years,
        "days_simulated": days_simulated,
        "total_miles": round(float(bottle.get("total_miles_traveled", 0.0)), 2),
        "coast_rolls": bottle.get("coast_roll_count", 0),
        "storm_encounters": storm_encounters,
        "nearest_coast_miles": round(nearest, 2) if nearest is not None else None,
        "regions": " -> ".join(bottle.get("journey_areas", [])),
        "bottle": bottle,
    }


def main():
    requested_runs = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    if requested_runs < 0:
        raise SystemExit("Run count must be 0 or greater. Use 0 for continuous mode.")

    os.makedirs(RUN_DIR, exist_ok=True)
    datasets, day_sources, days = load_local_year()

    print("AUTO CALIBRATION READY")
    print(f"Historical data: {days[0]} through {days[-1]} ({len(days)} days)")
    print(f"Local current files loaded: {len(datasets)}")

    if days[0] != "2025-01-01" or days[-1] != "2025-12-31" or len(days) != 365:
        close_datasets(datasets)
        raise SystemExit(
            "Full 2025 local data is not complete yet. Need Jan 1 through Dec 31 (365 days)."
        )

    run_number = 1
    try:
        while requested_runs == 0 or run_number <= requested_runs:
            record = simulate_one(run_number, day_sources, days)
            path = save_run(record)
            print(
                f"Run {run_number}: {record['stop_event']} | "
                f"{record['days_simulated']} days | "
                f"{record['total_miles']} miles | "
                f"{record['replayed_years']} year pass(es)"
            )
            print(f"  Start: {record['start_latitude']}, {record['start_longitude']}")
            print(f"  End:   {record['end_latitude']}, {record['end_longitude']}")
            print(f"  Saved: {path}")
            run_number += 1
    except KeyboardInterrupt:
        print("\nAuto calibration stopped by user.")
    finally:
        close_datasets(datasets)

    print(f"Summary: {SUMMARY_CSV}")


if __name__ == "__main__":
    main()
