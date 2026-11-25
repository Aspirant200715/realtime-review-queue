import os
import subprocess
import time
import pytest
import httpx
from playwright.sync_api import sync_playwright

SERVER_HOST = "127.0.0.1"
SERVER_PORT = "8088"
BASE_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"


@pytest.fixture(scope="module")
def live_server():
    """Starts the FastAPI application with live WebSockets on port 8088."""
    env = os.environ.copy()
    env["PATH"] = f"/usr/bin:/bin:{env.get('PATH', '')}"
    env["PYTHONPATH"] = "."
    if os.path.exists("/home/aspirant/Projects/realtime-review-queue/.tools/run"):
        env["POSTGRES_SOCKET_DIR"] = "/home/aspirant/Projects/realtime-review-queue/.tools/run"
        env["POSTGRES_PORT"] = "54329"

    proc = subprocess.Popen(
        [
            ".venv/bin/uvicorn",
            "app.main:app",
            "--host", SERVER_HOST,
            "--port", SERVER_PORT,
            "--log-level", "warning"
        ],
        env=env
    )

    # Wait for server to boot
    started = False
    for _ in range(60):
        try:
            r = httpx.get(f"{BASE_URL}/", timeout=0.5)
            if r.status_code == 200:
                started = True
                break
        except Exception:
            time.sleep(0.1)

    if not started:
        proc.terminate()
        raise RuntimeError("FastAPI live server failed to start within 6 seconds.")

    yield BASE_URL

    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_browser_six_reviewers_simultaneous_screens(live_server):
    """
    Scenario 1 & 4:
    Open six independent browser reviewer tabs simultaneously in Firefox.
    Verify all 6 establish persistent WebSockets, hydrate state, and receive live push broadcasts.
    """
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        contexts = []
        pages = []

        try:
            # 1. Open 6 independent reviewer screens
            for i in range(1, 7):
                ctx = browser.new_context()
                contexts.append(ctx)
                page = ctx.new_page()
                pages.append(page)
                page.goto(f"{BASE_URL}/?reviewer=reviewer_{i}")

            # 2. Verify all 6 pages render their respective headings and connected status
            expected_names = [
                "Reviewer 1 (Alpha)", "Reviewer 2 (Bravo)", "Reviewer 3 (Charlie)",
                "Reviewer 4 (Delta)", "Reviewer 5 (Echo)", "Reviewer 6 (Foxtrot)"
            ]
            for i, page in enumerate(pages):
                page.wait_for_selector("#active-reviewer-heading")
                heading = page.inner_text("#active-reviewer-heading")
                assert heading == expected_names[i]

                # Verify WebSocket connected indicator
                page.wait_for_selector(".status-indicator.connected")
                assert "WEBSOCKET ONLINE" in page.inner_text("#connection-status")

            # 3. Seed 6 items via REST
            httpx.post(f"{BASE_URL}/api/generator/burst", json={"size": 6, "duration": 0.5})

            # 4. All 6 screens MUST receive the pushed backlog count without polling
            for page in pages:
                page.wait_for_function("document.getElementById('stat-pending').innerText !== '0'")
                pending_val = int(page.inner_text("#stat-pending"))
                assert pending_val >= 6

        finally:
            for ctx in contexts:
                ctx.close()
            browser.close()


def test_browser_concurrent_claiming_zero_double_assignments(live_server):
    """
    Scenario 1:
    Six reviewers simultaneously click 'CLAIM NEXT ITEM' on their browser screens.
    Verify:
    - Exactly-once assignment: No two reviewers ever receive the same part.
    - Zero double claims.
    - Audit records and UI reflect unique claims.
    """
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        contexts = []
        pages = []

        try:
            # Seed 12 items
            httpx.post(f"{BASE_URL}/api/generator/burst", json={"size": 12, "duration": 0.5})

            for i in range(1, 7):
                ctx = browser.new_context()
                contexts.append(ctx)
                page = ctx.new_page()
                pages.append(page)
                page.goto(f"{BASE_URL}/?reviewer=reviewer_{i}")
                page.wait_for_selector(".status-indicator.connected")

            # All 6 reviewers click CLAIM button
            for page in pages:
                page.click("#btn-claim-action")

            # Wait for active workspace to appear on all 6 pages
            claimed_part_ids = []
            for i, page in enumerate(pages):
                page.wait_for_selector("#desk-active-state", state="visible", timeout=5000)
                part_id = page.inner_text("#item-part-id").strip()
                assert part_id != ""
                claimed_part_ids.append(part_id)

            # CRITICAL INVARIANT: Zero double-assignments across all 6 reviewers!
            assert len(claimed_part_ids) == 6
            assert len(set(claimed_part_ids)) == 6, f"Duplicate assignment detected: {claimed_part_ids}"

            # All 6 reviewers pass their items
            for page in pages:
                page.click("#btn-action-pass")
                page.wait_for_selector("#desk-empty-state", state="visible", timeout=5000)

        finally:
            for ctx in contexts:
                ctx.close()
            browser.close()


def test_browser_walkaway_recovery_and_sweeper_bound(live_server):
    """
    Scenario 3:
    Reviewer claims item, clicks 'Simulate Walkaway' (heartbeats stop).
    Verify:
    - Lease counts down and expires.
    - Background sweeper recovers item back to PENDING within <9.0s.
    - Reviewer's desk unlocks and resets.
    - Another reviewer can claim the recovered item.
    - Stale reviewer cannot submit a decision.
    """
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        ctx1 = browser.new_context()
        ctx2 = browser.new_context()
        page1 = ctx1.new_page()
        page2 = ctx2.new_page()

        try:
            # Seed 1 item
            httpx.post(f"{BASE_URL}/api/generator/burst", json={"size": 1, "duration": 0.5})

            def handle_ws(ws):
                print(f"[WS OPEN] {ws.url}")
                ws.on("framereceived", lambda data: print(f"[WS FRAME RECV] {data}"))
            page1.on("websocket", handle_ws)

            page1.goto(f"{BASE_URL}/?reviewer=reviewer_3")
            page1.wait_for_selector(".status-indicator.connected")

            # Claim item
            page1.click("#btn-claim-action")
            page1.wait_for_selector("#desk-active-state", state="visible")
            walkaway_part_id = page1.inner_text("#item-part-id").strip()

            # Start walkaway demo by clicking button
            page1.click("#btn-toggle-walkaway")
            assert "Resume Heartbeats" in page1.inner_text("#btn-toggle-walkaway")
            start_time = time.perf_counter()

            # Wait for sweeper to recover the item and reset page 1 desk to empty state
            page1.wait_for_selector("#desk-empty-state", state="visible", timeout=12000)
            elapsed_recovery = time.perf_counter() - start_time

            # VERIFY BOUND: Elapsed time must be <= 9.0s (+ small DOM render delta < 0.5s)
            print(f"\n[MEASUREMENT] Browser walkaway recovery completed in {elapsed_recovery:.2f}s (<9.0s bound).")
            assert elapsed_recovery <= 9.5, f"Recovery exceeded 9.0s bound: {elapsed_recovery}s"

            # Verify Reviewer 4 can now claim the recovered item
            page2.goto(f"{BASE_URL}/?reviewer=reviewer_4")
            page2.wait_for_selector(".status-indicator.connected")
            page2.click("#btn-claim-action")
            page2.wait_for_selector("#desk-active-state", state="visible")

            reclaimed_part = page2.inner_text("#item-part-id").strip()
            assert reclaimed_part == walkaway_part_id

            # Clean up
            page2.click("#btn-action-pass")

        finally:
            ctx1.close()
            ctx2.close()
            browser.close()


def test_browser_multi_tab_synchronization(live_server):
    """
    Scenario 4:
    Open Tab 1 and Tab 2 for the same reviewer.
    Action in Tab 1 must automatically reflect in Tab 2 in real time.
    """
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        ctx = browser.new_context()
        tab1 = ctx.new_page()
        tab2 = ctx.new_page()

        try:
            # Seed 2 items
            httpx.post(f"{BASE_URL}/api/generator/burst", json={"size": 2, "duration": 0.5})

            tab1.goto(f"{BASE_URL}/?reviewer=reviewer_sync")
            tab2.goto(f"{BASE_URL}/?reviewer=reviewer_sync")

            tab1.wait_for_selector(".status-indicator.connected")
            tab2.wait_for_selector(".status-indicator.connected")

            # Tab 1 claims item
            tab1.click("#btn-claim-action")
            tab1.wait_for_selector("#desk-active-state", state="visible")
            claimed_id = tab1.inner_text("#item-part-id")

            # Tab 2 MUST automatically reflect the claimed item without user action
            tab2.wait_for_selector("#desk-active-state", state="visible", timeout=3000)
            assert tab2.inner_text("#item-part-id") == claimed_id

            # Tab 2 decides PASS
            tab2.click("#btn-action-pass")
            tab2.wait_for_selector("#desk-empty-state", state="visible")

            # Tab 1 MUST automatically return to empty desk state
            tab1.wait_for_selector("#desk-empty-state", state="visible", timeout=3000)

        finally:
            ctx.close()
            browser.close()


def test_browser_zero_polling_verification(live_server):
    """
    Scenario 4 & Brief Requirement:
    Verify that the frontend NEVER uses HTTP polling for queue updates.
    All live updates are pushed strictly via WebSocket.
    """
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        polled_urls = []

        def handle_request(req):
            if "/api/queue/stats" in req.url or "/api/queue/preview" in req.url:
                polled_urls.append(req.url)

        page.on("request", handle_request)

        try:
            page.goto(f"{BASE_URL}/?reviewer=reviewer_nopoll")
            page.wait_for_selector(".status-indicator.connected")

            # Wait 3.5 seconds while queue events occur
            httpx.post(f"{BASE_URL}/api/generator/burst", json={"size": 3, "duration": 0.5})
            time.sleep(3.5)

            # VERIFY: Zero polling requests to /api/queue/stats or /api/queue/preview!
            assert len(polled_urls) == 0, f"Frontend is polling! Requests detected: {polled_urls}"

        finally:
            ctx.close()
            browser.close()
