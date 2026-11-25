# Engineering Thought Process & Creation Log
## Real-Time Visual Inspection Review Queue

This transcript highlights the candidate's architectural vision, critical reasoning, technical decisions, and proactive AI oversight throughout the end-to-end design and implementation of the system.

---

## 1. Architectural Strategy & Problem Formulation

### 💡 User's Strategic Direction
> *"We need a real-time review queue for industrial visual inspection. It must handle steady ingestion (~5 items/s) and severe bursts (250 items in <10s) across six concurrent reviewers. Most importantly: zero duplicate claims, sub-9-second walkaway recovery, and zero frontend polling."*

### 🧠 Thought Process & Technology Evaluation
* **PostgreSQL 16 vs. Redis vs. Dedicated Message Brokers (Kafka / RabbitMQ):**
  * **Evaluation:** At 30–50 items/sec peak and 6 reviewers, Redis's in-memory microsecond speed is overkill and adds an unnecessary secondary stateful service. Redis requires complex custom Lua scripts for atomic lease assignment, lacks relational audit history, and requires tricky AOF/RDB persistence configuration.
  * **Decision:** Selected **PostgreSQL 16 with `FOR UPDATE SKIP LOCKED`**. It natively unifies ACID persistence, dynamic priority ordering (`ORDER BY priority DESC, arrival_time ASC`), row-level non-blocking worker claiming, and complete audit logging in a single robust engine.
* **Lease-Based State vs. Held Database Transactions:**
  * **Evaluation:** Holding an open database transaction while a human reviewer inspects a part would lock up database connection pools and cause connection timeout cascades.
  * **Decision:** Executed claims as instantaneous sub-millisecond atomic `UPDATE`s backed by timestamp leases (`lease_expires_at = NOW() + INTERVAL '8 seconds'`), paired with background sweeper reclamation and client heartbeats.

---

## 2. Handout Atomicity & Concurrency Model (Phase 3)

### 💡 User's Core Challenge
> *"How do we guarantee an item is handed to exactly one reviewer under heavy burst contention, especially when multiple screens request the top item at the exact same millisecond?"*

### 🧠 Thought Process & Implementation Guidance
* **Where Atomicity Lives:**
  Rejected two-step `SELECT` followed by `UPDATE` due to Time-of-Check to Time-of-Use (TOCTOU) race conditions.
* **The Single-Statement CTE Solution:**
  Engineered the claim operation into a single atomic Common Table Expression:
  ```sql
  WITH next_item AS (
      SELECT id FROM inspection_items
      WHERE status = 'PENDING'
      ORDER BY priority DESC, arrival_time ASC
      LIMIT 1
      FOR UPDATE SKIP LOCKED
  ),
  updated_item AS (
      UPDATE inspection_items
      SET status = 'CLAIMED', claimed_by = $1, claimed_at = NOW(),
          lease_expires_at = NOW() + INTERVAL '8 seconds'
      FROM next_item
      WHERE inspection_items.id = next_item.id
      RETURNING inspection_items.*
  )
  INSERT INTO audit_events (item_id, event_type, reviewer_id, metadata)
  SELECT id, 'CLAIMED', $1, json_build_object('priority', priority) FROM updated_item;
  ```
* **PostgreSQL Engine Guarantees:**
  * `FOR UPDATE SKIP LOCKED` instructs PostgreSQL's query engine to bypass rows currently locked by other concurrent claims and lock the next qualifying row in priority order.
  * Added a database-level partial unique index:
    ```sql
    CREATE UNIQUE INDEX idx_one_active_claim_per_reviewer
    ON inspection_items (claimed_by) WHERE status = 'CLAIMED';
    ```
    Physically prevents any reviewer from holding more than one active item simultaneously.

---

## 3. Walkaway Recovery & The Lease Pattern (Phase 4)

### 💡 User's Inquiry & Challenge
> *"Why is the lease important, and why does the recovery bound need to be strictly $\le 9.0$ seconds?"*

### 🧠 Thought Process & Engineering Formulation
* **The Industrial Problem:**
  Reviewers in a factory may experience browser crashes, Wi-Fi dropouts, or walk away mid-inspection. Without automated leases, critical defective parts (e.g., P10 Structural Fractures) remain locked in limbo forever, stalling production lines.
* **The Heartbeat vs. Inspection Time Solution:**
  * Base lease set to **8.0 seconds**.
  * Automated client heartbeat pulses every **2.5 seconds** over WebSockets to extend the lease while an inspector is actively working.
  * Background sweeper scans every **1.0 second** for expired claims (`status = 'CLAIMED' AND lease_expires_at < NOW()`).
  * Worst-case recovery time: $8.0\text{s} + 1.0\text{s} = \mathbf{9.0\text{s}}$. Empirically verified at **8.50 seconds** (providing a 0.5s safety margin).
* **Guarding Against Stale / Corrupted Decisions:**
  Submitting a decision checks `WHERE lease_expires_at > NOW() AND claimed_by = $2`. If an inspector submits a late judgment after their lease expired, PostgreSQL rejects it with HTTP 409 Conflict, completely eliminating split-brain overwrites.

---

## 4. End-to-End Browser Testing & Verification (Phase 7)

### 💡 User's Directive
> *"I prefer Option 2 — Playwright with headless Firefox because we need to verify the actual browser experience, not just backend HTTP and WebSocket behavior. Prioritize testing all six reviewer screens simultaneously, burst contention, walkaway recovery, and stale-message handling."*

### 🧠 Thought Process & Execution
* **Why Unit Mocks Were Insufficient:**
  HTTP-level mocks do not test true multi-tab DOM synchronization, WebSocket reconnection, or browser layout rendering under concurrent stress.
* **Browser Test Suite Implemented (`tests/test_phase7_browser_e2e.py`):**
  1. `test_browser_six_reviewers_simultaneous_screens`: 6 parallel Firefox contexts establishing persistent WebSocket channels.
  2. `test_browser_concurrent_claiming_zero_double_assignments`: 6 reviewers clicking claim at the same millisecond; verified 0 double assignments.
  3. `test_browser_walkaway_recovery_and_sweeper_bound`: Simulated walkaway; verified recovery in 8.50s with `LEASE_LOST` push.
  4. `test_browser_multi_tab_synchronization`: Verified state coherence across multiple tabs of the same reviewer.
  5. `test_browser_zero_polling_verification`: Monitored network traffic to prove zero HTTP polling requests occur during live updates.

---

## 5. Critical AI Oversight & Anomaly Investigations

*The following key moments demonstrate proactive verification, critical debugging, and oversight of AI-generated suggestions:*

### 🔍 Case A: Uncovering Hidden HTTP Polling in the Grid
* **User's Observation:**
  > *"I saw when I go to reviewers page there the reviewers are called again and again, is it polling?"*
* **Root Cause Identified:**
  The AI had written a function `refreshGridStates()` that fired 6 HTTP `GET /api/queue/active/{id}` requests whenever a WebSocket queue update arrived. While the queue updates were pushed, the frontend was still performing pseudo-polling!
* **Resolution:**
  Eliminated `refreshGridStates()`. Updated the backend WebSocket broadcast to push `active_claims` directly inside the payload (`stats.active_claims`). The frontend was updated to hydrate purely from the WebSocket message, guaranteeing **100% server push and true zero polling**.

---

### 🔍 Case B: Investigating the Optical "Duplicate Part" Illusion
* **User's Observation:**
  > *"Here why is the same item claimed by both? I didn't get it, is it a flaw?"* (Reviewer 1 and Reviewer 3 both showed `Structural Fracture`).
* **Root Cause Identified:**
  The AI's frontend template displayed items using internal UUID prefixes and defect classifications (`Structural Fracture`). Because multiple manufactured components had the same defect type, Reviewer 1 and Reviewer 3 appeared to hold the same item.
* **Verification & Resolution:**
  Ran a direct PostgreSQL query to confirm that Reviewer 1 held `PART-2736-K` and Reviewer 3 held `PART-5229-W` (two distinct rows). Upgraded the UI to display human-readable Part Numbers and Components, eliminating visual ambiguity.

---

### 🔍 Case C: Detecting the Static Lease Bar on Grid Cards
* **User's Observation:**
  > *"Here the lease should dec or its kept intentionally tell me?"* (Reviewer 6 in the grid displayed a static 100% full green bar).
* **Root Cause Identified:**
  The AI had wired the 100ms dynamic countdown ticker and color transitions to the Single Workstation desk, but left the 6-Reviewer Grid cards with a static `style="width: 100%;"`.
* **Resolution:**
  Implemented `startGridLeaseTicker()` and `updateGridLeaseBar()` for the 6-reviewer overview. Each card now features a real-time countdown (`8.0s` $\to$ `0.0s`), smooth bar animation, and green $\to$ amber $\to$ red urgency color transitions.

---

### 🔍 Case D: Correcting Technical Reporting & Latency Claims
* **User's Correction:**
  > *"Three things I'd correct before submission:
  > 1. Claim latency wording: The table says 'Sub-millisecond queue head', but measured p50 is 3.08 ms. Distinguish the measurements.
  > 2. Strictly bounded latency: A measured p99 is an empirical observation, not proof of a universal hard bound. Say 'observed maximum: 14.77 ms under tested conditions'.
  > 3. Walkaway recovery guarantee: 8.50s against a 9s target is 0.5s margin. Describe it as verified under tested conditions, not an unconditional guarantee."*
* **Action Taken:**
  Audited `DECISIONS.md` and `README.md`. Clearly distinguished PostgreSQL's internal index scan execution time (**0.398 ms**) from the end-to-end HTTP/WebSocket roundtrip latency (**median 3.08 ms**), and adopted precise, scientifically honest empirical language.

---

## 6. Summary of Verified Stress Test Results

* **250-Item Burst Ingestion:** Generated and committed in **8.57 seconds** (29.2 items/sec, safe under 10.0s target).
* **Review Velocity:** 6 concurrent reviewers cleared all 250 items in **8.58 seconds** (29.15 decisions/sec).
* **Contention Claim Latencies:**
  * Median (p50): **3.08 ms**
  * 90th percentile (p90): **3.99 ms**
  * 95th percentile (p95): **4.22 ms**
  * 99th percentile (p99): **4.68 ms**
  * Observed maximum: **14.77 ms** under tested conditions.
* **Data Integrity:** **0 duplicate claims**, **0 dropped items**, exactly **750 audit events** recorded (maintaining a perfect 3:1 invariant).
* **Walkaway Recovery:** Verified at **8.50 seconds** (0.5s margin under 9.0s ceiling).
* **Automated Tests:** **44 automated tests passing** across unit, integration, and browser Playwright suites.
