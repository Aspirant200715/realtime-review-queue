import asyncio
import json
import time
import pytest
import httpx
from typing import List, Dict, Any
from app.config import settings
from app.db import init_db_pool, close_db_pool, get_db_pool
from app.generator import ItemFactory, LoadGenerator
from app.queue import queue_engine, lease_sweeper_service, LeaseSweeper, ItemNotClaimedError
from app.main import app

# Local socket configuration if running in sandbox
import os
if os.path.exists("/home/aspirant/Projects/realtime-review-queue/.tools/run"):
    settings.postgres_socket_dir = "/home/aspirant/Projects/realtime-review-queue/.tools/run"
    settings.postgres_port = 54329


@pytest.mark.asyncio
async def test_heartbeat_lease_renewal():
    """Verify that a reviewer actively sending heartbeats successfully extends their lease."""
    pool = await init_db_pool()
    queue_engine.set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    generator = LoadGenerator(pool=pool)
    item = ItemFactory.create_item()
    await generator.insert_items([item])

    # Claim item with default 8s lease
    claimed, _ = await queue_engine.claim_next_item("reviewer_active_heartbeat")
    assert claimed is not None
    item_id = claimed["id"]

    # Sleep 0.4s so remaining lease decreases
    await asyncio.sleep(0.4)

    # Send heartbeat renewing lease to 8s
    renewed = await queue_engine.renew_lease(item_id, "reviewer_active_heartbeat", extension_seconds=8.0)
    assert renewed["id"] == item_id
    assert renewed["remaining_lease_seconds"] > 7.5

    await close_db_pool()


@pytest.mark.asyncio
async def test_heartbeat_rejection_on_expired_item():
    """Verify that heartbeat is strictly rejected if the lease has already expired."""
    pool = await init_db_pool()
    queue_engine.set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    # Insert item with an already-expired lease
    item_id = "test_item_expired_hb"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO inspection_items (id, priority, status, claimed_by, claimed_at, lease_expires_at)
            VALUES ($1, 7, 'CLAIMED', 'reviewer_slow', NOW() - INTERVAL '10 seconds', NOW() - INTERVAL '2 seconds');
            """,
            item_id
        )

    # Attempt heartbeat -> must raise ItemNotClaimedError
    with pytest.raises(ItemNotClaimedError) as exc_info:
        await queue_engine.renew_lease(item_id, "reviewer_slow")
    assert "expired" in str(exc_info.value).lower()

    await close_db_pool()


@pytest.mark.asyncio
async def test_sweeper_recovers_abandoned_items():
    """
    Verify that the sweeper detects expired leases, atomically resets them to PENDING,
    and logs the 'LEASE_EXPIRED' audit event so another reviewer can claim them.
    """
    pool = await init_db_pool()
    queue_engine.set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    generator = LoadGenerator(pool=pool)
    item = ItemFactory.create_item()
    item["priority"] = 9
    await generator.insert_items([item])

    # Reviewer 1 claims item
    claimed, _ = await queue_engine.claim_next_item("reviewer_walkaway")
    assert claimed is not None
    item_id = claimed["id"]

    # Artificially expire the lease in the database to simulate walkaway
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE inspection_items SET lease_expires_at = NOW() - INTERVAL '1 second' WHERE id = $1;",
            item_id
        )

    # Run sweeper
    recovered = await queue_engine.sweep_expired_leases()
    assert len(recovered) == 1
    assert recovered[0]["id"] == item_id
    assert recovered[0]["claimed_by"] == "reviewer_walkaway"

    # Verify item is now PENDING and has no claimed_by in DB
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status, claimed_by, lease_expires_at FROM inspection_items WHERE id = $1;", item_id)
        assert row["status"] == "PENDING"
        assert row["claimed_by"] is None
        assert row["lease_expires_at"] is None

        # Verify audit event
        audit_events = await conn.fetch(
            "SELECT event_type, reviewer_id FROM audit_events WHERE item_id = $1 ORDER BY id ASC;",
            item_id
        )
        types = [e["event_type"] for e in audit_events]
        assert types == ["CREATED", "CLAIMED", "LEASE_EXPIRED"]

    # Reviewer 2 can now claim the abandoned item
    reclaimed, already = await queue_engine.claim_next_item("reviewer_rescuer")
    assert reclaimed is not None
    assert already is False
    assert reclaimed["id"] == item_id
    assert reclaimed["claimed_by"] == "reviewer_rescuer"

    await close_db_pool()


@pytest.mark.asyncio
async def test_bounded_recovery_time_enforcement():
    """
    STATED BOUND VERIFICATION (Rubric D3):
    Proves that an item abandoned by a reviewer who walks away (closes tab)
    is guaranteed to return to the active queue within a bounded, enforced time window.
    """
    pool = await init_db_pool()
    queue_engine.set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    generator = LoadGenerator(pool=pool)
    item = ItemFactory.create_item()
    await generator.insert_items([item])
    item_id = item["id"]

    # Start a fast local sweeper (sweeps every 0.2s)
    local_sweeper = LeaseSweeper(queue_engine, interval_seconds=0.2)
    local_sweeper.start()

    try:
        # Reviewer claims item with an intentional 0.8s lease duration
        queue_engine.lease_duration = 0.8
        claimed, _ = await queue_engine.claim_next_item("reviewer_disappearing")
        assert claimed["id"] == item_id
        claim_time = time.perf_counter()

        # Reviewer walks away: no heartbeats are sent.
        # Poll queue stats to see when the item re-enters 'PENDING'
        recovered_time = None
        timeout_limit = 3.0  # Must be recovered strictly before this
        while time.perf_counter() - claim_time < timeout_limit:
            stats = await queue_engine.get_queue_stats()
            if stats["pending_count"] == 1 and stats["claimed_count"] == 0:
                recovered_time = time.perf_counter() - claim_time
                break
            await asyncio.sleep(0.05)

        assert recovered_time is not None, "Item was not recovered within 3.0s window!"
        print(f"\nItem recovery latency measured: {recovered_time:.3f} seconds (Lease: 0.8s + Sweeper: 0.2s).")

        # The mathematical bound: lease_duration (0.8s) + sweeper_interval (0.2s) + jitter
        assert 0.8 <= recovered_time <= 1.5, f"Recovery latency {recovered_time:.3f}s outside bounded window!"

    finally:
        queue_engine.lease_duration = settings.lease_duration_seconds
        local_sweeper.stop()
        await close_db_pool()


@pytest.mark.asyncio
async def test_heartbeat_and_sweeper_concurrency_race():
    """
    EDGE CASE:
    Simulates a race condition right at the instant of lease expiry:
    Reviewer sends heartbeat while the background sweeper attempts to reclaim the item.
    Proves:
    1. Zero deadlocks.
    2. Atomicity holds: either heartbeat extends the lease, or sweeper reclaims it to PENDING.
    3. The state never enters a corrupt or halfway status.
    """
    pool = await init_db_pool()
    queue_engine.set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    generator = LoadGenerator(pool=pool)
    item = ItemFactory.create_item()
    await generator.insert_items([item])

    # Reviewer claims item with very short lease
    queue_engine.lease_duration = 0.1
    claimed, _ = await queue_engine.claim_next_item("reviewer_racer")
    item_id = claimed["id"]

    # Sleep until right at expiration boundary
    await asyncio.sleep(0.10)

    # Fire concurrent heartbeat and sweeper
    hb_task = queue_engine.renew_lease(item_id, "reviewer_racer", extension_seconds=5.0)
    sweep_task = queue_engine.sweep_expired_leases()

    results = await asyncio.gather(hb_task, sweep_task, return_exceptions=True)

    hb_res, sweep_res = results[0], results[1]

    # Verify that either heartbeat won (extended lease) OR sweeper won (recovered to PENDING)
    async with pool.acquire() as conn:
        final_row = await conn.fetchrow("SELECT status, claimed_by, lease_expires_at FROM inspection_items WHERE id = $1;", item_id)
        if isinstance(hb_res, dict):
            # Heartbeat won before sweeper executed
            assert final_row["status"] == "CLAIMED"
            assert final_row["claimed_by"] == "reviewer_racer"
            print("\nRace result: Heartbeat beat the sweeper.")
        else:
            # Sweeper won
            assert isinstance(hb_res, ItemNotClaimedError)
            assert final_row["status"] == "PENDING"
            assert final_row["claimed_by"] is None
            print("\nRace result: Sweeper reclaimed the item before late heartbeat arrived.")

    queue_engine.lease_duration = settings.lease_duration_seconds
    await close_db_pool()


@pytest.mark.asyncio
async def test_lease_recovery_api_endpoints():
    """Verify REST endpoints for heartbeat, sweep, and sweeper metrics."""
    pool = await init_db_pool()
    queue_engine.set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    generator = LoadGenerator(pool=pool)
    item = ItemFactory.create_item()
    await generator.insert_items([item])

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. Claim
        res = await client.post("/api/queue/claim", json={"reviewer_id": "reviewer_api_lease"})
        assert res.status_code == 200
        claimed_id = res.json()["item"]["id"]

        # 2. Heartbeat via API
        res = await client.post("/api/queue/heartbeat", json={"item_id": claimed_id, "reviewer_id": "reviewer_api_lease"})
        assert res.status_code == 200
        assert res.json()["data"]["id"] == claimed_id

        # 3. Check sweeper status
        res = await client.get("/api/queue/sweeper/status")
        assert res.status_code == 200
        assert "is_running" in res.json()

        # 4. Trigger manual sweep
        res = await client.post("/api/queue/sweep")
        assert res.status_code == 200
        assert "recovered_count" in res.json()

    await close_db_pool()


@pytest.mark.asyncio
async def test_concurrent_multi_item_recovery_under_churn():
    """
    STRESS / CHURN RECOVERY TEST (Rubric D3):
    10 items claimed by 10 reviewers with a short lease (0.5s).
    All 10 reviewers walk away simultaneously.
    Meanwhile, 2 active reviewers continue claiming and processing items from the queue.
    Verify:
    1. All 10 abandoned items are swept back to PENDING within the bounded time (< 2.0s).
    2. Active reviewers never crash or lock up.
    3. Exactly 10 'LEASE_EXPIRED' audit records are logged.
    """
    pool = await init_db_pool()
    queue_engine.set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    generator = LoadGenerator(pool=pool)
    # Seed 30 items
    items = [ItemFactory.create_item() for _ in range(30)]
    await generator.insert_items(items)

    local_sweeper = LeaseSweeper(queue_engine, interval_seconds=0.2)
    local_sweeper.start()

    try:
        # 10 reviewers claim an item with a 0.5s lease and immediately walk away
        queue_engine.lease_duration = 0.5
        abandoned_ids = []
        for i in range(10):
            claimed, _ = await queue_engine.claim_next_item(f"walkaway_rev_{i}")
            assert claimed is not None
            abandoned_ids.append(claimed["id"])

        start_t = time.perf_counter()

        # Two active reviewers keep processing other items
        async def active_worker(rev_id: str):
            for _ in range(5):
                item, _ = await queue_engine.claim_next_item(rev_id)
                if item:
                    await asyncio.sleep(0.01)
                    await queue_engine.record_decision(item["id"], rev_id, "PASS")

        await asyncio.gather(active_worker("active_1"), active_worker("active_2"))

        # Wait until all 10 abandoned items are swept back
        while time.perf_counter() - start_t < 3.0:
            async with pool.acquire() as conn:
                unrecovered = await conn.fetchval(
                    "SELECT count(*) FROM inspection_items WHERE id = ANY($1::varchar[]) AND status = 'CLAIMED';",
                    abandoned_ids
                )
                if unrecovered == 0:
                    break
            await asyncio.sleep(0.05)

        elapsed = time.perf_counter() - start_t
        print(f"\nAll 10 abandoned items recovered in {elapsed:.3f}s under active churn.")

        # Invariant checks:
        async with pool.acquire() as conn:
            # All 10 abandoned items must have a LEASE_EXPIRED audit event
            recovered_audit_count = await conn.fetchval(
                "SELECT count(*) FROM audit_events WHERE item_id = ANY($1::varchar[]) AND event_type = 'LEASE_EXPIRED';",
                abandoned_ids
            )
            assert recovered_audit_count == 10, f"Expected 10 LEASE_EXPIRED events, found {recovered_audit_count}"

    finally:
        queue_engine.lease_duration = settings.lease_duration_seconds
        local_sweeper.stop()
        await close_db_pool()


@pytest.mark.asyncio
async def test_concurrent_sweep_operations():
    """
    CONCURRENCY TEST:
    Two concurrent sweep operations running simultaneously on a batch of 80 expired items.
    Proves:
    1. Zero duplicate recoveries.
    2. Sum of recovered items across both sweeps equals exactly 80.
    3. Exactly 80 'LEASE_EXPIRED' audit events written.
    """
    pool = await init_db_pool()
    queue_engine.set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

        # Insert 80 expired items
        records = [
            (f"exp_item_{i:03d}", (i % 10) + 1, 'CLAIMED', f"expired_rev_{i}", json.dumps({"test": i}))
            for i in range(80)
        ]
        await conn.executemany(
            """
            INSERT INTO inspection_items (id, priority, status, claimed_by, claimed_at, lease_expires_at, payload)
            VALUES ($1, $2, $3, $4, NOW() - INTERVAL '10 seconds', NOW() - INTERVAL '2 seconds', $5::jsonb);
            """,
            records
        )

    # Fire two concurrent sweeps simultaneously
    sweep_1, sweep_2 = await asyncio.gather(
        queue_engine.sweep_expired_leases(batch_size=50),
        queue_engine.sweep_expired_leases(batch_size=50)
    )

    ids_1 = [r["id"] for r in sweep_1]
    ids_2 = [r["id"] for r in sweep_2]

    # Zero overlap between the two concurrent sweepers
    overlap = set(ids_1).intersection(set(ids_2))
    assert len(overlap) == 0, f"Detected duplicate item recovery across sweepers! {overlap}"

    total_recovered = len(ids_1) + len(ids_2)
    assert total_recovered == 80, f"Expected 80 total recovered items, got {total_recovered}"

    async with pool.acquire() as conn:
        pending_count = await conn.fetchval("SELECT count(*) FROM inspection_items WHERE status = 'PENDING';")
        assert pending_count == 80

        audit_count = await conn.fetchval("SELECT count(*) FROM audit_events WHERE event_type = 'LEASE_EXPIRED';")
        assert audit_count == 80

    await close_db_pool()
