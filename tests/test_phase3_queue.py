import asyncio
import time
import pytest
import httpx
from typing import List, Dict, Any
from app.config import settings
from app.db import init_db_pool, close_db_pool, get_db_pool
from app.generator import ItemFactory, LoadGenerator
from app.queue import queue_engine, ItemNotClaimedError
from app.main import app

# Local socket configuration if running in sandbox
import os
if os.path.exists("/home/aspirant/Projects/realtime-review-queue/.tools/run"):
    settings.postgres_socket_dir = "/home/aspirant/Projects/realtime-review-queue/.tools/run"
    settings.postgres_port = 54329


@pytest.mark.asyncio
async def test_reviewer_claim_and_judge_lifecycle():
    """Verify single reviewer lifecycle: peek, claim, idempotent claim, judge PASS."""
    pool = await init_db_pool()
    queue_engine.set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    generator = LoadGenerator(pool=pool)
    item = ItemFactory.create_item()
    item["priority"] = 9
    await generator.insert_items([item])

    # 1. Peek next item
    peeked = await queue_engine.peek_next_item()
    assert peeked is not None
    assert peeked["id"] == item["id"]
    assert peeked["priority"] == 9

    # 2. Claim item with reviewer_1
    claimed, already = await queue_engine.claim_next_item("reviewer_1")
    assert claimed is not None
    assert already is False
    assert claimed["id"] == item["id"]
    assert claimed["claimed_by"] == "reviewer_1"
    assert claimed["status"] == "CLAIMED"
    assert claimed["remaining_lease_seconds"] > 6.0

    # 3. Idempotent claim - reviewer_1 calls claim again without judging
    second_claim, already = await queue_engine.claim_next_item("reviewer_1")
    assert second_claim is not None
    assert already is True
    assert second_claim["id"] == item["id"]

    # 4. Check active claim for reviewer_1
    active = await queue_engine.get_active_claim_for_reviewer("reviewer_1")
    assert active is not None
    assert active["id"] == item["id"]

    # 5. Record PASS decision
    judged = await queue_engine.record_decision(
        item_id=item["id"],
        reviewer_id="reviewer_1",
        decision="PASS",
        notes="Micro-weld verified to aerospace tolerance."
    )
    assert judged["status"] == "PASSED"
    assert judged["decision"] == "PASS"

    # 6. Verify reviewer_1 has no active claim now
    active_now = await queue_engine.get_active_claim_for_reviewer("reviewer_1")
    assert active_now is None

    # 7. Verify audit sequence
    async with pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT event_type, reviewer_id FROM audit_events WHERE item_id = $1 ORDER BY id ASC;",
            item["id"]
        )
        assert [e["event_type"] for e in events] == ["CREATED", "CLAIMED", "PASSED"]

    await close_db_pool()


@pytest.mark.asyncio
async def test_voluntary_release_and_reclaim_lifecycle():
    """Verify reviewer can voluntarily release an item, returning it to PENDING for another reviewer."""
    pool = await init_db_pool()
    queue_engine.set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    generator = LoadGenerator(pool=pool)
    item = ItemFactory.create_item()
    item["priority"] = 10
    await generator.insert_items([item])

    # Reviewer 1 claims
    claimed, _ = await queue_engine.claim_next_item("reviewer_1")
    assert claimed["id"] == item["id"]

    # Reviewer 1 voluntarily releases
    released = await queue_engine.release_item(item["id"], "reviewer_1", reason="Need expert second opinion")
    assert released["status"] == "PENDING"
    assert released["claimed_by"] is None

    # Reviewer 2 claims the returned item
    reclaimed, _ = await queue_engine.claim_next_item("reviewer_2")
    assert reclaimed is not None
    assert reclaimed["id"] == item["id"]
    assert reclaimed["claimed_by"] == "reviewer_2"

    # Reviewer 2 marks FAIL
    judged = await queue_engine.record_decision(item["id"], "reviewer_2", "FAIL", "Confirmed through-wall crack")
    assert judged["status"] == "FAILED"

    # Audit events check
    async with pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT event_type, reviewer_id FROM audit_events WHERE item_id = $1 ORDER BY id ASC;",
            item["id"]
        )
        assert [e["event_type"] for e in events] == ["CREATED", "CLAIMED", "RELEASED", "CLAIMED", "FAILED"]

    await close_db_pool()


@pytest.mark.asyncio
async def test_unauthorized_and_stale_decision_protection():
    """Verify that reviewer A cannot decide or release an item claimed by reviewer B."""
    pool = await init_db_pool()
    queue_engine.set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    generator = LoadGenerator(pool=pool)
    item = ItemFactory.create_item()
    await generator.insert_items([item])

    # Reviewer 1 claims
    await queue_engine.claim_next_item("reviewer_1")

    # Reviewer 2 tries to judge Reviewer 1's item
    with pytest.raises(ItemNotClaimedError):
        await queue_engine.record_decision(item["id"], "reviewer_2", "PASS")

    # Reviewer 2 tries to release Reviewer 1's item
    with pytest.raises(ItemNotClaimedError):
        await queue_engine.release_item(item["id"], "reviewer_2")

    await close_db_pool()


@pytest.mark.asyncio
async def test_empty_queue_behavior():
    """Verify that claiming from an empty queue returns None without error."""
    pool = await init_db_pool()
    queue_engine.set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items;")

    claimed, already = await queue_engine.claim_next_item("reviewer_1")
    assert claimed is None
    assert already is False

    stats = await queue_engine.get_queue_stats()
    assert stats["pending_count"] == 0
    assert stats["next_preview"] is None

    await close_db_pool()


@pytest.mark.asyncio
async def test_concurrent_six_reviewer_contention_under_burst():
    """
    STRESS TEST: Simulates 6 reviewers continuously claiming, judging (PASS/FAIL),
    and draining a queue of 120 items dumped in a burst.
    Proves:
    1. Exactly-once handout (0 double assignments).
    2. Zero lost or orphaned items.
    3. Reviewers never block or deadlock each other.
    4. Queue drains completely to 0 pending.
    """
    pool = await init_db_pool()
    queue_engine.set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    # Seed 120 items with mixed priorities (1 to 10)
    items = [ItemFactory.create_item() for _ in range(120)]
    generator = LoadGenerator(pool=pool)
    await generator.insert_items(items)

    stats_before = await queue_engine.get_queue_stats()
    assert stats_before["pending_count"] == 120

    claims_by_reviewer: Dict[str, List[str]] = {f"reviewer_{r+1}": [] for r in range(6)}
    all_processed_ids = []
    lock = asyncio.Lock()

    async def reviewer_bot(rev_id: str):
        while True:
            # Claim next item
            item, _ = await queue_engine.claim_next_item(rev_id)
            if not item:
                # Queue empty
                break

            claims_by_reviewer[rev_id].append(item["id"])
            async with lock:
                all_processed_ids.append(item["id"])

            # Simulate inspection time (5ms - 15ms)
            await asyncio.sleep(0.008)

            # Record decision (PASS 80%, FAIL 20%)
            dec = "PASS" if hash(item["id"]) % 5 != 0 else "FAIL"
            await queue_engine.record_decision(item["id"], rev_id, dec)

    start_time = time.perf_counter()
    tasks = [reviewer_bot(f"reviewer_{r+1}") for r in range(6)]
    await asyncio.gather(*tasks)
    elapsed = time.perf_counter() - start_time

    # Verification assertions
    total_processed = len(all_processed_ids)
    unique_processed = len(set(all_processed_ids))

    print(f"\n--- Six Reviewer Contention Test Results ---")
    print(f"Total items processed: {total_processed} (Unique: {unique_processed})")
    print(f"Time to drain 120 items: {elapsed:.3f} s ({total_processed / elapsed:.1f} reviews/sec)")
    for rev_id, claims in claims_by_reviewer.items():
        print(f"  {rev_id}: {len(claims)} items processed")

    # Invariant 1: Exactly-once handout (no duplicates)
    assert total_processed == 120, f"Expected 120 items processed, got {total_processed}"
    assert unique_processed == 120, f"Detected duplicate claims! Expected 120 unique, got {unique_processed}"

    # Invariant 2: Queue completely drained
    stats_after = await queue_engine.get_queue_stats()
    assert stats_after["pending_count"] == 0, f"Expected 0 pending, got {stats_after['pending_count']}"
    assert stats_after["claimed_count"] == 0, f"Expected 0 claimed, got {stats_after['claimed_count']}"
    assert stats_after["passed_count"] + stats_after["failed_count"] == 120

    # Invariant 3: Audit trail verification
    async with pool.acquire() as conn:
        claim_events = await conn.fetchval("SELECT count(*) FROM audit_events WHERE event_type = 'CLAIMED';")
        decision_events = await conn.fetchval("SELECT count(*) FROM audit_events WHERE event_type IN ('PASSED', 'FAILED');")
        assert claim_events == 120
        assert decision_events == 120

    print("SUCCESS: 6 concurrent reviewers processed all 120 items with zero collisions and zero drops.")
    await close_db_pool()


@pytest.mark.asyncio
async def test_queue_api_endpoints():
    """Verify REST endpoints for claiming, judging, releasing, and queue statistics."""
    pool = await init_db_pool()
    queue_engine.set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    generator = LoadGenerator(pool=pool)
    item = ItemFactory.create_item()
    item["priority"] = 8
    await generator.insert_items([item])

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. Preview
        res = await client.get("/api/queue/preview")
        assert res.status_code == 200
        assert res.json()["preview"]["id"] == item["id"]

        # 2. Claim
        res = await client.post("/api/queue/claim", json={"reviewer_id": "reviewer_api"})
        assert res.status_code == 200
        claimed_data = res.json()["item"]
        assert claimed_data["id"] == item["id"]
        assert claimed_data["claimed_by"] == "reviewer_api"

        # 3. Active item lookup
        res = await client.get("/api/queue/active/reviewer_api")
        assert res.status_code == 200
        assert res.json()["active_item"]["id"] == item["id"]

        # 4. Release item
        res = await client.post("/api/queue/release", json={"item_id": item["id"], "reviewer_id": "reviewer_api"})
        assert res.status_code == 200
        assert res.json()["item"]["status"] == "PENDING"

        # 5. Reclaim by different reviewer
        res = await client.post("/api/queue/claim", json={"reviewer_id": "reviewer_api_2"})
        assert res.status_code == 200
        assert res.json()["item"]["id"] == item["id"]

        # 6. Judge item PASS
        res = await client.post("/api/queue/judge", json={
            "item_id": item["id"],
            "reviewer_id": "reviewer_api_2",
            "decision": "PASS",
            "notes": "Passed through API test"
        })
        assert res.status_code == 200
        assert res.json()["item"]["status"] == "PASSED"

        # 7. Check stats
        res = await client.get("/api/queue/stats")
        assert res.status_code == 200
        stats = res.json()
        assert stats["pending_count"] == 0
        assert stats["passed_count"] == 1
        assert stats["failed_count"] == 0

    await close_db_pool()
