-- Schema for Real-Time Visual Inspection Review Queue

CREATE TABLE IF NOT EXISTS inspection_items (
    id VARCHAR(64) PRIMARY KEY,
    priority INTEGER NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'CLAIMED', 'PASSED', 'FAILED', 'RELEASED')),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    arrival_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_by VARCHAR(64) NULL,
    claimed_at TIMESTAMPTZ NULL,
    lease_expires_at TIMESTAMPTZ NULL,
    decided_at TIMESTAMPTZ NULL,
    decision VARCHAR(20) NULL CHECK (decision IS NULL OR decision IN ('PASS', 'FAIL', 'RELEASE')),
    notes TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Partial index for atomic claim handout with priority ordering
-- Enables sub-millisecond SELECT ... FOR UPDATE SKIP LOCKED seeks
CREATE INDEX IF NOT EXISTS idx_inspection_items_pending 
    ON inspection_items (priority DESC, arrival_time ASC) 
    WHERE status = 'PENDING';

-- Index for background lease recovery sweeper
CREATE INDEX IF NOT EXISTS idx_inspection_items_claimed_lease 
    ON inspection_items (lease_expires_at) 
    WHERE status = 'CLAIMED';

-- Index to quickly look up a reviewer's currently active claimed item
CREATE INDEX IF NOT EXISTS idx_inspection_items_claimed_by 
    ON inspection_items (claimed_by) 
    WHERE status = 'CLAIMED';

-- Audit trail table for proving zero double-assignments, tracking state transitions and latency
CREATE TABLE IF NOT EXISTS audit_events (
    id BIGSERIAL PRIMARY KEY,
    item_id VARCHAR(64) NOT NULL REFERENCES inspection_items(id) ON DELETE CASCADE,
    event_type VARCHAR(30) NOT NULL,
    reviewer_id VARCHAR(64) NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_audit_events_item_id 
    ON audit_events (item_id, created_at ASC);
