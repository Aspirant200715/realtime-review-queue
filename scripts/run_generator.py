#!/usr/bin/env python3
"""
CLI runner for the inspection queue load generator.
Supports dry-run terminal inspection, live database ingestion, and manual burst triggers.
"""

import argparse
import asyncio
import os
import sys
import time
from typing import Optional
from app.config import settings
from app.db import init_db_pool, close_db_pool
from app.generator import ItemFactory, LoadGenerator


# Check if local test socket is active
if os.path.exists("/home/aspirant/Projects/realtime-review-queue/.tools/run"):
    settings.postgres_socket_dir = "/home/aspirant/Projects/realtime-review-queue/.tools/run"
    settings.postgres_port = 54329


async def run_dry_run(count: int, trigger_burst: bool, burst_size: int, burst_duration: float):
    print(f"\n=======================================================")
    print(f"LOAD GENERATOR DRY RUN (No Database Connection)")
    print(f"=======================================================\n")

    if trigger_burst:
        print(f">>> Triggering burst: {burst_size} items shaped over {burst_duration}s...")
        generator = LoadGenerator(pool=None, burst_size=burst_size, burst_duration=burst_duration)
        start_time = time.perf_counter()
        metrics = await generator.trigger_burst(size=burst_size, duration=burst_duration)
        elapsed = time.perf_counter() - start_time

        print(f"\n>>> Burst Results:")
        print(f"    - Items emitted: {metrics['items_generated']}")
        print(f"    - Elapsed time:  {metrics['elapsed_seconds']} s (Target: {burst_duration}s)")
        print(f"    - Ingestion rate: {metrics['effective_rate_per_sec']} items/s")
        print(f"    - Completed in under 10 seconds: {metrics['completed_in_under_10s']}")
        return

    print(f">>> Generating {count} inspection items to verify schema and priority spread...")
    critical_count = 0
    high_count = 0
    routine_count = 0

    for i in range(1, count + 1):
        item = ItemFactory.create_item()
        prio = item["priority"]
        cat = item["payload"]["category"]
        if prio in [9, 10]:
            critical_count += 1
        elif prio in [6, 7, 8]:
            high_count += 1
        else:
            routine_count += 1

        if i <= 10 or i % 50 == 0 or i == count:
            print(f"[{i:04d}] ID={item['id']} | Prio={prio:02d} ({cat:<8}) | Part={item['payload']['part_number']} | Defect={item['payload']['defect_type']} ({item['payload']['confidence_score']:.2f})")

    print(f"\n>>> Priority Distribution Summary ({count} total items):")
    print(f"    - Critical (Prio 9-10): {critical_count:4d} ({critical_count / count * 100:.1f}%) [Target: ~10%]")
    print(f"    - High     (Prio 6-8):  {high_count:4d} ({high_count / count * 100:.1f}%) [Target: ~25%]")
    print(f"    - Routine  (Prio 1-5):  {routine_count:4d} ({routine_count / count * 100:.1f}%) [Target: ~65%]")


async def run_live(args):
    print(f"\nConnecting to PostgreSQL at {settings.get_dsn()}...")
    pool = await init_db_pool()
    print("Database pool connected.")

    generator = LoadGenerator(
        pool=pool,
        steady_rate=args.steady_rate,
        burst_size=args.burst_size,
        burst_duration=args.burst_duration,
        min_burst_interval=args.min_burst_interval,
        max_burst_interval=args.max_burst_interval
    )

    try:
        if args.burst_now:
            print(f"\n>>> Firing immediate burst of {args.burst_size} items into PostgreSQL...")
            metrics = await generator.trigger_burst(size=args.burst_size, duration=args.burst_duration)
            print(f">>> Burst completed: {metrics['items_generated']} items in {metrics['elapsed_seconds']}s ({metrics['effective_rate_per_sec']} items/s).")
            async with pool.acquire() as conn:
                pending = await conn.fetchval("SELECT count(*) FROM inspection_items WHERE status = 'PENDING';")
                total = await conn.fetchval("SELECT count(*) FROM inspection_items;")
                print(f">>> Database State: Total items: {total}, Currently PENDING: {pending}")
            return

        print(f"\n>>> Starting Live Load Generator:")
        print(f"    - Steady Rate: {args.steady_rate} items/sec")
        print(f"    - Burst Size: {args.burst_size} items in < 10s")
        print(f"    - Unpredictable Burst Interval: {args.min_burst_interval}s to {args.max_burst_interval}s")
        print("Press Ctrl+C to stop.\n")

        generator.start()

        run_duration = args.duration
        start_t = time.time()
        while True:
            await asyncio.sleep(2.0)
            metrics = generator.get_metrics()
            async with pool.acquire() as conn:
                pending = await conn.fetchval("SELECT count(*) FROM inspection_items WHERE status = 'PENDING';")
            print(f"[LIVE STATUS] Rate: {metrics['current_rate_per_sec']:5.1f} items/s | Total Generated: {metrics['total_items_generated']:5d} | Bursts: {metrics['total_bursts_triggered']:2d} | Queue Backlog: {pending:5d}")

            if run_duration and (time.time() - start_t) >= run_duration:
                print(f"\nDuration limit ({run_duration}s) reached. Stopping...")
                break

    except KeyboardInterrupt:
        print("\nStopping load generator...")
    finally:
        generator.stop()
        await close_db_pool()
        print("Generator stopped and database connection closed.")


def main():
    parser = argparse.ArgumentParser(description="Inspection Queue Realistic Load Generator")
    parser.add_argument("--mode", choices=["dry-run", "live"], default="dry-run", help="Execution mode (dry-run or live DB)")
    parser.add_argument("--count", type=int, default=500, help="Number of items to generate in dry-run mode (default 500)")
    parser.add_argument("--burst-now", action="store_true", help="Immediately fire a 250-item burst")
    parser.add_argument("--burst-size", type=int, default=250, help="Number of items per burst (default 250)")
    parser.add_argument("--burst-duration", type=float, default=8.0, help="Burst duration in seconds (default 8.0, must be <10s)")
    parser.add_argument("--steady-rate", type=float, default=5.0, help="Target steady arrival rate per second (default 5.0)")
    parser.add_argument("--min-burst-interval", type=float, default=30.0, help="Minimum seconds between bursts")
    parser.add_argument("--max-burst-interval", type=float, default=60.0, help="Maximum seconds between bursts")
    parser.add_argument("--duration", type=float, default=None, help="Total seconds to run in live mode before exiting")

    args = parser.parse_args()

    if args.mode == "dry-run":
        asyncio.run(run_dry_run(
            count=args.count,
            trigger_burst=args.burst_now,
            burst_size=args.burst_size,
            burst_duration=args.burst_duration
        ))
    else:
        asyncio.run(run_live(args))


if __name__ == "__main__":
    main()
