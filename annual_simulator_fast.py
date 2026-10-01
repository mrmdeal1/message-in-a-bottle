import json
import sys
from datetime import datetime, timedelta, timezone

import bottle_engine as engine
import fast_current_engine as currents
import fast_geometry as geometry


HISTORICAL_YEAR = 2025


def as_utc_iso(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def simulate_bottle(bottle):
    # Simulator only: don't write test state back to storage.
    engine.save_bottle = lambda _bottle: None

    # Fast indexed geometry used only by this simulator.
    engine.nearest_coast_distance_miles = geometry.nearest_coast_distance_miles
    engine.nearest_coast_id = geometry.nearest_coast_id
    engine.movement_path_coast_distance = geometry.movement_path_coast_distance
    engine.movement_path_coast_event = geometry.movement_path_coast_event
    engine.point_is_on_land = geometry.point_is_on_land
    engine.movement_path_hits_land = geometry.movement_path_hits_land
    engine.first_land_contact = geometry.first_land_contact

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

            def fast_move(
                lat,
                lon,
                _date,
                hours=24,
                historical_date=historical_date,
            ):
                return currents.move_bottle(
                    lat,
                    lon,
                    historical_date,
                    hours,
                )

            def historical_storm(
                lat,
                lon,
                _journey_time,
                historical_dt=historical_dt,
            ):
                return original_storm(
                    lat,
                    lon,
                    as_utc_iso(historical_dt),
                )

            engine.move_bottle_live = fast_move
            engine.storm_level_for_bottle = historical_storm

            previous_history_len = len(
                bottle.get("journey_history", [])
            )

            bottle = engine.advance_bottle(
                bottle,
                total_hours=24,
                step_hours=24,
            )

            days_advanced += 1

            for event in bottle.get(
                "journey_history", []
            )[previous_history_len:]:
                event_type = event.get("event")

                if event_type in {
                    "washed_ashore",
                    "lost_at_sea",
                }:
                    stop_event = event_type
                    break

            if stop_event or bottle.get("status") != "drifting":
                break

    finally:
        engine.move_bottle_live = original_move
        engine.storm_level_for_bottle = original_storm

    new_history = bottle.get(
        "journey_history", []
    )[start_history_len:]

    storm_encounters = sum(
        1
        for event in new_history
        if event.get("event") == "storm_encountered"
    )

    if stop_event is None:
        for event in new_history:
            event_type = event.get("event")

            if event_type in {
                "washed_ashore",
                "lost_at_sea",
            }:
                stop_event = event_type
                break

    nearest_coast_miles = (
        engine.nearest_coast_distance_miles(
            bottle["latitude"],
            bottle["longitude"],
        )
    )

    result = {
        "bottle": bottle,
        "historical_year": HISTORICAL_YEAR,
        "days_advanced": days_advanced,
        "stop_event": stop_event,
        "storm_encounters": storm_encounters,
        "coast_rolls_this_run": (
            bottle.get("coast_roll_count", 0)
            - start_coast_rolls
        ),
        "nearest_coast_miles": (
            round(nearest_coast_miles, 2)
            if nearest_coast_miles is not None
            else None
        ),
        "current_engine": "local_tiles_fast",
    }

    return result


def main():
    bottle = json.load(sys.stdin)

    try:
        result = simulate_bottle(bottle)
        print(json.dumps(result))
    finally:
        currents.close_all()


if __name__ == "__main__":
    main()
