import asyncio
import time
import pytest
from typing import List, Dict, Any
from app.config import settings
from app.db import init_db_pool, close_db_pool, get_db_pool
from app.generator import ItemFactory, LoadGenerator
from app.queue import queue_engine, ItemNotClaimedError

# Local socket configuration if running in sandbox
import os
if os.path.exists("/home/aspirant/Projects/realtime-review-queue/.tools/run"):
    settings.postgres_socket_dir = "/home/aspirant/Projects/realtime-review-queue/.tools/run"
    settings.postgres_port = 54329


@pytest.mark.asyncio
async def test_simultaneous_claims_same_reviewer():
    """
    CRITICAL EDGE CASE (Invariant 3):
    Five simultaneous claim requests fired for the SAME reviewer at the exact same millisecond.
    Proves:
    1. The reviewer receives the exact same item in all concurrent requests (idempotency).
    2. The reviewer holds exactly ONE active item in the database (never multiple).
    3. Exactly ONE 'CLAIMED' audit event is written.
    """
    pool = await init_db_pool()
    queue_engine.set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    generator = LoadGenerator(pool=pool)
    items = [ItemFactory.create_item() for _ in range(10)]
    await generator.insert_items(items)

    rev_id = "reviewer_race_test"

    # Fire 5 simultaneous claims concurrently for the same reviewer
    results = await asyncio.gather(
        queue_engine.claim_next_item(rev_id),
        queue_engine.claim_next_item(rev_id),
        queue_engine.claim_next_item(rev_id),
        queue_engine.claim_next_item(rev_id),
        queue_engine.claim_next_item(rev_id),
        return_exceptions=True
    )

    # Check for unexpected exceptions
    for res in results:
        assert not isinstance(res, Exception), f"Unexpected exception in concurrent claim: {res}"

    # Extract claimed item IDs
    claimed_ids = [res[0]["id"] for res in results if res[0] is not None]
    assert len(claimed_ids) == 5

    # Invariant: All 5 calls must have resolved to the EXACT SAME item ID!
    assert len(set(claimed_ids)) == 1, f"Reviewer got different items concurrently! {claimed_ids}"
    assigned_item_id = claimed_ids[0]

    # Database state verification
    async with pool.acquire() as conn:
        active_items_count = await conn.fetchval(
            "SELECT count(*) FROM inspection_items WHERE claimed_by = $1 AND status = 'CLAIMED';",
            rev_id
        )
        assert active_items_count == 1, f"Expected 1 active item in DB, found {active_items_count}"

        audit_claims_count = await conn.fetchval(
            "SELECT count(*) FROM audit_events WHERE item_id = $1 AND event_type = 'CLAIMED';",
            assigned_item_id
        )
        assert audit_claims_count == 1, f"Expected exactly 1 CLAIMED audit record, found {audit_claims_count}"

    await close_db_pool()


@pytest.mark.asyncio
async def test_simultaneous_decisions_same_item():
    """
    EDGE CASE (Invariant 5):
    Two simultaneous decision requests (e.g. rapid double click on PASS and FAIL).
    Proves:
    1. Exactly one decision commits.
    2. The other request fails with ItemNotClaimedError (HTTP 409).
    3. Exactly one decision audit record is written.
    """
    pool = await init_db_pool()
    queue_engine.set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    generator = LoadGenerator(pool=pool)
    item = ItemFactory.create_item()
    await generator.insert_items([item])

    # Claim item
    claimed, _ = await queue_engine.claim_next_item("reviewer_double_judge")
    assert claimed is not None
    item_id = claimed["id"]

    # Fire two concurrent decisions (one PASS, one FAIL)
    results = await asyncio.gather(
        queue_engine.record_decision(item_id, "reviewer_double_judge", "PASS"),
        queue_engine.record_decision(item_id, "reviewer_double_judge", "FAIL"),
        return_exceptions=True
    )

    successes = [r for r in results if isinstance(r, dict)]
    failures = [r for r in results if isinstance(r, ItemNotClaimedError)]

    assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}"
    assert len(failures) == 1, f"Expected exactly 1 ItemNotClaimedError, got {len(failures)}"

    # Check DB state
    async with pool.acquire() as conn:
        status_val = await conn.fetchval("SELECT status FROM inspection_items WHERE id = $1;", item_id)
        assert status_val in ("PASSED", "FAILED")

        audit_count = await conn.fetchval(
            "SELECT count(*) FROM audit_events WHERE item_id = $1 AND event_type IN ('PASSED', 'FAILED');",
            item_id
        )
        assert audit_count == 1, f"Expected exactly 1 decision audit event, found {audit_count}"

    await close_db_pool()


@pytest.mark.asyncio
async def test_concurrent_release_vs_decision():
    """
    EDGE CASE (Invariants 5 & 7):
    Simultaneous release and decision requests for the same item.
    Proves:
    1. Exactly one operation wins.
    2. The second operation is rejected cleanly without corrupting state.
    """
    pool = await init_db_pool()
    queue_engine.set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    generator = LoadGenerator(pool=pool)
    item = ItemFactory.create_item()
    await generator.insert_items([item])

    claimed, _ = await queue_engine.claim_next_item("reviewer_race_rel_dec")
    item_id = claimed["id"]

    results = await asyncio.gather(
        queue_engine.release_item(item_id, "reviewer_race_rel_dec", reason="racing release"),
        queue_engine.record_decision(item_id, "reviewer_race_rel_dec", "PASS"),
        return_exceptions=True
    )

    successes = [r for r in results if isinstance(r, dict)]
    failures = [r for r in results if isinstance(r, ItemNotClaimedError)]

    assert len(successes) == 1
    assert len(failures) == 1

    # Verify final state is consistent (either PENDING or PASSED)
    async with pool.acquire() as conn:
        final_status = await conn.fetchval("SELECT status FROM inspection_items WHERE id = $1;", item_id)
        assert final_status in ("PENDING", "PASSED")

    await close_db_pool()


@pytest.mark.asyncio
async def test_full_250_item_burst_contention():
    """
    HARD CASE TEST (Rubric D1 & D5):
    A full 250-item burst drained by 6 concurrent reviewer bots competing simultaneously.
    Proves zero double-claims, zero dropped items, and complete queue drainage under full burst size.
    """
    pool = await init_db_pool()
    queue_engine.set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    # Ingest full 250 items
    generator = LoadGenerator(pool=pool)
    items = [ItemFactory.create_item() for _ in range(250)]
    await generator.insert_items(items)

    all_processed: List[str] = []
    lock = asyncio.Lock()

    async def bot(rev_id: str):
        while True:
            item, _ = await queue_engine.claim_next_item(rev_id)
            if not item:
                break
            async with lock:
                all_processed.append(item["id"])
            await asyncio.sleep(0.005)
            await queue_engine.record_decision(item["id"], rev_id, "PASS")

    start_t = time.perf_counter()
    tasks = [bot(f"rev_{i+1}") for i in range(6)]
    await asyncio.gather(*tasks)
    duration = time.perf_counter() - start_t

    print(f"\n250-Item Burst drained in {duration:.2f}s ({len(all_processed) / duration:.1f} reviews/s)")

    # Assertions
    assert len(all_processed) == 250, f"Expected 250 processed, got {len(all_processed)}"
    assert len(set(all_processed)) == 250, "Detected duplicate claims across reviewers!"

    stats = await queue_engine.get_queue_stats()
    assert stats["pending_count"] == 0
    assert stats["claimed_count"] == 0
    assert stats["passed_count"] == 250

    await close_db_pool()


@pytest.mark.asyncio
async def test_audit_consistency_on_failure():
    """
    EDGE CASE (Invariant 8):
    Verifies that rejected / failed requests write zero audit records.
    """
    pool = await init_db_pool()
    queue_engine.set_pool(pool)

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

    # Try judging a non-existent item
    with pytest.raises(ItemNotClaimedError):
        await queue_engine.record_decision("non_existent_item", "reviewer_ghost", "PASS")

    # Verify zero audit records created
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM audit_events;")
        assert count == 0

    await close_db_pool()
