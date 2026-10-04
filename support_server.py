from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="Vicissitude Support")


@app.get("/", response_class=HTMLResponse)
def support_home():
    return Path("support_page.html").read_text(encoding="utf-8")


@app.get("/health")
def health():
    return {"ok": True, "service": "vicissitude-support"}
