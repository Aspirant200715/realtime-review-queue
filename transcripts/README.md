# Project Transcripts & Design Logs

This directory contains the complete record of the engineering thought process, architectural brainstorming, and collaboration history for the **Real-Time Visual Inspection Review Queue**.

---

## Directory Contents

| File | Description |
| :--- | :--- |
| **[`BRAINSTORMING.md`](./BRAINSTORMING.md)** | **Core Thought Process & Decision Log**<br>Details the candidate's strategic technical direction, evaluation of alternatives (PostgreSQL vs. Redis vs. Kafka), concurrency design (`SKIP LOCKED`), lease and walkaway recovery formulation, and critical oversight of AI code generation (including anomaly detection and verification). |
| **[`FULL_CHAT_TRANSCRIPT.md`](./FULL_CHAT_TRANSCRIPT.md)** | **Human-Readable Full Transcript (Markdown)**<br>A formatted turn-by-turn markdown record of all 32 conversation turns across all phases (Phase 1 through Phase 8), detailing each prompt, actions executed, and architectural solutions delivered. |
| **[`transcript.jsonl`](./transcript.jsonl)** | **Compact Machine-Readable Transcript**<br>Standard structured JSON Lines transcript log of the entire session. |
| **[`transcript_full.jsonl`](./transcript_full.jsonl)** | **Full Untruncated Session Log**<br>Complete, raw JSON Lines log containing full payloads, inputs, and execution traces. |
