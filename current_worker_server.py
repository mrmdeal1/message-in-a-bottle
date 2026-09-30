from fastapi import FastAPI, HTTPException
import copernicusmarine

DATASET_ID = "cmems_mod_glo_phy-cur_anfc_0.083deg_P1D-m"
SURFACE_DEPTH = 0.49402499198913574

app = FastAPI(title="Message in a Bottle Current Worker", version="0.1.0")


@app.get("/health")
def health():
    return {"ok": True, "service": "current-worker"}


@app.get("/current")
def current(lat: float, lon: float, date: str):
    dataset = None
    point = None
    try:
        dataset = copernicusmarine.open_dataset(
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
        point = dataset.sel(
            latitude=lat,
            longitude=lon,
            method="nearest",
        )
        u = float(point["uo"].values.squeeze())
        v = float(point["vo"].values.squeeze())
        return {"u": u, "v": v}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        point = None
        if dataset is not None:
            close = getattr(dataset, "close", None)
            if callable(close):
                close()
