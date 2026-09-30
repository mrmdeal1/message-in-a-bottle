from datetime import datetime, timedelta
import random

from bottle_engine import bottle_is_mature

WALK_DURATION_MINUTES = 10
DAILY_WALK_LIMIT = 3
BOTTLE_FIND_ODDS = 10000
BEACH_BOTTLE_RADIUS_MILES = 0.25


def bottle_find_roll():
    return random.randint(1, BOTTLE_FIND_ODDS) == 1


def start_beach_walk(user_state, beach_id, current_time):
    now = datetime.fromisoformat(current_time)
    today = now.date().isoformat()

    if user_state.get("beach_walk_date") != today:
        user_state["beach_walk_date"] = today
        user_state["beach_walks_used"] = 0
        user_state.pop("active_beach_walk", None)

    active_walk = user_state.get("active_beach_walk")

    if active_walk is not None:
        active_end = datetime.fromisoformat(
            active_walk["end_time"]
        )

        if now < active_end:
            return {
                "started": False,
                "reason": "walk_already_active",
                "end_time": active_walk["end_time"],
                "walks_remaining": (
                    DAILY_WALK_LIMIT
                    - user_state.get("beach_walks_used", 0)
                ),
            }

        user_state.pop("active_beach_walk", None)

    walks_used = user_state.get("beach_walks_used", 0)

    if walks_used >= DAILY_WALK_LIMIT:
        return {
            "started": False,
            "reason": "daily_limit_reached",
            "walks_remaining": 0,
        }

    user_state["beach_walks_used"] = walks_used + 1

    end_time = (
        now + timedelta(minutes=WALK_DURATION_MINUTES)
    ).isoformat()

    walk = {
        "started": True,
        "beach_id": beach_id,
        "start_time": current_time,
        "end_time": end_time,
        "duration_minutes": WALK_DURATION_MINUTES,
        "walk_number": user_state["beach_walks_used"],
        "walks_remaining": (
            DAILY_WALK_LIMIT
            - user_state["beach_walks_used"]
        ),
        "bottle_found": False,
    }

    user_state["active_beach_walk"] = walk

    return walk


def distance_miles(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, sqrt, atan2

    earth_radius_miles = 3958.7613

    lat1 = radians(lat1)
    lon1 = radians(lon1)
    lat2 = radians(lat2)
    lon2 = radians(lon2)

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        sin(dlat / 2) ** 2
        + cos(lat1)
        * cos(lat2)
        * sin(dlon / 2) ** 2
    )

    c = 2 * atan2(sqrt(a), sqrt(1 - a))

    return earth_radius_miles * c


def bottle_is_at_beach(bottle, beach_lat, beach_lon):
    if bottle.get("status") != "ashore":
        return False

    distance = distance_miles(
        beach_lat,
        beach_lon,
        bottle["latitude"],
        bottle["longitude"],
    )

    return distance <= BEACH_BOTTLE_RADIUS_MILES


def discover_bottle_on_walk(
    bottles,
    beach_lat,
    beach_lon,
    current_time=None,
):
    eligible = [
        bottle
        for bottle in bottles
        if bottle_is_at_beach(
            bottle,
            beach_lat,
            beach_lon,
        )
        and bottle_is_mature(
            bottle,
            check_time=current_time,
        )
    ]

    if not eligible:
        return None

    if not bottle_find_roll():
        return None

    return random.choice(eligible)


def resolve_beach_walk(
    user_state,
    bottles,
    beach_lat,
    beach_lon,
    current_time,
    finder_id=None,
):
    walk = user_state.get("active_beach_walk")

    if walk is None:
        return {
            "resolved": False,
            "reason": "no_active_walk",
        }

    if walk.get("resolved"):
        return {
            "resolved": False,
            "reason": "walk_already_resolved",
            "bottle_found": walk.get("bottle_found", False),
        }

    if not finder_id:
        return {
            "resolved": False,
            "reason": "finder_id_required",
        }

    now = datetime.fromisoformat(current_time)
    end_time = datetime.fromisoformat(walk["end_time"])

    if now < end_time:
        return {
            "resolved": False,
            "reason": "walk_still_active",
            "end_time": walk["end_time"],
        }

    bottle = discover_bottle_on_walk(
        bottles,
        beach_lat,
        beach_lon,
        current_time=current_time,
    )

    if bottle is not None:
        import bottle_engine

        result = bottle_engine.encounter_bottle(
            bottle,
            finder_id=finder_id,
            event_time=current_time,
        )

        if isinstance(result, str):
            bottle = None

    walk["resolved"] = True
    walk["resolved_time"] = current_time
    walk["bottle_found"] = bottle is not None
    walk["bottle_id"] = (
        bottle.get("bottle_id")
        if bottle is not None
        else None
    )

    return {
        "resolved": True,
        "bottle_found": bottle is not None,
        "bottle": bottle,
    }
