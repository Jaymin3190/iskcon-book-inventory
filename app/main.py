import traceback

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR, STATIC_DIR
from .database import init_db
from .routers import auth, catalogue, locations, pos, reports, stock, users
from .seed import seed

init_db()
seed()

app = FastAPI(
    title="ISKCON Book Inventory & Distribution Management",
    version="0.1.0",
    description="Centralized book inventory, multi-level distribution, barcode POS billing, and audit for ISKCON.",
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    try:
        with open(BASE_DIR / "server-errors.log", "a", encoding="utf-8") as f:
            f.write(traceback.format_exc() + "\n")
    except Exception:
        pass
    return JSONResponse(status_code=500, content={"detail": str(exc)})

for r in (auth.router, users.router, catalogue.router, locations.router, stock.router, pos.router, reports.router):
    app.include_router(r)

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
