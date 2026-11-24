import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.db import init_db_pool, close_db_pool, get_db_pool
from app.generator import generator_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)
logger = logging.getLogger("review_queue")


class BurstRequest(BaseModel):
    size: int = Field(default=250, ge=1, le=1000, description="Number of items to generate in burst")
    duration: float = Field(default=8.0, ge=0.5, le=30.0, description="Duration in seconds (default 8.0s, <10s)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database connection pool...")
    try:
        pool = await init_db_pool()
        logger.info("Database pool initialized and schema verified.")
        generator_service.set_pool(pool)
    except Exception as e:
        logger.error(f"Failed to connect to database on startup: {e}")
        raise e

    yield

    logger.info("Stopping load generator if running...")
    generator_service.stop()
    logger.info("Closing database pool...")
    await close_db_pool()
    logger.info("Database pool closed.")


app = FastAPI(
    title="Real-Time Review Queue",
    description="Inspection line review queue with atomic claim, live WebSockets, and lease recovery.",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check():
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval("SELECT 1")
            if val != 1:
                raise HTTPException(status_code=500, detail="Database ping failed")
        return {"status": "ok", "database": "connected"}
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=503, detail=f"Database unavailable: {str(e)}")


# -------------------------------------------------------------
# Load Generator Control Endpoints
# -------------------------------------------------------------

@app.post("/api/generator/start")
async def start_generator():
    """Starts the background generator (steady flow + random burst scheduler)."""
    generator_service.start()
    return {"message": "Load generator started", "status": generator_service.get_metrics()}


@app.post("/api/generator/stop")
async def stop_generator():
    """Stops all background generator tasks."""
    generator_service.stop()
    return {"message": "Load generator stopped", "status": generator_service.get_metrics()}


@app.post("/api/generator/burst")
async def trigger_burst(req: BurstRequest = BurstRequest()):
    """Triggers an immediate burst of inspection items (default 250 in 8 seconds)."""
    metrics = await generator_service.trigger_burst(size=req.size, duration=req.duration)
    return {"message": "Burst completed", "metrics": metrics}


@app.get("/api/generator/status")
async def get_generator_status():
    """Returns real-time load generator metrics and recent arrival rate."""
    return generator_service.get_metrics()


# Mount static directory if it exists
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
