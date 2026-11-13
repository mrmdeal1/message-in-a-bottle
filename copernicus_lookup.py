"""Copernicus surface-current lookup used by the Render current worker.

Coastal points may be masked in the Copernicus grid. Only measured, finite
water-cell vectors are used; no synthetic movement is ever generated.
"""
import json
import math
import sys

import copernicusmarine
import numpy as np

DATASET_ID = "cmems_mod_glo_phy-cur_anfc_0.083deg_P1D-m"
SURFACE_DEPTH = 0.49402499198913574
SEARCH_RADIUS_DEGREES = 0.30
MAX_VALID_CELL_DISTANCE_KM = 30.0


def nearest_valid_surface_current(ds, lat, lon):
    """Choose the nearest finite measured water cell within 30 km."""
    u_grid = ds["uo"]
    v_grid = ds["vo"]
    # Copernicus supplies one time/depth for a dated surface lookup.
    # Discard singleton or first non-spatial indices; never collapse the grid.
    for dimension in tuple(u_grid.dims):
        if dimension not in ("latitude", "longitude"):
            u_grid = u_grid.isel({dimension: 0})
            v_grid = v_grid.isel({dimension: 0})
    u = np.asarray(u_grid.transpose("latitude", "longitude").values, dtype=float)
    v = np.asarray(v_grid.transpose("latitude", "longitude").values, dtype=float)
    latitudes = np.asarray(ds["latitude"].values, dtype=float).reshape(-1)
    longitudes = np.asarray(ds["longitude"].values, dtype=float).reshape(-1)

    if u.shape != (latitudes.size, longitudes.size) or u.shape != v.shape:
        raise ValueError("Unexpected Copernicus current grid shape")

    north_km = (latitudes[:, None] - lat) * 111.32
    delta_lon = (longitudes[None, :] - lon + 180.0) % 360.0 - 180.0
    east_km = delta_lon * 111.32 * math.cos(math.radians(lat))
    distance_km = np.hypot(north_km, east_km)
    valid = (np.isfinite(u) & np.isfinite(v) & np.isfinite(distance_km)
             & (distance_km <= MAX_VALID_CELL_DISTANCE_KM))

    if not np.any(valid):
        raise ValueError(
            f"No usable finite ocean current within "
            f"{MAX_VALID_CELL_DISTANCE_KM:g} km of lat={lat:.4f}, lon={lon:.4f}"
        )

    y, x = np.unravel_index(np.argmin(np.where(valid, distance_km, np.inf)), u.shape)
    return float(u[y, x]), float(v[y, x])


def main():
    lat = float(sys.argv[1])
    lon = float(sys.argv[2])
    date = sys.argv[3]
    hours = float(sys.argv[4])
    if not all(math.isfinite(v) for v in (lat, lon, hours)):
        raise ValueError("Non-finite lookup parameters")

    ds = copernicusmarine.open_dataset(
        dataset_id=DATASET_ID,
        variables=["uo", "vo"],
        minimum_longitude=lon - SEARCH_RADIUS_DEGREES,
        maximum_longitude=lon + SEARCH_RADIUS_DEGREES,
        minimum_latitude=max(-90.0, lat - SEARCH_RADIUS_DEGREES),
        maximum_latitude=min(90.0, lat + SEARCH_RADIUS_DEGREES),
        start_datetime=date,
        end_datetime=date,
        minimum_depth=SURFACE_DEPTH,
        maximum_depth=SURFACE_DEPTH,
    )

    try:
        u, v = nearest_valid_surface_current(ds, lat, lon)
        seconds = hours * 3600.0
        east_m = u * seconds
        north_m = v * seconds
        cos_lat = math.cos(math.radians(lat))
        if abs(cos_lat) < 1e-6:
            raise ValueError("No reliable longitude step near geographic pole")

        result = {
            "latitude": lat + north_m / 111320.0,
            "longitude": lon + east_m / (111320.0 * cos_lat),
            "miles": math.hypot(east_m, north_m) / 1609.344,
        }
        # A bad ocean-data value must never reach FastAPI's JSON serializer.
        print(json.dumps(result, allow_nan=False))
    finally:
        close = getattr(ds, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    main()
