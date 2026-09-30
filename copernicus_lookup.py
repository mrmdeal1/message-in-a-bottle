import json
import math
import sys

import copernicusmarine

DATASET_ID = "cmems_mod_glo_phy-cur_anfc_0.083deg_P1D-m"
SURFACE_DEPTH = 0.49402499198913574


def main():
    lat = float(sys.argv[1])
    lon = float(sys.argv[2])
    date = sys.argv[3]
    hours = float(sys.argv[4])

    ds = copernicusmarine.open_dataset(
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

    try:
        point = ds.sel(
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

        print(json.dumps({
            "latitude": new_lat,
            "longitude": new_lon,
            "miles": miles,
        }))
    finally:
        close = getattr(ds, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    main()
