import asyncio
import time
import pytest
import httpx
from typing import List, Dict, Any
from app.config import settings
from app.db import init_db_pool, close_db_pool, get_db_pool
from app.generator import ItemFactory, LoadGenerator, generator_service
from app.main import app

# Local socket configuration if running in sandbox
import os
if os.path.exists("/home/aspirant/Projects/realtime-review-queue/.tools/run"):
    settings.postgres_socket_dir = "/home/aspirant/Projects/realtime-review-queue/.tools/run"
    settings.postgres_port = 54329


def test_item_priority_distribution_and_schema():
    """Verify priority spread: ~10% Critical, ~25% High, ~65% Routine, and valid payload schema."""
    sample_size = 1000
    items = [ItemFactory.create_item() for _ in range(sample_size)]

    critical_count = sum(1 for item in items if item["priority"] in [9, 10])
    high_count = sum(1 for item in items if item["priority"] in [6, 7, 8])
    routine_count = sum(1 for item in items if item["priority"] in [1, 2, 3, 4, 5])

    critical_pct = (critical_count / sample_size) * 100
    high_pct = (high_count / sample_size) * 100
    routine_pct = (routine_count / sample_size) * 100

    print(f"\nPriority Distribution for N={sample_size}:")
    print(f"  Critical (9-10): {critical_count} ({critical_pct:.1f}%) [Expected: ~10%]")
    print(f"  High     (6-8):  {high_count} ({high_pct:.1f}%) [Expected: ~25%]")
    print(f"  Routine  (1-5):  {routine_count} ({routine_pct:.1f}%) [Expected: ~65%]")

    # Check bounds (with statistical margin of error for N=1000)
    assert 7.0 <= critical_pct <= 14.0, f"Critical percentage out of bounds: {critical_pct}%"
    assert 20.0 <= high_pct <= 30.0, f"High percentage out of bounds: {high_pct}%"
    assert 58.0 <= routine_pct <= 72.0, f"Routine percentage out of bounds: {routine_pct}%"

    # Schema integrity check
    for item in items[:20]:
        assert item["id"].startswith("item_")
        assert 1 <= item["priority"] <= 10
        payload = item["payload"]
        assert "part_number" in payload
        assert "component" in payload
        assert "defect_type" in payload
        assert "confidence_score" in payload
        assert "camera_id" in payload
        assert "dimensions_mm" in payload


@pytest.mark.asyncio
async def test_burst_shape_under_10_seconds():
    """Verify that a 250-item burst is dumped strictly in under 10 seconds."""
    generator = LoadGenerator(pool=None, burst_size=250, burst_duration=8.0)
    start_t = time.perf_counter()

    metrics = await generator.trigger_burst(size=250, duration=8.0)
    elapsed = time.perf_counter() - start_t

    assert metrics["items_generated"] == 250
    assert metrics["completed_in_under_10s"] is True
    assert elapsed < 10.0, f"Burst took too long: {elapsed:.3f} seconds (must be < 10s)"
    assert elapsed >= 7.0, f"Burst finished suspiciously fast: {elapsed:.3f} seconds"
    assert metrics["effective_rate_per_sec"] > 25.0, f"Burst rate too low: {metrics['effective_rate_per_sec']} items/s"


@pytest.mark.asyncio
async def test_steady_trickle_arrival_rate():
    """Verify that the steady rate produces ~5 items/second."""
    generator = LoadGenerator(pool=None, steady_rate=5.0)
    generator.start()

    # Let it run for 2.0 seconds
    await asyncio.sleep(2.0)
    generator.stop()

    generated = generator.total_generated
    print(f"\nSteady generator produced {generated} items in 2.0 seconds (~{generated / 2.0:.1f} items/s).")
    # Expected ~10 items (allowing ±3 items for random jitter)
    assert 7 <= generated <= 14, f"Expected 7-14 items in 2.0s, got {generated}"


@pytest.mark.asyncio
async def test_database_burst_ingestion_and_audit():
    """Verify that burst ingestion into PostgreSQL creates all rows and CREATED audit records."""
    pool = await init_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    generator = LoadGenerator(pool=pool, burst_size=250, burst_duration=8.0)
    metrics = await generator.trigger_burst(size=250, duration=8.0)

    assert metrics["completed_in_under_10s"] is True

    async with pool.acquire() as conn:
        # Verify 250 items in inspection_items
        items_count = await conn.fetchval("SELECT count(*) FROM inspection_items;")
        assert items_count == 250, f"Expected 250 items in DB, found {items_count}"

        # Verify all are PENDING
        pending_count = await conn.fetchval("SELECT count(*) FROM inspection_items WHERE status = 'PENDING';")
        assert pending_count == 250, f"Expected 250 PENDING items, found {pending_count}"

        # Verify 250 audit records with event_type = 'CREATED'
        audit_count = await conn.fetchval("SELECT count(*) FROM audit_events WHERE event_type = 'CREATED';")
        assert audit_count == 250, f"Expected 250 CREATED audit events, found {audit_count}"

        # Verify priority ordering seeks highest priority first
        top_item = await conn.fetchrow(
            """
            SELECT id, priority FROM inspection_items
            WHERE status = 'PENDING'
            ORDER BY priority DESC, arrival_time ASC
            LIMIT 1;
            """
        )
        assert top_item is not None
        assert top_item["priority"] in [9, 10], f"Top item priority should be critical (9-10), got {top_item['priority']}"

        # Clean up
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    await close_db_pool()


@pytest.mark.asyncio
async def test_generator_api_endpoints():
    """Verify REST endpoints for generator control and status monitoring."""
    pool = await init_db_pool()
    generator_service.set_pool(pool)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Check initial status
        res = await client.get("/api/generator/status")
        assert res.status_code == 200
        data = res.json()
        assert "current_rate_per_sec" in data
        assert "is_running" in data

        # Start generator
        res = await client.post("/api/generator/start")
        assert res.status_code == 200
        assert res.json()["status"]["is_running"] is True

        # Stop generator
        res = await client.post("/api/generator/stop")
        assert res.status_code == 200
        assert res.json()["status"]["is_running"] is False

    await close_db_pool()
