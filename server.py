from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import bottle_engine as engine
import beach_walk

app = FastAPI(title="Message in a Bottle API", version="0.1.0")


class LaunchRequest(BaseModel):
    latitude: float
    longitude: float
    sender_id: str | None = None
    message: str | None = None
    start_time: str | None = None


class ActionRequest(BaseModel):
    event_time: str | None = None
    finder_id: str | None = None
    reply_message: str | None = None


@app.get("/health")
def health():
    return {"ok": True, "service": "message-in-a-bottle"}


@app.post("/api/bottle/launch")
def launch(req: LaunchRequest):
    start_time = req.start_time or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    bottle = engine.create_bottle(
        req.latitude,
        req.longitude,
        start_time=start_time,
        sender_id=req.sender_id,
        message=req.message,
    )
    engine.save_bottle(bottle)
    return {
        "bottle_id": bottle["bottle_id"],
        "status": bottle["status"],
        "eligible_after": bottle["eligible_after"],
        "journey_areas": bottle["journey_areas"],
    }


@app.get("/api/bottle")
def current_bottle():
    bottle = engine.load_bottle()
    if bottle is None:
        raise HTTPException(status_code=404, detail="No bottle exists yet.")
    return engine.finder_view(bottle)


@app.post("/api/bottle/encounter")
def encounter(req: ActionRequest):
    bottle = engine.load_bottle()
    if bottle is None:
        raise HTTPException(status_code=404, detail="No bottle exists yet.")

    result = engine.encounter_bottle(
        bottle,
        finder_id=req.finder_id,
        event_time=req.event_time,
    )

    if isinstance(result, str):
        raise HTTPException(status_code=409, detail=result)

    return engine.finder_view(result)


@app.post("/api/bottle/open")
def open_bottle(req: ActionRequest):
    bottle = engine.load_bottle()
    if bottle is None:
        raise HTTPException(status_code=404, detail="No bottle exists yet.")

    result = engine.open_bottle(
        bottle,
        event_time=req.event_time,
    )

    if isinstance(result, str):
        raise HTTPException(status_code=409, detail=result)

    return engine.finder_view(result)


@app.post("/api/bottle/throw-back")
def throw_back(req: ActionRequest):
    bottle = engine.load_bottle()
    if bottle is None:
        raise HTTPException(status_code=404, detail="No bottle exists yet.")

    result = engine.throw_back(
        bottle,
        event_time=req.event_time,
    )

    if isinstance(result, str):
        raise HTTPException(status_code=409, detail=result)

    return {
        "status": result["status"],
        "bottle_id": result["bottle_id"],
    }


@app.post("/api/bottle/reply")
def reply(req: ActionRequest):
    if req.reply_message is None:
        raise HTTPException(status_code=400, detail="reply_message is required.")

    bottle = engine.load_bottle()
    if bottle is None:
        raise HTTPException(status_code=404, detail="No bottle exists yet.")

    result = engine.reply_to_sender(
        bottle,
        req.reply_message,
        event_time=req.event_time,
    )

    if isinstance(result, str) and result != "Reply sent to original sender.":
        raise HTTPException(status_code=409, detail=result)

    return {
        "result": result,
        "view": engine.finder_view(bottle),
    }
