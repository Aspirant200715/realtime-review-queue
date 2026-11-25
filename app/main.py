import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field
from fastapi import FastAPI, HTTPException, status, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.db import init_db_pool, close_db_pool, get_db_pool
from app.generator import generator_service
from app.queue import queue_engine, lease_sweeper_service, ItemNotClaimedError, QueueError
from app.websocket import ws_manager, serialize_json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)
logger = logging.getLogger("review_queue")


# -------------------------------------------------------------
# Request Schemas
# -------------------------------------------------------------

class BurstRequest(BaseModel):
    size: int = Field(default=250, ge=1, le=1000, description="Number of items to generate in burst")
    duration: float = Field(default=8.0, ge=0.5, le=30.0, description="Duration in seconds (default 8.0s, <10s)")


class ClaimRequest(BaseModel):
    reviewer_id: str = Field(..., min_length=1, max_length=64, description="ID of the claiming reviewer")


class JudgeRequest(BaseModel):
    item_id: str = Field(..., min_length=1, max_length=64)
    reviewer_id: str = Field(..., min_length=1, max_length=64)
    decision: str = Field(..., pattern="^(PASS|FAIL)$", description="Must be 'PASS' or 'FAIL'")
    notes: Optional[str] = Field(None, max_length=500)


class ReleaseRequest(BaseModel):
    item_id: str = Field(..., min_length=1, max_length=64)
    reviewer_id: str = Field(..., min_length=1, max_length=64)
    reason: Optional[str] = Field("voluntary_release", max_length=200)


class HeartbeatRequest(BaseModel):
    item_id: str = Field(..., min_length=1, max_length=64)
    reviewer_id: str = Field(..., min_length=1, max_length=64)


# -------------------------------------------------------------
# Lifespan
# -------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database connection pool...")
    try:
        pool = await init_db_pool()
        logger.info("Database pool initialized and schema verified.")
        generator_service.set_pool(pool)
        queue_engine.set_pool(pool)
        lease_sweeper_service.start()

        # Wire real-time push broadcasts on item arrivals
        async def on_items_generated(items):
            try:
                stats = await queue_engine.get_queue_stats()
                await ws_manager.broadcast({
                    "type": "QUEUE_UPDATE",
                    "trigger": "ITEM_ARRIVAL",
                    "arrival_count": len(items),
                    "stats": stats
                })
            except Exception as e:
                logger.error(f"Error broadcasting on item arrival: {e}")

        generator_service.register_listener(on_items_generated)

        # Wire real-time push broadcasts on claim, decision, release, and lease recovery
        async def on_queue_event(event_type, data):
            try:
                stats = await queue_engine.get_queue_stats()
                await ws_manager.broadcast({
                    "type": "QUEUE_UPDATE",
                    "trigger": event_type,
                    "event_data": data,
                    "stats": stats
                })
            except Exception as e:
                logger.error(f"Error broadcasting on queue event ({event_type}): {e}")

        queue_engine.register_listener(on_queue_event)

    except Exception as e:
        logger.error(f"Failed to connect to database on startup: {e}")
        raise e

    yield

    logger.info("Stopping lease sweeper...")
    lease_sweeper_service.stop()
    logger.info("Stopping load generator if running...")
    generator_service.stop()
    logger.info("Closing database pool...")
    await close_db_pool()
    logger.info("Database pool closed.")


# -------------------------------------------------------------
# FastAPI App
# -------------------------------------------------------------

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
# Queue Engine Endpoints (Phase 3)
# -------------------------------------------------------------

@app.post("/api/queue/claim")
async def claim_item(req: ClaimRequest):
    """
    Atomically claims the highest priority pending item for a reviewer.
    If the reviewer already holds an active claim, returns that claim (idempotent).
    """
    try:
        item, already_claimed = await queue_engine.claim_next_item(req.reviewer_id)
        if item is None:
            return {
                "item": None,
                "already_claimed": False,
                "message": "Queue is currently empty. No pending items available."
            }
        return {
            "item": item,
            "already_claimed": already_claimed,
            "message": "Existing active claim returned" if already_claimed else "Item claimed successfully"
        }
    except Exception as e:
        logger.error(f"Error claiming item for {req.reviewer_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/queue/judge")
async def judge_item(req: JudgeRequest):
    """
    Records a PASS or FAIL judgement for an actively claimed item.
    Guarantees only the currently active lease-holder can judge the item.
    """
    try:
        item = await queue_engine.record_decision(
            item_id=req.item_id,
            reviewer_id=req.reviewer_id,
            decision=req.decision,
            notes=req.notes
        )
        return {"message": f"Item successfully judged {req.decision}", "item": item}
    except ItemNotClaimedError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except Exception as e:
        logger.error(f"Error recording decision: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/queue/release")
async def release_item(req: ReleaseRequest):
    """
    Voluntarily releases a claimed item back to the PENDING queue.
    """
    try:
        item = await queue_engine.release_item(
            item_id=req.item_id,
            reviewer_id=req.reviewer_id,
            reason=req.reason or "voluntary_release"
        )
        return {"message": "Item returned to PENDING queue", "item": item}
    except ItemNotClaimedError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except Exception as e:
        logger.error(f"Error releasing item: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/queue/heartbeat")
async def heartbeat(req: HeartbeatRequest):
    """
    Extends the active lease for a reviewer's currently claimed item.
    """
    try:
        res = await queue_engine.renew_lease(req.item_id, req.reviewer_id)
        return {"message": "Lease extended successfully", "data": res}
    except ItemNotClaimedError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except Exception as e:
        logger.error(f"Error extending lease for {req.item_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/queue/sweep")
async def trigger_sweep():
    """
    Triggers an immediate sweep of expired leases, returning abandoned items to PENDING.
    """
    try:
        recovered = await queue_engine.sweep_expired_leases()
        return {"recovered_count": len(recovered), "recovered_items": recovered}
    except Exception as e:
        logger.error(f"Error sweeping expired leases: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/queue/sweeper/status")
async def get_sweeper_status():
    """Returns telemetry of the background lease sweeper service."""
    return lease_sweeper_service.get_metrics()


@app.get("/api/queue/stats")
async def get_queue_stats():
    """Returns current backlog counts, reviewer allocations, and next item preview."""
    try:
        return await queue_engine.get_queue_stats()
    except Exception as e:
        logger.error(f"Error fetching queue stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/queue/active/{reviewer_id}")
async def get_reviewer_active_item(reviewer_id: str):
    """Returns any actively claimed item held by the reviewer."""
    try:
        active = await queue_engine.get_active_claim_for_reviewer(reviewer_id)
        return {"active_item": active}
    except Exception as e:
        logger.error(f"Error fetching active item for {reviewer_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/queue/preview")
async def preview_next_item():
    """Peeks at the highest-priority pending item without claiming it."""
    try:
        preview = await queue_engine.peek_next_item()
        return {"preview": preview}
    except Exception as e:
        logger.error(f"Error peeking next item: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------
# Load Generator Control Endpoints (Phase 2)
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


# -------------------------------------------------------------
# WebSocket Endpoints (Phase 5)
# -------------------------------------------------------------

@app.websocket("/ws/reviewer/{reviewer_id}")
async def reviewer_websocket_endpoint(websocket: WebSocket, reviewer_id: str):
    """
    Dedicated bidirectional WebSocket channel for a reviewer screen:
    - Pushes live queue state and active item on connect.
    - Broadcasts live backlog updates on all state transitions (zero polling).
    - Accepts client heartbeats, claims, decisions, and releases.
    """
    await ws_manager.connect(websocket, reviewer_id)
    try:
        # Push initial queue state and active item
        stats = await queue_engine.get_queue_stats()
        active = await queue_engine.get_active_claim_for_reviewer(reviewer_id)
        await websocket.send_text(serialize_json({
            "type": "INITIAL_STATE",
            "reviewer_id": reviewer_id,
            "active_item": active,
            "stats": stats
        }))

        while True:
            raw_text = await websocket.receive_text()
            try:
                msg = json.loads(raw_text)
                msg_type = msg.get("type", "").upper()

                if msg_type == "HEARTBEAT":
                    item_id = msg.get("item_id")
                    if item_id:
                        try:
                            renewed = await queue_engine.renew_lease(item_id, reviewer_id)
                            await websocket.send_text(serialize_json({
                                "type": "HEARTBEAT_ACK",
                                "item_id": item_id,
                                "remaining_lease_seconds": renewed["remaining_lease_seconds"]
                            }))
                        except ItemNotClaimedError as e:
                            await websocket.send_text(serialize_json({
                                "type": "LEASE_LOST",
                                "item_id": item_id,
                                "message": str(e)
                            }))

                elif msg_type == "CLAIM":
                    item, already = await queue_engine.claim_next_item(reviewer_id)
                    await websocket.send_text(serialize_json({
                        "type": "CLAIM_RESULT",
                        "item": item,
                        "already_claimed": already
                    }))

                elif msg_type == "DECIDE":
                    item_id = msg.get("item_id")
                    decision = msg.get("decision", "").upper()
                    notes = msg.get("notes")
                    try:
                        judged = await queue_engine.record_decision(item_id, reviewer_id, decision, notes)
                        await websocket.send_text(serialize_json({
                            "type": "DECISION_ACK",
                            "item": judged,
                            "decision": decision
                        }))
                    except ItemNotClaimedError as e:
                        await websocket.send_text(serialize_json({
                            "type": "ERROR",
                            "code": "DECISION_REJECTED",
                            "message": str(e)
                        }))

                elif msg_type == "RELEASE":
                    item_id = msg.get("item_id")
                    reason = msg.get("reason", "voluntary_release")
                    try:
                        released = await queue_engine.release_item(item_id, reviewer_id, reason)
                        await websocket.send_text(serialize_json({
                            "type": "RELEASE_ACK",
                            "item": released
                        }))
                    except ItemNotClaimedError as e:
                        await websocket.send_text(serialize_json({
                            "type": "ERROR",
                            "code": "RELEASE_REJECTED",
                            "message": str(e)
                        }))

            except json.JSONDecodeError:
                await websocket.send_text(serialize_json({"type": "ERROR", "message": "Invalid JSON"}))
            except Exception as e:
                logger.error(f"Error handling WebSocket message from {reviewer_id}: {e}")
                await websocket.send_text(serialize_json({"type": "ERROR", "message": str(e)}))

    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket, reviewer_id)
    except Exception as e:
        logger.error(f"WebSocket exception for {reviewer_id}: {e}")
        await ws_manager.disconnect(websocket, reviewer_id)


@app.websocket("/ws/queue")
async def queue_monitor_websocket(websocket: WebSocket):
    """
    Read-only broadcast WebSocket for multi-panel overview or dashboard monitoring.
    Receives all live queue updates and telemetry.
    """
    await ws_manager.connect(websocket, "monitor")
    try:
        stats = await queue_engine.get_queue_stats()
        await websocket.send_text(serialize_json({
            "type": "INITIAL_STATE",
            "stats": stats
        }))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket, "monitor")
    except Exception as e:
        logger.error(f"Queue monitor WebSocket exception: {e}")
        await ws_manager.disconnect(websocket, "monitor")


# Mount static directory if it exists
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
