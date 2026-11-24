import asyncio
import json
import time
from typing import List, Dict, Any
from app.config import settings
from app.db import init_db_pool, close_db_pool, get_db_pool

# Set socket for local PostgreSQL daemon if present
import os
if os.path.exists("/home/aspirant/Projects/realtime-review-queue/.tools/run"):
    settings.postgres_socket_dir = "/home/aspirant/Projects/realtime-review-queue/.tools/run"
    settings.postgres_port = 54329


async def verify_explain_analyze():
    """Verify that the partial index idx_inspection_items_pending is used by EXPLAIN ANALYZE."""
    pool = await get_db_pool()
    print("\n--- 1. EXPLAIN ANALYZE VERIFICATION ---")

    async with pool.acquire() as conn:
        # Clean inspection_items
        await conn.execute("DELETE FROM inspection_items;")

        # Seed 2,500 items: 1,500 PENDING, 500 PASSED, 300 FAILED, 200 CLAIMED
        records = []
        for i in range(1500):
            prio = (i % 10) + 1
            records.append((f"pend_{i:04d}", prio, 'PENDING', json.dumps({"part": f"P-{i}"})))
        for i in range(500):
            records.append((f"pass_{i:04d}", 5, 'PASSED', json.dumps({"part": f"P-pass-{i}"})))
        for i in range(300):
            records.append((f"fail_{i:04d}", 8, 'FAILED', json.dumps({"part": f"P-fail-{i}"})))
        for i in range(200):
            records.append((f"claim_{i:04d}", 6, 'CLAIMED', json.dumps({"part": f"P-claim-{i}"})))

        await conn.executemany(
            """
            INSERT INTO inspection_items (id, priority, status, payload, arrival_time)
            VALUES ($1, $2, $3, $4::jsonb, NOW() - (INTERVAL '1 second' * (random() * 1000)::int));
            """,
            records
        )

        total_items = await conn.fetchval("SELECT count(*) FROM inspection_items;")
        pending_items = await conn.fetchval("SELECT count(*) FROM inspection_items WHERE status = 'PENDING';")
        print(f"Seeded {total_items} items ({pending_items} PENDING).")

        # Run EXPLAIN ANALYZE on the atomic claim CTE
        query = """
        EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
        WITH next_item AS (
            SELECT id FROM inspection_items
            WHERE status = 'PENDING'
            ORDER BY priority DESC, arrival_time ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        UPDATE inspection_items
        SET status = 'CLAIMED',
            claimed_by = 'reviewer_bench',
            claimed_at = NOW(),
            lease_expires_at = NOW() + INTERVAL '8 seconds'
        FROM next_item
        WHERE inspection_items.id = next_item.id
        RETURNING inspection_items.id, inspection_items.priority;
        """
        raw_plan = await conn.fetchval(query)
        plan_json = json.loads(raw_plan)[0]
        
        # Output plan summary
        exec_time = plan_json["Execution Time"]
        planning_time = plan_json["Planning Time"]
        print(f"Planning Time: {planning_time:.3f} ms, Execution Time: {exec_time:.3f} ms")
        
        # Check subplan for next_item
        plan_str = json.dumps(plan_json, indent=2)
        used_index = "idx_inspection_items_pending" in plan_str
        print(f"Partial Index 'idx_inspection_items_pending' Used: {used_index}")
        if not used_index:
            print("WARNING: Partial index was NOT found in the plan tree!")
            print(plan_str)
        else:
            print("SUCCESS: PostgreSQL utilized idx_inspection_items_pending with Index Scan.")

        return used_index, exec_time


async def verify_concurrent_claiming():
    """Verify that concurrent transactions across multiple DB connections never double-claim."""
    pool = await get_db_pool()
    print("\n--- 2. CONCURRENT CLAIMING VERIFICATION ---")

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items;")
        # Seed 100 items with varying priorities (1 to 10)
        items = [
            (f"item_concur_{i:03d}", (i % 10) + 1, 'PENDING', json.dumps({"test_id": i}))
            for i in range(100)
        ]
        await conn.executemany(
            """
            INSERT INTO inspection_items (id, priority, status, payload, arrival_time)
            VALUES ($1, $2, $3, $4::jsonb, NOW() - (INTERVAL '1 millisecond' * (100 - $2)));
            """,
            items
        )

    claim_query = """
    WITH next_item AS (
        SELECT id FROM inspection_items
        WHERE status = 'PENDING'
        ORDER BY priority DESC, arrival_time ASC
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    UPDATE inspection_items
    SET status = 'CLAIMED',
        claimed_by = $1,
        claimed_at = NOW(),
        lease_expires_at = NOW() + INTERVAL '8 seconds'
    FROM next_item
    WHERE inspection_items.id = next_item.id
    RETURNING inspection_items.id, inspection_items.priority, inspection_items.claimed_by;
    """

    claims_by_reviewer: Dict[str, List[Dict[str, Any]]] = {}
    all_claimed_ids = []

    async def reviewer_worker(reviewer_id: str):
        claims_by_reviewer[reviewer_id] = []
        while True:
            # Each claim uses a distinct connection acquisition from pool
            async with pool.acquire() as conn:
                row = await conn.fetchrow(claim_query, reviewer_id)
                if not row:
                    break
                claim_data = dict(row)
                claim_data["timestamp"] = time.perf_counter()
                claims_by_reviewer[reviewer_id].append(claim_data)
                all_claimed_ids.append(row["id"])
                # Micro-jitter to simulate client turn
                await asyncio.sleep(0.005)

    # Launch 6 concurrent reviewers competing simultaneously
    num_reviewers = 6
    start_time = time.perf_counter()
    tasks = [reviewer_worker(f"reviewer_{r+1}") for r in range(num_reviewers)]
    await asyncio.gather(*tasks)
    duration = time.perf_counter() - start_time

    # Verification assertions
    total_claims = len(all_claimed_ids)
    unique_claims = len(set(all_claimed_ids))
    print(f"Total claims made: {total_claims}")
    print(f"Unique items claimed: {unique_claims}")
    print(f"Contention duration: {duration:.3f} s ({total_claims / duration:.1f} claims/sec)")

    for rev, claims in claims_by_reviewer.items():
        print(f"  {rev}: claimed {len(claims)} items")

    double_claims = total_claims - unique_claims
    if double_claims > 0:
        print(f"FAILURE: Detected {double_claims} DOUBLE-CLAIMED items!")
    else:
        print("SUCCESS: ZERO double-claims. Exactly-once handout held under 6-way concurrent contention.")

    return total_claims == 100 and double_claims == 0


async def verify_state_semantics_and_audit():
    """Verify RELEASED vs PENDING state semantics and atomic audit logging."""
    pool = await get_db_pool()
    print("\n--- 3. STATE SEMANTICS & AUDIT LOGGING VERIFICATION ---")

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inspection_items; DELETE FROM audit_events;")

        # Insert a single test item
        item_id = "test_item_release_01"
        await conn.execute(
            """
            INSERT INTO inspection_items (id, priority, status, payload)
            VALUES ($1, 9, 'PENDING', '{"part": "ReleaseTest"}');
            """,
            item_id
        )

        # 1. Reviewer claims item
        claim_result = await conn.fetchrow(
            """
            WITH next_item AS (
                SELECT id FROM inspection_items
                WHERE status = 'PENDING'
                ORDER BY priority DESC, arrival_time ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            ),
            updated_item AS (
                UPDATE inspection_items
                SET status = 'CLAIMED',
                    claimed_by = 'reviewer_1',
                    claimed_at = NOW(),
                    lease_expires_at = NOW() + INTERVAL '8 seconds'
                FROM next_item
                WHERE inspection_items.id = next_item.id
                RETURNING inspection_items.*
            ),
            logged_audit AS (
                INSERT INTO audit_events (item_id, event_type, reviewer_id, metadata)
                SELECT id, 'CLAIMED', 'reviewer_1', json_build_object('priority', priority)
                FROM updated_item
                RETURNING id
            )
            SELECT * FROM updated_item;
            """
        )
        assert claim_result is not None, "Claim should succeed"
        print("Step 1: Item successfully claimed with atomic audit event.")

        # 2. Reviewer voluntarily RELEASES item back to queue
        # CRITICAL TEST: Does releasing return status to 'PENDING'?
        release_result = await conn.fetchrow(
            """
            WITH released_item AS (
                UPDATE inspection_items
                SET status = 'PENDING',
                    claimed_by = NULL,
                    lease_expires_at = NULL
                WHERE id = $1 AND status = 'CLAIMED' AND claimed_by = 'reviewer_1'
                RETURNING *
            ),
            logged_audit AS (
                INSERT INTO audit_events (item_id, event_type, reviewer_id, metadata)
                SELECT id, 'RELEASED', 'reviewer_1', json_build_object('reason', 'manual_release')
                FROM released_item
                RETURNING id
            )
            SELECT * FROM released_item;
            """,
            item_id
        )
        assert release_result is not None, "Release should succeed"
        assert release_result["status"] == "PENDING", "Status MUST be 'PENDING' so it can be reclaimed!"
        assert release_result["claimed_by"] is None, "claimed_by must be cleared"
        print(f"Step 2: Item released. Status is '{release_result['status']}', claimed_by is NULL.")

        # 3. Can reviewer 2 now claim this released item?
        reclaim_result = await conn.fetchrow(
            """
            WITH next_item AS (
                SELECT id FROM inspection_items
                WHERE status = 'PENDING'
                ORDER BY priority DESC, arrival_time ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            ),
            updated_item AS (
                UPDATE inspection_items
                SET status = 'CLAIMED',
                    claimed_by = 'reviewer_2',
                    claimed_at = NOW(),
                    lease_expires_at = NOW() + INTERVAL '8 seconds'
                FROM next_item
                WHERE inspection_items.id = next_item.id
                RETURNING inspection_items.*
            ),
            logged_audit AS (
                INSERT INTO audit_events (item_id, event_type, reviewer_id, metadata)
                SELECT id, 'CLAIMED', 'reviewer_2', json_build_object('priority', priority)
                FROM updated_item
                RETURNING id
            )
            SELECT * FROM updated_item;
            """
        )
        assert reclaim_result is not None, "Released item MUST be reclaimable!"
        assert reclaim_result["id"] == item_id
        assert reclaim_result["claimed_by"] == "reviewer_2"
        print("Step 3: Reviewer 2 successfully claimed the previously released item.")

        # 4. Check audit log integrity
        events = await conn.fetch(
            "SELECT event_type, reviewer_id FROM audit_events WHERE item_id = $1 ORDER BY id ASC;",
            item_id
        )
        event_types = [e["event_type"] for e in events]
        print(f"Step 4: Audit trail for {item_id}: {event_types}")
        assert event_types == ["CLAIMED", "RELEASED", "CLAIMED"], f"Unexpected audit sequence: {event_types}"
        print("SUCCESS: State transitions and audit trail verified.")

        return True


async def main():
    await init_db_pool()
    try:
        index_ok, exec_time = await verify_explain_analyze()
        concur_ok = await verify_concurrent_claiming()
        state_ok = await verify_state_semantics_and_audit()
        print("\n==========================================")
        print("ALL VERIFICATIONS FINISHED:")
        print(f"1. Partial Index Scan: {'PASSED' if index_ok else 'FAILED'} (Execution: {exec_time:.2f} ms)")
        print(f"2. Concurrency (Zero Double Claims): {'PASSED' if concur_ok else 'FAILED'}")
        print(f"3. State Semantics & Atomic Audit: {'PASSED' if state_ok else 'FAILED'}")
        print("==========================================")
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
