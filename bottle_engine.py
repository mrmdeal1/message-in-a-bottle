import math
import random
import uuid
import json
import os
from datetime import datetime, timedelta
import copernicusmarine
import shapefile
import rasterio
from shapely.geometry import Point, shape
from storm_detector import storm_level_for_bottle

DATASET_ID = "cmems_mod_glo_phy-cur_anfc_0.083deg_P1D-m"
SURFACE_DEPTH = 0.49402499198913574
SAVE_FILE = "bottle_state.json"

SHIPPING_RASTER = "shipping_data/shipdensity_global.tif"
_SHIPPING_SRC = None


def shipping_density_at(lat, lon):
    global _SHIPPING_SRC

    if _SHIPPING_SRC is None:
        _SHIPPING_SRC = rasterio.open(SHIPPING_RASTER)

    try:
        value = next(_SHIPPING_SRC.sample([(lon, lat)]))[0]
    except Exception:
        return 0

    if value == _SHIPPING_SRC.nodata or value < 0:
        return 0

    return int(value)


def shipping_traffic_multiplier(density):
    if density <= 1000:
        return 1.0, "none"
    if density <= 1_000_000:
        return 3.0, "light"
    if density <= 6_000_000:
        return 8.0, "moderate"
    if density <= 20_000_000:
        return 20.0, "heavy"
    if density <= 30_000_000:
        return 40.0, "very_heavy"

    return 80.0, "extreme"


MARINE_SHP = (
    "ne_10m_geography_marine_polys/"
    "ne_10m_geography_marine_polys.shp"
)


def load_marine_regions():
    reader = shapefile.Reader(MARINE_SHP)

    fields = [
        field[0]
        for field in reader.fields
        if field[0] != "DeletionFlag"
    ]

    regions = []

    for sr in reader.iterShapeRecords():
        data = dict(zip(fields, sr.record))
        name = data.get("name")

        if not name:
            continue

        geom = shape(sr.shape.__geo_interface__)

        if geom.is_empty or not geom.is_valid:
            continue

        regions.append({
            "name": name,
            "featurecla": data.get("featurecla"),
            "geometry": geom,
            "bounds": geom.bounds,
            "area": geom.area,
        })

    # If marine polygons ever overlap, prefer the
    # smaller/more geographically specific region.
    regions.sort(key=lambda item: item["area"])

    return regions


MARINE_REGIONS = load_marine_regions()


def broad_region(lat, lon):
    point = Point(lon, lat)

    for region in MARINE_REGIONS:
        minx, miny, maxx, maxy = region["bounds"]

        if not (
            minx <= lon <= maxx
            and miny <= lat <= maxy
        ):
            continue

        if region["geometry"].covers(point):
            return region["name"]

    return "Open Ocean"


def create_bottle(
    lat,
    lon,
    start_time="2026-09-30T00:00:00",
    sender_id=None,
    message=None,
):
    maturity_days = random.randint(7, 30)
    eligible_after = (
        datetime.fromisoformat(start_time)
        + timedelta(days=maturity_days)
    ).isoformat()

    return {
        "bottle_id": str(uuid.uuid4()),
        "latitude": lat,
        "longitude": lon,
        "total_miles_traveled": 0.0,
        "status": "drifting",
        "opened": False,
        "journey_areas": [broad_region(lat, lon)],
        "current_time": start_time,
        "launch_time": start_time,
        "sender_id": sender_id,
        "message": message,
        "reply_message": None,
        "maturity_days": maturity_days,
        "eligible_after": eligible_after,
        "journey_history": [
            {
                "event": "launched",
                "time": start_time,
                "latitude": lat,
                "longitude": lon,
                "miles": 0.0,
            }
        ],
    }


def add_journey_event(
    bottle,
    event,
    event_time=None,
    **details,
):
    history = bottle.setdefault("journey_history", [])

    entry = {
        "event": event,
        "time": (
            event_time
            if event_time is not None
            else bottle.get("current_time")
        ),
        "latitude": bottle.get("latitude"),
        "longitude": bottle.get("longitude"),
        "miles": round(
            bottle.get("total_miles_traveled", 0.0),
            2,
        ),
    }

    entry.update(details)
    history.append(entry)

    return entry


def save_bottle(bottle):
    with open(SAVE_FILE, "w") as f:
        json.dump(bottle, f, indent=2)


def load_bottle():
    if not os.path.exists(SAVE_FILE):
        return None

    with open(SAVE_FILE, "r") as f:
        bottle = json.load(f)

    if "current_time" not in bottle:
        bottle["current_time"] = "2026-09-30T00:00:00"

    if "bottle_id" not in bottle:
        bottle["bottle_id"] = str(uuid.uuid4())
        save_bottle(bottle)

    return bottle


def get_currents(lat, lon, date):
    return copernicusmarine.open_dataset(
        dataset_id=DATASET_ID,
        variables=["uo", "vo"],
        minimum_longitude=lon - 0.1,
        maximum_longitude=lon + 0.1,
        minimum_latitude=lat - 0.1,
        maximum_latitude=lat + 0.1,
        start_datetime=date,
        end_datetime=date,
        minimum_depth=SURFACE_DEPTH,
        maximum_depth=SURFACE_DEPTH,
    )


def move_bottle_live(lat, lon, date, hours=6):
    local_currents = get_currents(lat, lon, date)

    point = local_currents.sel(
        latitude=lat,
        longitude=lon,
        method="nearest",
    )

    u = float(point["uo"].values.squeeze())
    v = float(point["vo"].values.squeeze())

    seconds = hours * 3600
    east_m = u * seconds
    north_m = v * seconds

    new_lat = lat + north_m / 111320
    new_lon = lon + east_m / (
        111320 * math.cos(math.radians(lat))
    )

    miles = math.hypot(east_m, north_m) / 1609.344

    return new_lat, new_lon, miles




def public_journey_history(bottle):
    public_history = []

    safe_fields = (
        "region",
        "previous_region",
        "storm_name",
        "storm_level",
        "wind_band",
        "distance_nm",
        "wind_knots",
        "category",
    )

    for raw_event in bottle.get("journey_history", []):
        event = {
            "event": raw_event.get("event"),
            "time": raw_event.get("time"),
            "miles": raw_event.get("miles"),
        }

        for field in safe_fields:
            if raw_event.get(field) is not None:
                event[field] = raw_event[field]

        # Give the launch event a useful geographic name
        # without exposing its exact coordinates.
        if (
            event["event"] == "launched"
            and "region" not in event
        ):
            areas = bottle.get("journey_areas", [])

            if areas:
                event["region"] = areas[0]

        public_history.append(event)

    return public_history


def public_journey_story(bottle):
    story = []

    for event in public_journey_history(bottle):
        event_type = event.get("event")
        miles = event.get("miles", 0.0)

        if event_type == "launched":
            region = event.get("region", "the ocean")
            story.append(
                f"Launched in {region}."
            )

        elif event_type == "region_entered":
            region = event.get("region", "a new region")
            story.append(
                f"Entered {region} after {miles:.2f} miles."
            )

        elif event_type == "storm_encountered":
            name = event.get("storm_name", "a storm")
            level = event.get("storm_level", "storm")
            wind_band = event.get("wind_band")

            text = (
                f"Encountered {name} "
                f"with {level} conditions"
            )

            if wind_band:
                text += (
                    f" in the {wind_band.replace('_', '-')} "
                    f"wind band"
                )

            text += "."
            story.append(text)

        elif event_type == "washed_ashore":
            story.append(
                f"Washed ashore after {miles:.2f} miles."
            )

        elif event_type == "found":
            story.append("Found on the beach.")

        elif event_type == "opened":
            story.append(
                "Opened. The bottle's journey ended here."
            )

        elif event_type == "thrown_back":
            story.append(
                "Found unopened and thrown back into the ocean."
            )

        elif event_type == "lost_at_sea":
            story.append(
                f"Lost at sea after {miles:.2f} miles."
            )

        elif event_type == "reply_sent":
            story.append(
                "The finder sent a reply to the original sender."
            )

    return story


def finder_view(bottle):
    opened = bottle.get("opened", False)

    return {
        "status": bottle["status"],
        "message": (
            bottle.get("message")
            if opened
            else None
        ),
        "miles_traveled": (
            round(
                bottle.get(
                    "total_miles_traveled",
                    0.0,
                ),
                2,
            )
            if opened
            else None
        ),
        "journey_areas": (
            list(bottle.get("journey_areas", []))
            if opened
            else None
        ),
        "journey_history": (
            public_journey_history(bottle)
            if opened
            else None
        ),
        "journey_story": (
            public_journey_story(bottle)
            if opened
            else None
        ),
    }


def sender_view(bottle):
    return {
        "status": bottle["status"],
        "miles_traveled": (
            round(bottle["total_miles_traveled"], 2)
            if bottle["opened"]
            else None
        ),
        "journey_areas": (
            bottle["journey_areas"]
            if bottle["opened"]
            else None
        ),
        "reply_message": bottle.get("reply_message"),
        "reply_time": bottle.get("reply_time"),
    }


if __name__ == "__main__":
    bottle = load_bottle()

    if bottle is None:
        bottle = create_bottle(29.0, -88.0)
        save_bottle(bottle)
        print("Created new bottle.")
    else:
        print("Loaded existing bottle.")

    print(bottle)

def advance_bottle(bottle, total_hours=24, step_hours=6):
    steps = total_hours // step_hours

    for _ in range(steps):
        if bottle["opened"]:
            break
        update_bottle(bottle, step_hours)

    return bottle

def bottle_is_mature(bottle, check_time=None):
    eligible_after = bottle.get("eligible_after")

    if eligible_after is None:
        return True

    comparison_time = (
        check_time
        if check_time is not None
        else bottle.get("current_time")
    )

    if comparison_time is None:
        return False

    return (
        datetime.fromisoformat(comparison_time)
        >= datetime.fromisoformat(eligible_after)
    )


def encounter_bottle(
    bottle,
    finder_id=None,
    event_time=None,
):
    if bottle["opened"]:
        return "Journey already ended."

    if bottle["status"] != "ashore":
        return "Bottle is not available to be found."

    check_time = (
        event_time
        if event_time is not None
        else bottle.get("current_time")
    )

    burial_due_time = bottle.get("burial_due_time")

    if burial_due_time and check_time:
        if datetime.fromisoformat(check_time) >= datetime.fromisoformat(
            burial_due_time
        ):
            bottle["status"] = "lost_at_sea"
            bottle["destroyed"] = True
            bottle["loss_reason"] = "buried_ashore"
            bottle["destruction_time"] = check_time
            bottle["destruction_miles"] = bottle.get(
                "total_miles_traveled",
                0.0,
            )

            add_journey_event(
                bottle,
                "lost_at_sea",
                event_time=check_time,
                reason="buried_ashore",
                ashore_time=bottle.get("ashore_time"),
                burial_due_time=burial_due_time,
            )

            save_bottle(bottle)
            return "Bottle was buried ashore and permanently lost."

    if not bottle_is_mature(
        bottle,
        check_time=event_time,
    ):
        return "Bottle is not yet eligible to be encountered."

    if not bottle.get("finder_eligible", False):
        return "Bottle remains undiscovered."

    if not finder_id:
        return "Finder identity is required."

    bottle["status"] = "encountered"
    bottle["finder_id"] = finder_id
    bottle["encounter_count"] = bottle.get("encounter_count", 0) + 1

    add_journey_event(
        bottle,
        "found",
        event_time=event_time,
        finder_id=finder_id,
    )

    save_bottle(bottle)

    return bottle


def open_bottle(bottle, event_time=None):
    if bottle["status"] != "encountered":
        return "Bottle must be encountered before it can be opened."

    bottle["opened"] = True
    bottle["status"] = "journey_ended"

    add_journey_event(
        bottle,
        "opened",
        event_time=event_time,
        finder_id=bottle.get("finder_id"),
    )

    save_bottle(bottle)
    return bottle


def reply_to_sender(
    bottle,
    reply_message,
    event_time=None,
):
    if not bottle.get("opened"):
        return "Bottle must be opened before replying."

    if bottle.get("status") != "journey_ended":
        return "Bottle journey must be ended before replying."

    if not bottle.get("sender_id"):
        return "Original sender is unavailable."

    reply_time = (
        event_time
        if event_time is not None
        else bottle.get("current_time")
    )

    bottle["reply_message"] = reply_message
    bottle["reply_time"] = reply_time

    add_journey_event(
        bottle,
        "reply_sent",
        event_time=reply_time,
        finder_id=bottle.get("finder_id"),
    )

    save_bottle(bottle)

    return "Reply sent to original sender."


def throw_back(bottle, event_time=None):
    if bottle["opened"]:
        return "Cannot throw back an opened bottle."

    if bottle["status"] != "encountered":
        return "Bottle has not been encountered."

    finder_id = bottle.get("finder_id")

    bottle["status"] = "drifting"

    add_journey_event(
        bottle,
        "thrown_back",
        event_time=event_time,
        finder_id=finder_id,
    )

    if finder_id:
        bottle["last_finder_id"] = finder_id
        bottle.pop("finder_id", None)

    save_bottle(bottle)
    return bottle

import random


def encounter_roll(chance=0.10):
    return random.random() < chance



def coast_roll_allowed(bottle, coast_id):
    rolled = bottle.get("rolled_coasts", [])
    return coast_id not in rolled


def record_coast_roll(bottle, coast_id):
    if "rolled_coasts" not in bottle:
        bottle["rolled_coasts"] = []

    if coast_id not in bottle["rolled_coasts"]:
        bottle["rolled_coasts"].append(coast_id)

    return bottle

import shapefile
from shapely.geometry import Point, shape


COASTLINE_SHP = "ne_10m_coastline/ne_10m_coastline.shp"


def load_coastlines():
    reader = shapefile.Reader(COASTLINE_SHP)
    return [shape(s.__geo_interface__) for s in reader.shapes()]


COASTLINES = load_coastlines()


def nearest_coast_distance_miles(lat, lon):
    point = Point(lon, lat)

    nearest_distance_degrees = min(
        coastline.distance(point)
        for coastline in COASTLINES
    )

    miles_per_degree = 69.0
    return nearest_distance_degrees * miles_per_degree

def nearest_coast_distance_miles(lat, lon):
    point = Point(lon, lat)
    distances = []

    for coastline in COASTLINES:
        if coastline.is_empty:
            continue

        distance = coastline.distance(point)

        if math.isfinite(distance):
            distances.append(distance)

    if not distances:
        return None

    nearest_distance_degrees = min(distances)
    return nearest_distance_degrees * 69.0

import warnings

def nearest_coast_distance_miles(lat, lon):
    point = Point(lon, lat)
    distances = []

    for coastline in COASTLINES:
        if coastline.is_empty or not coastline.is_valid:
            continue

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            distance = coastline.distance(point)

        if math.isfinite(distance):
            distances.append(distance)

    if not distances:
        return None

    return min(distances) * 69.0
COAST_ENCOUNTER_MILES = 0.25

from shapely.ops import nearest_points


def haversine_miles(lat1, lon1, lat2, lon2):
    earth_radius_miles = 3958.7613

    lat1 = math.radians(lat1)
    lon1 = math.radians(lon1)
    lat2 = math.radians(lat2)
    lon2 = math.radians(lon2)

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin(dlon / 2) ** 2
    )

    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return earth_radius_miles * c


def nearest_coast_distance_miles(lat, lon):
    point = Point(lon, lat)
    nearest_coastline = None
    nearest_degree_distance = float("inf")

    for coastline in COASTLINES:
        if coastline.is_empty or not coastline.is_valid:
            continue

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            distance = coastline.distance(point)

        if math.isfinite(distance) and distance < nearest_degree_distance:
            nearest_degree_distance = distance
            nearest_coastline = coastline

    if nearest_coastline is None:
        return None

    _, coast_point = nearest_points(point, nearest_coastline)

    return haversine_miles(
        lat,
        lon,
        coast_point.y,
        coast_point.x,
    )

def within_encounter_range(bottle):
    distance = nearest_coast_distance_miles(
        bottle["latitude"],
        bottle["longitude"],
    )

    if distance is None:
        return False

    return distance <= COAST_ENCOUNTER_MILES

def nearest_coast_id(lat, lon):
    point = Point(lon, lat)
    best_id = None
    best_distance = float("inf")

    for index, coastline in enumerate(COASTLINES):
        if coastline.is_empty or not coastline.is_valid:
            continue

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            distance = coastline.distance(point)

        if math.isfinite(distance) and distance < best_distance:
            best_distance = distance
            best_id = f"coast_{index}"

    return best_id

COAST_RESET_MILES = 25.0

def update_coast_arm_state(bottle):
    distance = nearest_coast_distance_miles(
        bottle["latitude"],
        bottle["longitude"],
    )

    if distance is None:
        return bottle

    if distance >= COAST_RESET_MILES:
        bottle["coast_armed"] = True

    return bottle

def maybe_encounter_real_coast(bottle, chance=0.10, persist=True):
    distance = nearest_coast_distance_miles(
        bottle["latitude"],
        bottle["longitude"],
    )

    if distance is None:
        return bottle

    if distance >= COAST_RESET_MILES:
        bottle["coast_armed"] = True

    if distance > COAST_ENCOUNTER_MILES:
        if persist:
            save_bottle(bottle)
        return bottle

    if not bottle.get("coast_armed", True):
        return bottle

    if not bottle_is_mature(bottle):
        return bottle

    coast_id = nearest_coast_id(
        bottle["latitude"],
        bottle["longitude"],
    )

    if coast_id is None:
        return bottle

    bottle["coast_armed"] = False
    bottle["last_coast_id"] = coast_id
    bottle["coast_roll_count"] = bottle.get("coast_roll_count", 0) + 1

    if encounter_roll(chance):
        bottle["status"] = "ashore"
        bottle["ashore_time"] = bottle.get("current_time")
        bottle["ashore_coast_id"] = coast_id

        ashore_dt = datetime.fromisoformat(bottle["ashore_time"])
        burial_days = random.randint(30, 180)
        bottle["burial_due_time"] = (
            ashore_dt + timedelta(days=burial_days)
        ).isoformat()
        bottle["finder_eligible"] = random.random() < 0.20

        add_journey_event(
            bottle,
            "washed_ashore",
            coast_id=coast_id,
        )

    if persist:
        save_bottle(bottle)

    return bottle




from shapely.geometry import LineString

def movement_path_coast_distance(lat1, lon1, lat2, lon2):
    path = LineString([
        (lon1, lat1),
        (lon2, lat2),
    ])

    best_distance = float("inf")
    best_coast_id = None

    for index, coastline in enumerate(COASTLINES):
        if coastline.is_empty or not coastline.is_valid:
            continue

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            path_point, coast_point = nearest_points(path, coastline)

        distance = haversine_miles(
            path_point.y,
            path_point.x,
            coast_point.y,
            coast_point.x,
        )

        if distance < best_distance:
            best_distance = distance
            best_coast_id = f"coast_{index}"

    if best_coast_id is None:
        return None, None

    return best_distance, best_coast_id


def movement_path_coast_event(lat1, lon1, lat2, lon2):
    path = LineString([
        (lon1, lat1),
        (lon2, lat2),
    ])

    best_distance = float("inf")
    best_coast_id = None
    best_path_point = None

    for index, coastline in enumerate(COASTLINES):
        if coastline.is_empty or not coastline.is_valid:
            continue

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            path_point, coast_point = nearest_points(path, coastline)

        distance = haversine_miles(
            path_point.y,
            path_point.x,
            coast_point.y,
            coast_point.x,
        )

        if distance < best_distance:
            best_distance = distance
            best_coast_id = f"coast_{index}"
            best_path_point = path_point

    if best_coast_id is None:
        return None, None, None, None

    return (
        best_distance,
        best_coast_id,
        best_path_point.y,
        best_path_point.x,
    )

def movement_fraction_to_point(
    start_lat,
    start_lon,
    end_lat,
    end_lon,
    point_lat,
    point_lon,
):
    full_distance = haversine_miles(
        start_lat,
        start_lon,
        end_lat,
        end_lon,
    )

    if full_distance == 0:
        return 0.0

    partial_distance = haversine_miles(
        start_lat,
        start_lon,
        point_lat,
        point_lon,
    )

    fraction = partial_distance / full_distance

    return max(0.0, min(1.0, fraction))




LAND_SHP = "ne_10m_land/ne_10m_land.shp"

def load_land_polygons():
    reader = shapefile.Reader(LAND_SHP)
    return [
        shape(s.__geo_interface__)
        for s in reader.shapes()
        if not shape(s.__geo_interface__).is_empty
    ]

LAND_POLYGONS = load_land_polygons()

def point_is_on_land(lat, lon):
    point = Point(lon, lat)

    for land in LAND_POLYGONS:
        if not land.is_valid:
            continue

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)

            if land.contains(point) or land.touches(point):
                return True

    return False

LAND_SHP = "ne_10m_land/ne_10m_land.shp"

def load_land_polygons():
    reader = shapefile.Reader(LAND_SHP)
    return [
        shape(s.__geo_interface__)
        for s in reader.shapes()
        if not shape(s.__geo_interface__).is_empty
    ]

LAND_POLYGONS = load_land_polygons()

def point_is_on_land(lat, lon):
    point = Point(lon, lat)

    for land in LAND_POLYGONS:
        if not land.is_valid:
            continue

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)

            if land.contains(point) or land.touches(point):
                return True

    return False

def movement_path_hits_land(lat1, lon1, lat2, lon2):
    path = LineString([
        (lon1, lat1),
        (lon2, lat2),
    ])

    for land in LAND_POLYGONS:
        if land.is_empty or not land.is_valid:
            continue

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)

            if path.intersects(land):
                return True

    return False

def first_land_contact(lat1, lon1, lat2, lon2):
    path = LineString([
        (lon1, lat1),
        (lon2, lat2),
    ])

    first_point = None
    first_position = float("inf")

    for land in LAND_POLYGONS:
        if land.is_empty or not land.is_valid:
            continue

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)

            if not path.intersects(land):
                continue

            hit = path.intersection(land.boundary)

        if hit.is_empty:
            continue

        if hit.geom_type == "Point":
            points = [hit]
        elif hasattr(hit, "geoms"):
            points = [
                g for g in hit.geoms
                if g.geom_type == "Point"
            ]
        else:
            points = []

        for point in points:
            position = path.project(point)

            if position < first_position:
                first_position = position
                first_point = point

    if first_point is None:
        return None

    return first_point.y, first_point.x


OFFSHORE_BUFFER_MILES = 0.05

def point_before_land_contact(
    start_lat,
    start_lon,
    hit_lat,
    hit_lon,
    buffer_miles=OFFSHORE_BUFFER_MILES,
):
    distance = haversine_miles(
        start_lat,
        start_lon,
        hit_lat,
        hit_lon,
    )

    if distance <= buffer_miles or distance == 0:
        return start_lat, start_lon

    fraction = (distance - buffer_miles) / distance

    safe_lat = start_lat + (hit_lat - start_lat) * fraction
    safe_lon = start_lon + (hit_lon - start_lon) * fraction

    return safe_lat, safe_lon


def deflect_along_coast(
    start_lat,
    start_lon,
    hit_lat,
    hit_lon,
    attempted_lat,
    attempted_lon,
):
    approach_lat = hit_lat - start_lat
    approach_lon = hit_lon - start_lon

    tangent1_lat = -approach_lon
    tangent1_lon = approach_lat

    tangent2_lat = approach_lon
    tangent2_lon = -approach_lat

    move_lat = attempted_lat - start_lat
    move_lon = attempted_lon - start_lon

    dot1 = tangent1_lat * move_lat + tangent1_lon * move_lon
    dot2 = tangent2_lat * move_lat + tangent2_lon * move_lon

    if dot1 >= dot2:
        tangent_lat = tangent1_lat
        tangent_lon = tangent1_lon
    else:
        tangent_lat = tangent2_lat
        tangent_lon = tangent2_lon

    length = math.hypot(tangent_lat, tangent_lon)

    if length == 0:
        return start_lat, start_lon

    tangent_lat /= length
    tangent_lon /= length

    attempted_distance = haversine_miles(
        start_lat,
        start_lon,
        attempted_lat,
        attempted_lon,
    )

    miles_per_lat_degree = 69.0
    miles_per_lon_degree = 69.0 * math.cos(math.radians(hit_lat))

    new_lat = hit_lat + (
        tangent_lat * attempted_distance / miles_per_lat_degree
    )

    new_lon = hit_lon + (
        tangent_lon * attempted_distance / miles_per_lon_degree
    )

    return new_lat, new_lon

def local_coast_direction(coast_id, hit_lat, hit_lon):
    index = int(coast_id.split("_")[1])
    coastline = COASTLINES[index]

    point = Point(hit_lon, hit_lat)

    if coastline.geom_type == "MultiLineString":
        coastline = min(
            coastline.geoms,
            key=lambda g: g.distance(point),
        )

    position = coastline.project(point)

    small_step = max(coastline.length * 0.0001, 0.00001)

    before = coastline.interpolate(
        max(0.0, position - small_step)
    )
    after = coastline.interpolate(
        min(coastline.length, position + small_step)
    )

    lat_direction = after.y - before.y
    lon_direction = after.x - before.x

    return lat_direction, lon_direction

def slide_along_coast(
    coast_id,
    hit_lat,
    hit_lon,
    attempted_lat,
    attempted_lon,
    slide_miles=1.0,
):
    lat_dir, lon_dir = local_coast_direction(
        coast_id,
        hit_lat,
        hit_lon,
    )

    move_lat = attempted_lat - hit_lat
    move_lon = attempted_lon - hit_lon

    dot = lat_dir * move_lat + lon_dir * move_lon

    if dot < 0:
        lat_dir = -lat_dir
        lon_dir = -lon_dir

    length = math.hypot(lat_dir, lon_dir)

    if length == 0:
        return hit_lat, hit_lon

    lat_dir /= length
    lon_dir /= length

    miles_per_lat_degree = 69.0
    miles_per_lon_degree = 69.0 * math.cos(math.radians(hit_lat))

    new_lat = hit_lat + (
        lat_dir * slide_miles / miles_per_lat_degree
    )

    new_lon = hit_lon + (
        lon_dir * slide_miles / miles_per_lon_degree
    )

    return new_lat, new_lon

def slide_along_coast_safe(
    coast_id,
    hit_lat,
    hit_lon,
    water_lat,
    water_lon,
    attempted_lat,
    attempted_lon,
    slide_miles=1.0,
):
    coast_lat, coast_lon = slide_along_coast(
        coast_id,
        hit_lat,
        hit_lon,
        attempted_lat,
        attempted_lon,
        slide_miles,
    )

    distance_to_water = haversine_miles(
        coast_lat,
        coast_lon,
        water_lat,
        water_lon,
    )

    if distance_to_water == 0:
        return coast_lat, coast_lon

    fraction = min(
        1.0,
        OFFSHORE_BUFFER_MILES / distance_to_water,
    )

    safe_lat = coast_lat + (
        water_lat - coast_lat
    ) * fraction

    safe_lon = coast_lon + (
        water_lon - coast_lon
    ) * fraction

    return safe_lat, safe_lon

def coast_slide_point(
    coast_id,
    hit_lat,
    hit_lon,
    attempted_lat,
    attempted_lon,
    slide_miles=1.0,
):
    index = int(coast_id.split("_")[1])
    coastline = COASTLINES[index]

    hit = Point(hit_lon, hit_lat)

    if coastline.geom_type == "MultiLineString":
        coastline = min(
            coastline.geoms,
            key=lambda g: g.distance(hit),
        )

    position = coastline.project(hit)

    lat_dir, lon_dir = local_coast_direction(
        coast_id,
        hit_lat,
        hit_lon,
    )

    move_lat = attempted_lat - hit_lat
    move_lon = attempted_lon - hit_lon

    direction = 1 if (
        lat_dir * move_lat + lon_dir * move_lon
    ) >= 0 else -1

    length = math.hypot(lat_dir, lon_dir)

    if length == 0:
        return hit_lat, hit_lon

    lat_unit = lat_dir / length
    lon_unit = lon_dir / length

    miles_per_degree = math.sqrt(
        (lat_unit * 69.0) ** 2
        + (
            lon_unit
            * 69.0
            * math.cos(math.radians(hit_lat))
        ) ** 2
    )

    degree_step = slide_miles / miles_per_degree

    new_position = position + direction * degree_step
    new_position = max(
        0.0,
        min(coastline.length, new_position),
    )

    coast_point = coastline.interpolate(new_position)

    return coast_point.y, coast_point.x

def offset_coast_point_to_water(
    coast_lat,
    coast_lon,
    water_lat,
    water_lon,
    buffer_miles=OFFSHORE_BUFFER_MILES,
):
    distance = haversine_miles(
        coast_lat,
        coast_lon,
        water_lat,
        water_lon,
    )

    if distance == 0:
        return coast_lat, coast_lon

    fraction = min(
        1.0,
        buffer_miles / distance,
    )

    safe_lat = coast_lat + (
        water_lat - coast_lat
    ) * fraction

    safe_lon = coast_lon + (
        water_lon - coast_lon
    ) * fraction

    return safe_lat, safe_lon

def offset_coast_point_to_water_safe(
    coast_id,
    coast_lat,
    coast_lon,
    known_water_lat,
    known_water_lon,
    buffer_miles=OFFSHORE_BUFFER_MILES,
):
    lat_dir, lon_dir = local_coast_direction(
        coast_id,
        coast_lat,
        coast_lon,
    )

    cos_lat = math.cos(math.radians(coast_lat))

    north = lat_dir * 69.0
    east = lon_dir * 69.0 * cos_lat

    length = math.hypot(north, east)

    if length == 0:
        return coast_lat, coast_lon

    north /= length
    east /= length

    normals = [
        (-east, north),
        (east, -north),
    ]

    candidates = []

    for normal_north, normal_east in normals:
        test_lat = coast_lat + (
            normal_north * buffer_miles / 69.0
        )

        test_lon = coast_lon + (
            normal_east
            * buffer_miles
            / (69.0 * cos_lat)
        )

        if not point_is_on_land(test_lat, test_lon):
            distance_to_known_water = haversine_miles(
                test_lat,
                test_lon,
                known_water_lat,
                known_water_lon,
            )

            candidates.append(
                (
                    distance_to_known_water,
                    test_lat,
                    test_lon,
                )
            )

    if not candidates:
        return known_water_lat, known_water_lon

    candidates.sort(key=lambda x: x[0])

    return candidates[0][1], candidates[0][2]


def coast_slide_mileage_and_time(
    old_lat,
    old_lon,
    hit_lat,
    hit_lon,
    safe_lat,
    safe_lon,
    attempted_miles,
    hours,
):
    to_hit = haversine_miles(
        old_lat,
        old_lon,
        hit_lat,
        hit_lon,
    )

    slide_distance = haversine_miles(
        hit_lat,
        hit_lon,
        safe_lat,
        safe_lon,
    )

    total_actual = to_hit + slide_distance

    if attempted_miles <= 0:
        elapsed_hours = 0.0
    else:
        speed_mph = attempted_miles / hours
        elapsed_hours = total_actual / speed_mph if speed_mph > 0 else 0.0

    return total_actual, elapsed_hours


BASE_DESTRUCTION_CHANCE = 0.00003


def destruction_roll(chance):
    return random.random() < chance


def bottle_age_days(bottle):
    launch_time = bottle.get("launch_time")

    if launch_time is None:
        history = bottle.get("journey_history", [])
        if history:
            launch_time = history[0].get("time")

    if launch_time is None:
        return 0.0

    try:
        launched = datetime.fromisoformat(launch_time)
        current = datetime.fromisoformat(bottle["current_time"])
        return max(0.0, (current - launched).total_seconds() / 86400.0)
    except (ValueError, TypeError, KeyError):
        return 0.0


def age_sinking_chance(age_days):
    # New bottles should almost never sink from age alone.
    # Fouling, leakage, seal deterioration and loss of buoyancy
    # gradually increase as the bottle spends longer at sea.
    if age_days < 90:
        return 0.0
    if age_days < 180:
        return 0.000008
    if age_days < 270:
        return 0.000025
    if age_days < 365:
        return 0.000050

    return 0.000100


def storm_destruction_multiplier(storm_level):
    levels = {
        "calm": 1.0,
        "rough": 8.0,
        "storm": 40.0,
        "severe": 160.0,
        "hurricane": 800.0,
    }

    return levels.get(storm_level, 1.0)


def maybe_destroy_bottle(
    bottle,
    storm_level="calm",
    persist=True,
):
    if bottle.get("opened"):
        return bottle

    if bottle.get("status") != "drifting":
        return bottle

    age_days = bottle_age_days(bottle)
    sinking_chance = age_sinking_chance(age_days)

    # Age-related sinking is its own physical failure mode.
    if sinking_chance > 0.0 and destruction_roll(sinking_chance):
        bottle["status"] = "lost_at_sea"
        bottle["destroyed"] = True
        bottle["loss_reason"] = "sank"
        bottle["destruction_time"] = bottle.get("current_time")
        bottle["destruction_miles"] = bottle.get(
            "total_miles_traveled",
            0.0,
        )
        bottle["age_days_at_loss"] = age_days
        bottle["destruction_chance"] = sinking_chance

        add_journey_event(
            bottle,
            "lost_at_sea",
            reason="sank",
            age_days=age_days,
            destruction_chance=sinking_chance,
        )

        if persist:
            save_bottle(bottle)

        return bottle

    storm_multiplier = storm_destruction_multiplier(storm_level)

    shipping_density = shipping_density_at(
        bottle["latitude"],
        bottle["longitude"],
    )
    shipping_multiplier, shipping_band = shipping_traffic_multiplier(
        shipping_density
    )

    combined_multiplier = storm_multiplier * shipping_multiplier

    final_chance = BASE_DESTRUCTION_CHANCE * combined_multiplier
    final_chance = min(1.0, final_chance)

    bottle["destruction_chance"] = final_chance
    bottle["storm_level"] = storm_level
    bottle["shipping_density"] = shipping_density
    bottle["shipping_traffic_band"] = shipping_band

    if destruction_roll(final_chance):
        if shipping_band in ("heavy", "very_heavy", "extreme"):
            loss_reason = "shipping_traffic"
        elif storm_level == "hurricane":
            loss_reason = "hurricane"
        elif storm_level == "severe":
            loss_reason = "severe_storm"
        elif storm_level == "storm":
            loss_reason = "storm"
        elif storm_level == "rough":
            loss_reason = "rough_seas"
        elif shipping_band in ("light", "moderate"):
            loss_reason = "vessel_traffic"
        else:
            loss_reason = "open_ocean_hazard"

        bottle["status"] = "lost_at_sea"
        bottle["destroyed"] = True
        bottle["loss_reason"] = loss_reason
        bottle["destruction_time"] = bottle.get("current_time")
        bottle["destruction_miles"] = bottle.get(
            "total_miles_traveled",
            0.0,
        )

        add_journey_event(
            bottle,
            "lost_at_sea",
            reason=loss_reason,
            storm_level=storm_level,
            storm_name=bottle.get("storm_name"),
            shipping_density=shipping_density,
            shipping_traffic_band=shipping_band,
            destruction_chance=final_chance,
        )

    if persist:
        save_bottle(bottle)

    return bottle


def update_bottle(
    bottle,
    hours=6,
    encounter_chance=0.06,
    persist=True,
    storm_level=None,
):
    if bottle["opened"]:
        return bottle

    if bottle["status"] != "drifting":
        return bottle

    if storm_level is None:
        storm_level, storm_info = storm_level_for_bottle(
            bottle["latitude"],
            bottle["longitude"],
            bottle["current_time"],
        )

        if storm_info is not None:
            bottle["storm_name"] = storm_info["name"]
            bottle["storm_distance_nm"] = storm_info["distance_nm"]
            bottle["storm_wind_band"] = storm_info["wind_band"]

            storm_sid = storm_info.get("sid")

            if (
                storm_sid
                and bottle.get("active_storm_sid") != storm_sid
            ):
                add_journey_event(
                    bottle,
                    "storm_encountered",
                    storm_sid=storm_sid,
                    storm_name=storm_info.get("name"),
                    storm_level=storm_level,
                    wind_band=storm_info.get("wind_band"),
                    distance_nm=storm_info.get("distance_nm"),
                    wind_knots=storm_info.get("wind_knots"),
                    category=storm_info.get("category"),
                )

            bottle["active_storm_sid"] = storm_sid

        else:
            bottle.pop("storm_name", None)
            bottle.pop("storm_distance_nm", None)
            bottle.pop("storm_wind_band", None)
            bottle.pop("active_storm_sid", None)

    maybe_destroy_bottle(
        bottle,
        storm_level=storm_level,
        persist=False,
    )

    if bottle["status"] == "lost_at_sea":
        if persist:
            save_bottle(bottle)
        return bottle

    maybe_encounter_real_coast(
        bottle,
        chance=encounter_chance,
        persist=False,
    )

    if bottle["status"] == "ashore":
        if persist:
            save_bottle(bottle)
        return bottle

    old_lat = bottle["latitude"]
    old_lon = bottle["longitude"]

    current_time = datetime.fromisoformat(
        bottle["current_time"]
    )
    date = current_time.strftime("%Y-%m-%d")

    new_lat, new_lon, miles = move_bottle_live(
        old_lat,
        old_lon,
        date,
        hours,
    )

    # Extreme polar waters are intentional bottle-loss zones.
    polar_boundary = None

    if new_lat >= 82.0:
        polar_boundary = 82.0
    elif new_lat <= -78.0:
        polar_boundary = -78.0

    if polar_boundary is not None:
        if new_lat != old_lat:
            fraction = (
                (polar_boundary - old_lat)
                / (new_lat - old_lat)
            )
            fraction = max(0.0, min(1.0, fraction))
        else:
            fraction = 1.0

        final_lon = old_lon + (
            (new_lon - old_lon) * fraction
        )

        bottle["latitude"] = polar_boundary
        bottle["longitude"] = final_lon
        bottle["total_miles_traveled"] += miles * fraction
        bottle["current_time"] = (
            current_time
            + timedelta(hours=hours * fraction)
        ).isoformat()

        bottle["status"] = "lost_at_sea"
        bottle["destroyed"] = True
        bottle["destruction_time"] = bottle["current_time"]
        bottle["destruction_miles"] = bottle[
            "total_miles_traveled"
        ]
        bottle["loss_reason"] = "polar_cap"

        add_journey_event(
            bottle,
            "lost_at_sea",
            reason="polar_cap",
            polar_boundary=polar_boundary,
        )

        if persist:
            save_bottle(bottle)

        return bottle

    (
        path_distance,
        path_coast_id,
        event_lat,
        event_lon,
    ) = movement_path_coast_event(
        old_lat,
        old_lon,
        new_lat,
        new_lon,
    )

    if (
        bottle.get("coast_armed", True)
        and bottle_is_mature(bottle)
        and path_distance is not None
        and path_distance <= COAST_ENCOUNTER_MILES
    ):
        bottle["coast_armed"] = False
        bottle["last_coast_id"] = path_coast_id
        bottle["coast_roll_count"] = (
            bottle.get("coast_roll_count", 0) + 1
        )

        if encounter_roll(encounter_chance):
            fraction = movement_fraction_to_point(
                old_lat,
                old_lon,
                new_lat,
                new_lon,
                event_lat,
                event_lon,
            )

            bottle["latitude"] = event_lat
            bottle["longitude"] = event_lon
            bottle["total_miles_traveled"] += (
                miles * fraction
            )
            bottle["current_time"] = (
                current_time
                + timedelta(hours=hours * fraction)
            ).isoformat()

            bottle["status"] = "ashore"
            bottle["ashore_time"] = bottle["current_time"]
            bottle["ashore_coast_id"] = path_coast_id

            ashore_dt = datetime.fromisoformat(bottle["ashore_time"])
            burial_days = random.randint(30, 180)
            bottle["burial_due_time"] = (
                ashore_dt + timedelta(days=burial_days)
            ).isoformat()
            bottle["finder_eligible"] = random.random() < 0.20

            add_journey_event(
                bottle,
                "washed_ashore",
                coast_id=path_coast_id,
            )

            if persist:
                save_bottle(bottle)

            return bottle

    # Expensive exact land-intersection checks are only
    # necessary when the movement path is actually near coast.
    land_hit = None

    if (
        path_distance is None
        or path_distance <= COAST_ENCOUNTER_MILES
    ):
        land_hit = first_land_contact(
            old_lat,
            old_lon,
            new_lat,
            new_lon,
        )

    if land_hit is not None:
        hit_lat, hit_lon = land_hit

        actual_coast_id = nearest_coast_id(
            hit_lat,
            hit_lon,
        )

        safe_lat, safe_lon = point_before_land_contact(
            old_lat,
            old_lon,
            hit_lat,
            hit_lon,
        )

        if actual_coast_id is not None:
            coast_lat, coast_lon = coast_slide_point(
                actual_coast_id,
                hit_lat,
                hit_lon,
                new_lat,
                new_lon,
                slide_miles=1.0,
            )

            safe_lat, safe_lon = (
                offset_coast_point_to_water_safe(
                    actual_coast_id,
                    coast_lat,
                    coast_lon,
                    old_lat,
                    old_lon,
                )
            )

        actual_miles, elapsed_hours = (
            coast_slide_mileage_and_time(
                old_lat,
                old_lon,
                hit_lat,
                hit_lon,
                safe_lat,
                safe_lon,
                miles,
                hours,
            )
        )

        bottle["latitude"] = safe_lat
        bottle["longitude"] = safe_lon
        bottle["total_miles_traveled"] += actual_miles
        bottle["current_time"] = (
            current_time
            + timedelta(hours=elapsed_hours)
        ).isoformat()

        bottle["status"] = "drifting"
        bottle["last_coast_id"] = (
            actual_coast_id
            or bottle.get("last_coast_id")
        )

        if persist:
            save_bottle(bottle)

        return bottle

    bottle["latitude"] = new_lat
    bottle["longitude"] = new_lon
    bottle["total_miles_traveled"] += miles
    bottle["current_time"] = (
        current_time + timedelta(hours=hours)
    ).isoformat()

    region = broad_region(
        new_lat,
        new_lon,
    )

    if (
        not bottle["journey_areas"]
        or bottle["journey_areas"][-1] != region
    ):
        previous_region = (
            bottle["journey_areas"][-1]
            if bottle["journey_areas"]
            else None
        )

        bottle["journey_areas"].append(region)

        add_journey_event(
            bottle,
            "region_entered",
            region=region,
            previous_region=previous_region,
        )

    update_coast_arm_state(bottle)

    if persist:
        save_bottle(bottle)

    return bottle


