import math
from collections import OrderedDict
from pathlib import Path

import numpy as np
import xarray as xr


DATA_DIR = Path("copernicus_data/2025")
MAX_OPEN_FILES = 3

_file_index = None
_open_cache = OrderedDict()

# Reuse solved current vectors during large simulator batches.
# The 1,000-bottle fate test repeats the same global routes,
# so identical position/date lookups do not need another
# NetCDF/HDF5 read.
_vector_cache = OrderedDict()
MAX_VECTOR_CACHE = 250000


def normalize_lon(lon):
    return ((float(lon) + 180.0) % 360.0) - 180.0


def nearest_index(values, target):
    values = np.asarray(values)
    i = int(np.searchsorted(values, target))

    if i <= 0:
        return 0
    if i >= len(values):
        return len(values) - 1

    before = values[i - 1]
    after = values[i]

    if abs(target - before) <= abs(after - target):
        return i - 1
    return i


def build_file_index():
    global _file_index

    files = []

    for path in sorted(DATA_DIR.glob("*.nc")):
        try:
            ds = xr.open_dataset(path)

            lat = ds.latitude.values
            lon = ds.longitude.values
            times = ds.time.values

            files.append({
                "path": path,
                "lat_min": float(lat[0]),
                "lat_max": float(lat[-1]),
                "lon_min": float(lon[0]),
                "lon_max": float(lon[-1]),
                "time_min": np.datetime64(times[0], "D"),
                "time_max": np.datetime64(times[-1], "D"),
            })

            ds.close()

        except Exception as exc:
            print(f"Skipping {path}: {exc}")

    _file_index = files
    return files


def get_index():
    if _file_index is None:
        return build_file_index()
    return _file_index


def find_file(lat, lon, date):
    lon = normalize_lon(lon)
    date64 = np.datetime64(date, "D")

    candidates = []

    for item in get_index():
        if (
            item["lat_min"] <= lat <= item["lat_max"]
            and item["lon_min"] <= lon <= item["lon_max"]
            and item["time_min"] <= date64 <= item["time_max"]
        ):
            candidates.append(item)

    if not candidates:
        raise ValueError(
            f"No local current tile covers "
            f"lat={lat:.4f}, lon={lon:.4f}, date={date}"
        )

    # Prefer the geographically smallest matching file.
    candidates.sort(
        key=lambda x: (
            (x["lat_max"] - x["lat_min"])
            * (x["lon_max"] - x["lon_min"])
        )
    )

    return candidates[0]["path"]


def open_cached(path):
    path = str(path)

    if path in _open_cache:
        ds = _open_cache.pop(path)
        _open_cache[path] = ds
        return ds

    ds = xr.open_dataset(path)
    _open_cache[path] = ds

    while len(_open_cache) > MAX_OPEN_FILES:
        _, old_ds = _open_cache.popitem(last=False)
        old_ds.close()

    return ds


def current_vector(lat, lon, date):
    lon = normalize_lon(lon)

    cache_key = (
        round(float(lat), 7),
        round(float(lon), 7),
        str(date),
    )

    if cache_key in _vector_cache:
        value = _vector_cache.pop(cache_key)
        _vector_cache[cache_key] = value
        return value

    path = find_file(lat, lon, date)
    ds = open_cached(path)

    lat_values = ds.latitude.values
    lon_values = ds.longitude.values
    time_values = ds.time.values.astype("datetime64[D]")

    yi = nearest_index(lat_values, lat)
    xi = nearest_index(lon_values, lon)
    ti = nearest_index(time_values, np.datetime64(date, "D"))

    def vector_at(y, x):
        u = float(
            ds["uo"]
            .isel(time=ti, depth=0, latitude=y, longitude=x)
            .values
            .squeeze()
        )

        v = float(
            ds["vo"]
            .isel(time=ti, depth=0, latitude=y, longitude=x)
            .values
            .squeeze()
        )

        return u, v

    u, v = vector_at(yi, xi)

    if math.isfinite(u) and math.isfinite(v):
        result = (u, v, str(path))

        _vector_cache[cache_key] = result

        while len(_vector_cache) > MAX_VECTOR_CACHE:
            _vector_cache.popitem(last=False)

        return result

    # Coastal grid cells may be masked even though nearby ocean
    # cells contain valid Copernicus current data. Search outward
    # for the nearest usable ocean-current cell.
    best = None
    max_radius = 12

    y_count = len(lat_values)
    x_count = len(lon_values)

    for radius in range(1, max_radius + 1):
        candidates = []

        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if max(abs(dy), abs(dx)) != radius:
                    continue

                y = yi + dy
                x = xi + dx

                if not (0 <= y < y_count and 0 <= x < x_count):
                    continue

                cu, cv = vector_at(y, x)

                if not math.isfinite(cu) or not math.isfinite(cv):
                    continue

                cell_lat = float(lat_values[y])
                cell_lon = float(lon_values[x])

                distance2 = (
                    (cell_lat - lat) ** 2
                    + (cell_lon - lon) ** 2
                )

                candidates.append(
                    (distance2, cu, cv, cell_lat, cell_lon)
                )

        if candidates:
            best = min(candidates, key=lambda item: item[0])
            break

    if best is None:
        raise ValueError(
            f"No usable ocean current near "
            f"{lat:.4f}, {lon:.4f} on {date}"
        )

    _, u, v, fallback_lat, fallback_lon = best

    result = (u, v, str(path))

    _vector_cache[cache_key] = result

    while len(_vector_cache) > MAX_VECTOR_CACHE:
        _vector_cache.popitem(last=False)

    return result


def move_bottle(lat, lon, date, hours=24):
    u, v, source_file = current_vector(lat, lon, date)

    seconds = float(hours) * 3600.0

    east_m = u * seconds
    north_m = v * seconds

    new_lat = lat + north_m / 111320.0

    cos_lat = math.cos(math.radians(lat))
    if abs(cos_lat) < 1e-6:
        cos_lat = 1e-6

    new_lon = lon + east_m / (111320.0 * cos_lat)
    new_lon = normalize_lon(new_lon)

    miles = math.hypot(east_m, north_m) / 1609.344

    return new_lat, new_lon, miles


def close_all():
    for ds in _open_cache.values():
        ds.close()

    _open_cache.clear()


if __name__ == "__main__":
    index = get_index()

    print(f"Indexed {len(index)} local current files.")

    for item in index:
        print(
            item["path"].name,
            f'lat {item["lat_min"]:.2f}..{item["lat_max"]:.2f}',
            f'lon {item["lon_min"]:.2f}..{item["lon_max"]:.2f}',
            item["time_min"],
            "to",
            item["time_max"],
        )
