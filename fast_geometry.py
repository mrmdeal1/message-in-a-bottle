import math
from numbers import Integral

from shapely.geometry import Point, LineString
from shapely.ops import nearest_points
import warnings
from shapely.strtree import STRtree

import bottle_engine as engine


# ---------------------------------------------------------
# Build clean geometry lists ONCE.
# Preserve original coastline IDs from bottle_engine.
# ---------------------------------------------------------

COAST_GEOMS = []
COAST_IDS = []

for index, geom in enumerate(engine.COASTLINES):
    if geom is None or geom.is_empty or not geom.is_valid:
        continue

    COAST_GEOMS.append(geom)
    COAST_IDS.append(f"coast_{index}")


LAND_GEOMS = [
    geom
    for geom in engine.LAND_POLYGONS
    if geom is not None
    and not geom.is_empty
    and geom.is_valid
]


COAST_TREE = STRtree(COAST_GEOMS)
LAND_TREE = STRtree(LAND_GEOMS)

_COAST_OBJECT_POS = {
    id(geom): pos
    for pos, geom in enumerate(COAST_GEOMS)
}


def haversine_miles(lat1, lon1, lat2, lon2):
    earth_radius_miles = 3958.7613

    lat1 = math.radians(lat1)
    lon1 = math.radians(lon1)
    lat2 = math.radians(lat2)
    lon2 = math.radians(lon2)

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin(dlon / 2.0) ** 2
    )

    c = 2.0 * math.atan2(
        math.sqrt(a),
        math.sqrt(1.0 - a),
    )

    return earth_radius_miles * c


def _tree_result_to_geometry(result, geoms):
    """
    Shapely 2 returns integer indexes.
    Older Shapely versions may return geometry objects.
    """
    if isinstance(result, Integral):
        return geoms[int(result)]

    return result


def _coast_geometry_and_id(result):
    if isinstance(result, Integral):
        pos = int(result)
        return COAST_GEOMS[pos], COAST_IDS[pos]

    pos = _COAST_OBJECT_POS.get(id(result))

    if pos is not None:
        return COAST_GEOMS[pos], COAST_IDS[pos]

    # Compatibility fallback.
    for pos, geom in enumerate(COAST_GEOMS):
        if geom.equals(result):
            return geom, COAST_IDS[pos]

    return None, None


def _query_geometries(tree, geoms, target, predicate=None):
    try:
        if predicate is None:
            results = tree.query(target)
        else:
            results = tree.query(
                target,
                predicate=predicate,
            )
    except TypeError:
        # Compatibility with older Shapely.
        results = tree.query(target)

    output = []

    for result in results:
        output.append(
            _tree_result_to_geometry(
                result,
                geoms,
            )
        )

    return output


# ---------------------------------------------------------
# FAST NEAREST COAST
# ---------------------------------------------------------

def _safe_nearest_points(a, b):
    if a is None or b is None:
        return None

    if a.is_empty or b.is_empty:
        return None

    try:
        bounds = list(a.bounds) + list(b.bounds)

        if not all(math.isfinite(v) for v in bounds):
            return None

        with warnings.catch_warnings():
            warnings.simplefilter(
                "ignore",
                RuntimeWarning,
            )
            p1, p2 = nearest_points(a, b)

        if (
            p1.is_empty
            or p2.is_empty
            or not math.isfinite(p1.x)
            or not math.isfinite(p1.y)
            or not math.isfinite(p2.x)
            or not math.isfinite(p2.y)
        ):
            return None

        return p1, p2

    except Exception:
        return None


def nearest_coast_distance_miles(lat, lon):
    if not COAST_GEOMS:
        return None

    point = Point(lon, lat)

    result = COAST_TREE.nearest(point)

    if result is None:
        return None

    coastline, _ = _coast_geometry_and_id(result)

    if coastline is None:
        return None

    nearest = _safe_nearest_points(
        point,
        coastline,
    )

    if nearest is None:
        return None

    _, coast_point = nearest

    return haversine_miles(
        lat,
        lon,
        coast_point.y,
        coast_point.x,
    )


def nearest_coast_id(lat, lon):
    if not COAST_GEOMS:
        return None

    point = Point(lon, lat)

    result = COAST_TREE.nearest(point)

    if result is None:
        return None

    _, coast_id = _coast_geometry_and_id(result)

    return coast_id


# ---------------------------------------------------------
# FAST MOVEMENT PATH / COAST CHECK
# ---------------------------------------------------------

def movement_path_coast_distance(
    lat1,
    lon1,
    lat2,
    lon2,
):
    if not COAST_GEOMS:
        return None, None

    path = LineString([
        (lon1, lat1),
        (lon2, lat2),
    ])

    result = COAST_TREE.nearest(path)

    if result is None:
        return None, None

    coastline, coast_id = _coast_geometry_and_id(
        result
    )

    if coastline is None:
        return None, None

    nearest = _safe_nearest_points(
        path,
        coastline,
    )

    if nearest is None:
        return None, None

    path_point, coast_point = nearest

    distance = haversine_miles(
        path_point.y,
        path_point.x,
        coast_point.y,
        coast_point.x,
    )

    return distance, coast_id


def _movement_segments(lat1, lon1, lat2, lon2):
    delta = lon2 - lon1

    if abs(delta) <= 180.0:
        return [
            LineString([
                (lon1, lat1),
                (lon2, lat2),
            ])
        ]

    if delta > 180.0:
        unwrapped_lon2 = lon2 - 360.0
        boundary1 = -180.0
        boundary2 = 180.0
    else:
        unwrapped_lon2 = lon2 + 360.0
        boundary1 = 180.0
        boundary2 = -180.0

    fraction = (
        (boundary1 - lon1)
        / (unwrapped_lon2 - lon1)
    )
    fraction = max(0.0, min(1.0, fraction))

    cross_lat = lat1 + (lat2 - lat1) * fraction

    return [
        LineString([
            (lon1, lat1),
            (boundary1, cross_lat),
        ]),
        LineString([
            (boundary2, cross_lat),
            (lon2, lat2),
        ]),
    ]


def movement_path_coast_event(
    lat1,
    lon1,
    lat2,
    lon2,
):
    if not COAST_GEOMS:
        return None, None, None, None

    best = None

    for path in _movement_segments(
        lat1, lon1, lat2, lon2
    ):
        result = COAST_TREE.nearest(path)

        if result is None:
            continue

        coastline, coast_id = _coast_geometry_and_id(
            result
        )

        if coastline is None:
            continue

        nearest = _safe_nearest_points(
            path,
            coastline,
        )

        if nearest is None:
            continue

        path_point, coast_point = nearest

        distance = haversine_miles(
            path_point.y,
            path_point.x,
            coast_point.y,
            coast_point.x,
        )

        candidate = (
            distance,
            coast_id,
            path_point.y,
            path_point.x,
        )

        if best is None or distance < best[0]:
            best = candidate

    if best is None:
        return None, None, None, None

    return best


# ---------------------------------------------------------
# FAST LAND CHECKS
# ---------------------------------------------------------

def point_is_on_land(lat, lon):
    point = Point(lon, lat)

    candidates = _query_geometries(
        LAND_TREE,
        LAND_GEOMS,
        point,
        predicate="intersects",
    )

    # The STRtree predicate already performed the exact
    # intersection test. Any returned polygon means this
    # point is on or inside land.
    return bool(candidates)


def movement_path_hits_land(
    lat1,
    lon1,
    lat2,
    lon2,
):
    for path in _movement_segments(
        lat1, lon1, lat2, lon2
    ):
        candidates = _query_geometries(
            LAND_TREE,
            LAND_GEOMS,
            path,
            predicate="intersects",
        )

        if candidates:
            return True

    return False


def first_land_contact(
    lat1,
    lon1,
    lat2,
    lon2,
):
    for path in _movement_segments(
        lat1, lon1, lat2, lon2
    ):
        candidates = _query_geometries(
            LAND_TREE,
            LAND_GEOMS,
            path,
            predicate="intersects",
        )

        first_point = None
        first_position = float("inf")

        for land in candidates:
            # STRtree predicate already confirmed the path
            # intersects this land polygon.
            hit = path.intersection(
                land.boundary
            )

            if hit.is_empty:
                continue

            if hit.geom_type == "Point":
                points = [hit]

            elif hasattr(hit, "geoms"):
                points = [
                    geom
                    for geom in hit.geoms
                    if geom.geom_type == "Point"
                ]

            else:
                points = []

            for point in points:
                position = path.project(point)

                if position < first_position:
                    first_position = position
                    first_point = point

        if first_point is not None:
            return (
                first_point.y,
                first_point.x,
            )

    return None


import sys

print(
    f"Fast geometry ready: "
    f"{len(COAST_GEOMS)} coast geometries, "
    f"{len(LAND_GEOMS)} land geometries.",
    file=sys.stderr,
)
