/**
 * Real-Time Inspection Review Queue - Frontend Application
 *
 * Capabilities:
 * - Persistent bidirectional WebSocket connection (/ws/reviewer/{id})
 * - Zero-polling push updates for backlog depth, arrival rate, and next item
 * - Sub-second visual lease countdown with 2.5s automated heartbeats
 * - Walkaway simulation toggle for demonstrating <9s sweeper reclamation
 * - Single workstation mode and 6-Reviewer concurrent grid mode
 * - Keyboard shortcuts: Space (Claim), P (Pass), F (Fail), R (Release)
 */

(function() {
  'use strict';

  // --- Configuration & State ---
  const CONFIG = {
    LEASE_DURATION: 8.0,
    HEARTBEAT_INTERVAL_MS: 2500,
    RECONNECT_DELAY_MS: 2000,
  };

  const state = {
    reviewerId: 'reviewer_1',
    currentView: 'single', // 'single' or 'grid'
    ws: null,
    connected: false,
    lastSeenSeq: 0,
    activeItem: null,
    leaseRemaining: 8.0,
    leaseTimer: null,
    heartbeatTimer: null,
    walkawayPaused: false,
    stats: {
      pending: 0,
      claimed: 0,
      passed: 0,
      failed: 0,
      total: 0,
      rate: 0.0,
    },
    // Grid mode states for 6 reviewers
    gridReviewers: {
      reviewer_1: { name: 'Reviewer 1 (Alpha)', activeItem: null, leaseRemaining: 0, timer: null },
      reviewer_2: { name: 'Reviewer 2 (Bravo)', activeItem: null, leaseRemaining: 0, timer: null },
      reviewer_3: { name: 'Reviewer 3 (Charlie)', activeItem: null, leaseRemaining: 0, timer: null },
      reviewer_4: { name: 'Reviewer 4 (Delta)', activeItem: null, leaseRemaining: 0, timer: null },
      reviewer_5: { name: 'Reviewer 5 (Echo)', activeItem: null, leaseRemaining: 0, timer: null },
      reviewer_6: { name: 'Reviewer 6 (Foxtrot)', activeItem: null, leaseRemaining: 0, timer: null },
    }
  };

  // --- DOM Elements ---
  const el = {
    connectionStatus: document.getElementById('connection-status'),
    seqCounter: document.getElementById('seq-counter'),
    reviewerSelect: document.getElementById('reviewer-select'),
    activeHeading: document.getElementById('active-reviewer-heading'),
    btnViewSingle: document.getElementById('btn-view-single'),
    btnViewGrid: document.getElementById('btn-view-grid'),
    viewSingle: document.getElementById('view-single'),
    viewGrid: document.getElementById('view-grid'),
    reviewerContainer: document.getElementById('reviewer-selector-container'),

    // Stats
    statPending: document.getElementById('stat-pending'),
    statClaimed: document.getElementById('stat-claimed'),
    statPassed: document.getElementById('stat-passed'),
    statFailed: document.getElementById('stat-failed'),
    statRate: document.getElementById('stat-rate'),
    statActiveNames: document.getElementById('stat-active-names'),
    badgeCrit: document.getElementById('badge-crit'),
    badgeHigh: document.getElementById('badge-high'),
    badgeRout: document.getElementById('badge-rout'),

    // Controls
    btnBurst: document.getElementById('btn-burst'),
    btnSteady: document.getElementById('btn-steady'),
    btnSteadyText: document.getElementById('btn-steady-text'),
    btnSeed: document.getElementById('btn-seed'),

    // Desk
    deskEmptyState: document.getElementById('desk-empty-state'),
    deskActiveState: document.getElementById('desk-active-state'),
    btnClaimAction: document.getElementById('btn-claim-action'),
    leaseBox: document.getElementById('lease-box'),
    leaseSeconds: document.getElementById('lease-seconds'),
    leaseBarFill: document.getElementById('lease-bar-fill'),

    // Active Item fields
    itemPartId: document.getElementById('item-part-id'),
    itemPriorityBadge: document.getElementById('item-priority-badge'),
    itemComponent: document.getElementById('item-component'),
    itemDefectType: document.getElementById('item-defect-type'),
    itemConfidenceText: document.getElementById('item-confidence-text'),
    itemConfidenceFill: document.getElementById('item-confidence-fill'),
    itemCamera: document.getElementById('item-camera'),
    itemDimensions: document.getElementById('item-dimensions'),
    itemArrivalTime: document.getElementById('item-arrival-time'),
    itemUuid: document.getElementById('item-uuid'),
    inspectionNotes: document.getElementById('inspection-notes'),

    // Actions
    btnPass: document.getElementById('btn-action-pass'),
    btnFail: document.getElementById('btn-action-fail'),
    btnRelease: document.getElementById('btn-action-release'),
    btnWalkaway: document.getElementById('btn-toggle-walkaway'),

    // Sidebar
    nextPreviewBox: document.getElementById('next-preview-box'),
    rosterList: document.getElementById('roster-list'),
    activityLog: document.getElementById('activity-log'),

    // Grid View
    reviewersGrid: document.getElementById('reviewers-grid-container'),
    btnClaimAll: document.getElementById('btn-claim-all'),
  };

  // --- WebSocket Setup ---
  function getWsUrl(reviewerId) {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${window.location.host}/ws/reviewer/${reviewerId}`;
  }

  function initWebSocket() {
    if (state.ws) {
      state.ws.close();
    }

    const wsUrl = getWsUrl(state.reviewerId);
    logActivity('system', `Connecting to WebSocket: ${wsUrl}...`);
    state.ws = new WebSocket(wsUrl);

    state.ws.onopen = () => {
      state.connected = true;
      updateConnectionUI(true);
      logActivity('system', `WebSocket connected for ${state.reviewerId}.`);
    };

    state.ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        handleServerMessage(msg);
      } catch (err) {
        console.error('Error parsing WebSocket frame:', err);
      }
    };

    state.ws.onclose = () => {
      state.connected = false;
      updateConnectionUI(false);
      logActivity('system', 'WebSocket disconnected. Reconnecting in 2s...');
      setTimeout(initWebSocket, CONFIG.RECONNECT_DELAY_MS);
    };

    state.ws.onerror = (err) => {
      console.warn('WebSocket error:', err);
    };
  }

  function sendWs(payload) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(payload));
    }
  }

  function updateConnectionUI(connected) {
    if (connected) {
      el.connectionStatus.innerHTML = `
        <span class="status-indicator connected"></span>
        <span class="status-label">WEBSOCKET ONLINE</span>
        <span class="seq-badge" id="seq-counter">SEQ #${state.lastSeenSeq}</span>
      `;
    } else {
      el.connectionStatus.innerHTML = `
        <span class="status-indicator"></span>
        <span class="status-label">RECONNECTING...</span>
      `;
    }
  }

  // --- Message Handling ---
  function handleServerMessage(msg) {
    // Monotonic sequence verification for stale broadcast rejection
    if (msg.seq !== undefined) {
      if (msg.seq < state.lastSeenSeq) {
        console.warn(`Discarding stale broadcast seq ${msg.seq} (current: ${state.lastSeenSeq})`);
        return;
      }
      state.lastSeenSeq = msg.seq;
      const seqEl = document.getElementById('seq-counter');
      if (seqEl) seqEl.textContent = `SEQ #${msg.seq}`;
    }

    switch (msg.type) {
      case 'INITIAL_STATE':
        logActivity('system', `Hydrated initial state. Backlog: ${msg.stats?.pending_count || 0}`);
        if (msg.stats) updateStats(msg.stats);
        if (msg.active_item) {
          setActiveItem(msg.active_item);
        } else {
          clearActiveItem();
        }
        break;

      case 'QUEUE_UPDATE':
        if (msg.stats) updateStats(msg.stats);
        handleQueueUpdateEvent(msg);
        break;

      case 'CLAIM_RESULT':
        if (msg.item) {
          setActiveItem(msg.item);
          const partLabel = msg.item.payload?.part_number || msg.item.payload?.part_id || msg.item.id;
          logActivity('claim', `${state.reviewerId} claimed ${partLabel} (P${msg.item.priority})`);
        } else {
          logActivity('system', 'Queue empty. No pending items available.');
        }
        break;

      case 'DECISION_ACK':
        const decPart = msg.item?.payload?.part_number || msg.item?.payload?.part_id || msg.item?.id;
        logActivity(
          msg.decision === 'PASS' ? 'pass' : 'fail',
          `Item ${decPart} recorded as ${msg.decision}`
        );
        clearActiveItem();
        break;

      case 'RELEASE_ACK':
        const relPart = msg.item?.payload?.part_number || msg.item?.payload?.part_id || msg.item?.id;
        logActivity('system', `Item ${relPart} returned to PENDING queue.`);
        clearActiveItem();
        break;

      case 'HEARTBEAT_ACK':
        if (state.activeItem && msg.remaining_lease_seconds !== undefined) {
          state.leaseRemaining = parseFloat(msg.remaining_lease_seconds);
          updateLeaseBarUI();
        }
        break;

      case 'LEASE_LOST':
        logActivity('recovery', `ALERT: Lease expired for ${msg.item_id}! Sweeper recovered item back to queue.`);
        clearActiveItem();
        alertLeaseLost();
        break;

      case 'ERROR':
        logActivity('system', `Server error: ${msg.message}`);
        break;

      default:
        console.debug('Unhandled message type:', msg.type);
    }
  }

  function handleQueueUpdateEvent(msg) {
    const trigger = msg.trigger;
    const data = msg.event_data || {};

    if (trigger === 'ITEM_ARRIVAL') {
      logActivity('arrival', `+${msg.arrival_count} new inspection items arrived on line.`);
    } else if (trigger === 'CLAIM') {
      const rev = data.reviewer_id;
      const part = data.item?.payload?.part_number || data.item?.payload?.part_id || data.item?.id;
      if (rev !== state.reviewerId) {
        logActivity('claim', `${rev} claimed ${part} (P${data.item?.priority})`);
      }
    } else if (trigger === 'DECISION') {
      const rev = data.reviewer_id;
      const dec = data.decision;
      const part = data.item?.payload?.part_number || data.item?.payload?.part_id || data.item?.id;
      if (rev !== state.reviewerId) {
        logActivity(dec === 'PASS' ? 'pass' : 'fail', `${rev} judged ${part} as ${dec}`);
      }
    } else if (trigger === 'RELEASE') {
      const rev = data.reviewer_id;
      const part = data.item?.payload?.part_number || data.item?.payload?.part_id || data.item?.id;
      if (rev !== state.reviewerId) {
        logActivity('system', `${rev} released ${part} back to queue`);
      }
    } else if (trigger === 'LEASE_RECOVERED') {
      const count = data.count || 0;
      const recovered = data.recovered_items || [];
      const lostItem = recovered.find(r => r.claimed_by === state.reviewerId);
      if (lostItem) {
        logActivity('recovery', `ALERT: Lease expired for ${lostItem.id}! Sweeper recovered item back to queue.`);
        clearActiveItem();
        alertLeaseLost();
      } else {
        logActivity('recovery', `Sweeper reclaimed ${count} walked-away items (<9s bound enforced).`);
      }
    }

    // Update Next Up Preview
    if (msg.stats?.next_preview) {
      updateNextPreviewUI(msg.stats.next_preview);
    } else {
      updateNextPreviewUI(null);
    }
  }

  // --- Telemetry & Stats UI ---
  function updateStats(s) {
    el.statPending.textContent = s.pending_count ?? 0;
    el.statClaimed.textContent = s.claimed_count ?? 0;
    el.statPassed.textContent = s.passed_count ?? 0;
    el.statFailed.textContent = s.failed_count ?? 0;

    // Active reviewers list
    const activeRevs = s.active_reviewers || [];
    if (activeRevs.length === 0) {
      el.statActiveNames.textContent = 'No active reviewers';
    } else {
      const names = activeRevs.map(r => r.claimed_by.replace('reviewer_', 'R')).join(', ');
      el.statActiveNames.textContent = `Active: ${names}`;
    }

    // Next Up Preview
    updateNextPreviewUI(s.next_preview);

    // Update Roster Card
    updateRosterUI(activeRevs);

    // Update Grid if visible
    if (state.currentView === 'grid') {
      refreshGridStates();
    }
  }

  function updateNextPreviewUI(preview) {
    if (!preview) {
      el.nextPreviewBox.innerHTML = '<div class="preview-empty">No pending items in queue.</div>';
      return;
    }

    const payload = preview.payload || {};
    const prioClass = preview.priority >= 9 ? 'priority-crit' : (preview.priority >= 6 ? 'priority-high' : 'priority-rout');
    const prioLabel = preview.priority >= 9 ? 'CRITICAL' : (preview.priority >= 6 ? 'HIGH' : 'ROUTINE');

    el.nextPreviewBox.innerHTML = `
      <div class="preview-item-box">
        <div class="preview-top">
          <span class="preview-part">${payload.part_number || payload.part_id || preview.id}</span>
          <span class="priority-badge ${prioClass}">P${preview.priority} // ${prioLabel}</span>
        </div>
        <div class="preview-defect">${payload.defect_type || 'Inspection Item'}</div>
        <div class="preview-comp">${payload.component || 'Standard Component'} &bull; Camera: ${payload.camera_id || 'CAM-01'}</div>
      </div>
    `;
  }

  function updateRosterUI(activeRevs) {
    const activeMap = {};
    activeRevs.forEach(r => { activeMap[r.claimed_by] = r.items_held; });

    const reviewers = [
      { id: 'reviewer_1', label: 'Reviewer 1 (Alpha)' },
      { id: 'reviewer_2', label: 'Reviewer 2 (Bravo)' },
      { id: 'reviewer_3', label: 'Reviewer 3 (Charlie)' },
      { id: 'reviewer_4', label: 'Reviewer 4 (Delta)' },
      { id: 'reviewer_5', label: 'Reviewer 5 (Echo)' },
      { id: 'reviewer_6', label: 'Reviewer 6 (Foxtrot)' },
    ];

    el.rosterList.innerHTML = reviewers.map(r => {
      const isReviewing = !!activeMap[r.id];
      const isCurrent = r.id === state.reviewerId;
      return `
        <div class="roster-item ${isCurrent ? 'current-roster' : ''}">
          <span class="roster-id">${r.label} ${isCurrent ? '★' : ''}</span>
          <span class="roster-status ${isReviewing ? 'status-reviewing' : 'status-idle'}">
            ${isReviewing ? 'REVIEWING' : 'IDLE'}
          </span>
        </div>
      `;
    }).join('');
  }

  // --- Active Item Desk & Lease Countdown ---
  function setActiveItem(item) {
    state.activeItem = item;
    state.walkawayPaused = false;
    el.btnWalkaway.classList.remove('paused');
    el.btnWalkaway.textContent = '⏸ Simulate Walkaway (Pause Heartbeat)';

    const payload = item.payload || {};
    el.itemPartId.textContent = payload.part_number || payload.part_id || item.id;
    el.itemComponent.textContent = payload.component || 'Aircraft Turbine Component';
    el.itemDefectType.textContent = payload.defect_type || 'Visual Anomaly';
    el.itemCamera.textContent = payload.camera_id || 'CAM-ALPHA-01';

    const dims = payload.bounding_dimensions_mm;
    el.itemDimensions.textContent = dims ? `${dims.length} x ${dims.width} x ${dims.depth} mm` : '12.0 x 4.0 x 1.0 mm';
    el.itemArrivalTime.textContent = item.arrival_time ? new Date(item.arrival_time).toLocaleTimeString() : 'Just now';
    el.itemUuid.textContent = item.id.substring(0, 16) + '...';

    // Confidence
    const conf = payload.confidence ? Math.round(payload.confidence * 100) : 95;
    el.itemConfidenceText.textContent = `${conf}%`;
    el.itemConfidenceFill.style.width = `${conf}%`;

    // Priority badge
    const p = item.priority;
    const prioClass = p >= 9 ? 'priority-crit' : (p >= 6 ? 'priority-high' : 'priority-rout');
    const prioLabel = p >= 9 ? 'CRITICAL' : (p >= 6 ? 'HIGH' : 'ROUTINE');
    el.itemPriorityBadge.className = `priority-badge ${prioClass}`;
    el.itemPriorityBadge.textContent = `${p} / ${prioLabel}`;

    // Show active state
    el.deskEmptyState.style.display = 'none';
    el.deskActiveState.style.display = 'block';
    el.leaseBox.style.display = 'flex';
    el.inspectionNotes.value = '';

    // Initialize remaining lease time
    state.leaseRemaining = item.remaining_lease_seconds !== undefined
      ? Math.max(0, parseFloat(item.remaining_lease_seconds))
      : CONFIG.LEASE_DURATION;

    startLeaseTimers();
  }

  function clearActiveItem() {
    state.activeItem = null;
    stopLeaseTimers();
    el.deskEmptyState.style.display = 'flex';
    el.deskActiveState.style.display = 'none';
    el.leaseBox.style.display = 'none';
  }

  function startLeaseTimers() {
    stopLeaseTimers();

    // Visual countdown timer (runs every 100ms)
    const tickInterval = 100;
    state.leaseTimer = setInterval(() => {
      state.leaseRemaining = Math.max(0, state.leaseRemaining - (tickInterval / 1000));
      updateLeaseBarUI();

      if (state.leaseRemaining <= 0) {
        clearInterval(state.leaseTimer);
        state.leaseTimer = null;
      }
    }, tickInterval);

    // Two-way automated heartbeat timer (runs every 2.5s)
    state.heartbeatTimer = setInterval(() => {
      if (state.activeItem && !state.walkawayPaused) {
        sendWs({
          type: 'HEARTBEAT',
          item_id: state.activeItem.id
        });
      }
    }, CONFIG.HEARTBEAT_INTERVAL_MS);
  }

  function stopLeaseTimers() {
    if (state.leaseTimer) {
      clearInterval(state.leaseTimer);
      state.leaseTimer = null;
    }
    if (state.heartbeatTimer) {
      clearInterval(state.heartbeatTimer);
      state.heartbeatTimer = null;
    }
  }

  function updateLeaseBarUI() {
    el.leaseSeconds.textContent = `${state.leaseRemaining.toFixed(1)}s`;
    const pct = Math.max(0, Math.min(100, (state.leaseRemaining / CONFIG.LEASE_DURATION) * 100));
    el.leaseBarFill.style.width = `${pct}%`;

    // Color transition based on urgency
    if (state.leaseRemaining > 4.5) {
      el.leaseBarFill.style.backgroundColor = 'var(--color-pass)';
      el.leaseSeconds.style.color = 'var(--color-pass)';
    } else if (state.leaseRemaining > 2.0) {
      el.leaseBarFill.style.backgroundColor = 'var(--color-high)';
      el.leaseSeconds.style.color = 'var(--color-high)';
    } else {
      el.leaseBarFill.style.backgroundColor = 'var(--color-crit)';
      el.leaseSeconds.style.color = 'var(--color-crit)';
    }
  }

  function alertLeaseLost() {
    el.deskActiveState.style.border = '2px solid var(--color-crit)';
    setTimeout(() => {
      if (el.deskActiveState) el.deskActiveState.style.border = '';
    }, 1500);
  }

  // --- Inspection Actions ---
  function claimItem() {
    if (!state.connected) {
      logActivity('system', 'Cannot claim: WebSocket disconnected.');
      return;
    }
    sendWs({ type: 'CLAIM' });
  }

  function decideItem(decision) {
    if (!state.activeItem) return;
    const notes = el.inspectionNotes.value.trim() || undefined;
    sendWs({
      type: 'DECIDE',
      item_id: state.activeItem.id,
      decision: decision,
      notes: notes
    });
  }

  function releaseItem() {
    if (!state.activeItem) return;
    sendWs({
      type: 'RELEASE',
      item_id: state.activeItem.id,
      reason: 'manual_voluntary_release'
    });
  }

  // --- Generator & Control Actions ---
  async function triggerBurst() {
    el.btnBurst.disabled = true;
    el.btnBurst.innerHTML = '<span class="btn-icon">⏳</span> Ingesting 250 Items...';
    try {
      const res = await fetch('/api/generator/burst', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ size: 250, duration: 8.0 })
      });
      const data = await res.json();
      logActivity('arrival', `Burst complete: 250 items dumped in ${data.metrics?.duration_seconds?.toFixed(2) || '8.0'}s`);
    } catch (err) {
      logActivity('system', `Error triggering burst: ${err.message}`);
    } finally {
      el.btnBurst.disabled = false;
      el.btnBurst.innerHTML = '<span class="btn-icon">💥</span> Burst 250 Items';
    }
  }

  async function toggleSteadyFlow() {
    const isRunning = el.btnSteady.classList.contains('running');
    const endpoint = isRunning ? '/api/generator/stop' : '/api/generator/start';
    try {
      const res = await fetch(endpoint, { method: 'POST' });
      const data = await res.json();
      if (!isRunning) {
        el.btnSteady.classList.add('running');
        el.btnSteadyText.textContent = 'Stop Flow (5/s)';
        logActivity('system', 'Steady ingestion started (5 items/sec).');
      } else {
        el.btnSteady.classList.remove('running');
        el.btnSteadyText.textContent = 'Start Flow (5/s)';
        logActivity('system', 'Steady ingestion paused.');
      }
    } catch (err) {
      logActivity('system', `Generator error: ${err.message}`);
    }
  }

  async function seedTenItems() {
    try {
      await fetch('/api/generator/burst', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ size: 10, duration: 0.5 })
      });
      logActivity('arrival', 'Seeded 10 items into backlog.');
    } catch (err) {
      console.error(err);
    }
  }

  // --- 6-Reviewer Grid Mode ("God Mode") ---
  function initGridView() {
    const container = el.reviewersGrid;
    container.innerHTML = '';

    const reviewers = Object.keys(state.gridReviewers);
    reviewers.forEach(id => {
      const r = state.gridReviewers[id];
      const card = document.createElement('div');
      card.className = 'reviewer-card';
      card.id = `grid-card-${id}`;
      card.innerHTML = `
        <div class="reviewer-card-header">
          <span>${r.name}</span>
          <span class="status-badge" id="grid-status-${id}">IDLE</span>
        </div>
        <div class="reviewer-card-body" id="grid-body-${id}">
          <div class="grid-desk-idle">
            <span>No active item claimed</span>
            <button class="btn-grid-claim" onclick="window.gridClaim('${id}')">⚡ Claim Item</button>
          </div>
        </div>
      `;
      container.appendChild(card);
    });

    refreshGridStates();
  }

  async function refreshGridStates() {
    // Fetch active items for all 6 reviewers via REST
    for (const id of Object.keys(state.gridReviewers)) {
      try {
        const res = await fetch(`/api/queue/active/${id}`);
        const data = await res.json();
        updateGridReviewerCard(id, data.active_item);
      } catch (e) {
        // quiet ignore
      }
    }
  }

  function updateGridReviewerCard(id, item) {
    const body = document.getElementById(`grid-body-${id}`);
    const status = document.getElementById(`grid-status-${id}`);
    if (!body || !status) return;

    if (!item) {
      status.textContent = 'IDLE';
      status.style.color = 'var(--text-dim)';
      body.innerHTML = `
        <div class="grid-desk-idle">
          <span>No active item</span>
          <button class="btn-grid-claim" onclick="window.gridClaim('${id}')">⚡ Claim Item</button>
        </div>
      `;
    } else {
      status.textContent = 'REVIEWING';
      status.style.color = 'var(--accent-cyan)';
      const p = item.priority;
      const prioClass = p >= 9 ? 'priority-crit' : (p >= 6 ? 'priority-high' : 'priority-rout');
      const payload = item.payload || {};
      const partDisplay = payload.part_number || payload.part_id || item.id;
      const compDisplay = payload.component ? `<span style="font-size: 11px; color: var(--text-muted); font-weight: 500;"> • ${payload.component}</span>` : '';

      body.innerHTML = `
        <div class="grid-desk-active">
          <div class="grid-part-row">
            <span class="part-id">${partDisplay}${compDisplay}</span>
            <span class="priority-badge ${prioClass}">P${p}</span>
          </div>
          <div class="grid-defect-text">${payload.defect_type || 'Defect'}</div>
          <div class="grid-lease-row">
            <span>Lease:</span>
            <div class="grid-lease-bar">
              <div class="grid-lease-bar-fill" style="width: 100%;"></div>
            </div>
          </div>
          <div class="grid-actions-row">
            <button class="btn-grid-action btn-pass" onclick="window.gridJudge('${id}', '${item.id}', 'PASS')">PASS</button>
            <button class="btn-grid-action btn-fail" onclick="window.gridJudge('${id}', '${item.id}', 'FAIL')">FAIL</button>
            <button class="btn-grid-action btn-release" onclick="window.gridRelease('${id}', '${item.id}')">REL</button>
          </div>
        </div>
      `;
    }
  }

  // Window exposed helpers for grid interaction
  window.gridClaim = async function(reviewerId) {
    try {
      const res = await fetch('/api/queue/claim', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reviewer_id: reviewerId })
      });
      const data = await res.json();
      updateGridReviewerCard(reviewerId, data.item);
    } catch (e) {
      console.error(e);
    }
  };

  window.gridJudge = async function(reviewerId, itemId, decision) {
    try {
      await fetch('/api/queue/judge', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reviewer_id: reviewerId, item_id: itemId, decision: decision })
      });
      updateGridReviewerCard(reviewerId, null);
    } catch (e) {
      console.error(e);
    }
  };

  window.gridRelease = async function(reviewerId, itemId) {
    try {
      await fetch('/api/queue/release', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reviewer_id: reviewerId, item_id: itemId })
      });
      updateGridReviewerCard(reviewerId, null);
    } catch (e) {
      console.error(e);
    }
  };

  async function claimAllSixReviewers() {
    logActivity('system', 'Sending 6 simultaneous claim requests across all reviewers...');
    const reviewers = ['reviewer_1', 'reviewer_2', 'reviewer_3', 'reviewer_4', 'reviewer_5', 'reviewer_6'];
    await Promise.all(reviewers.map(r => window.gridClaim(r)));
  }

  // --- Activity Logging ---
  function logActivity(type, message) {
    if (!el.activityLog) return;
    const now = new Date();
    const timeStr = now.toTimeString().split(' ')[0];

    const entry = document.createElement('div');
    entry.className = `log-entry log-${type}`;
    entry.innerHTML = `<span class="log-time">[${timeStr}]</span> ${message}`;

    el.activityLog.appendChild(entry);
    el.activityLog.scrollTop = el.activityLog.scrollHeight;

    // Prune old log entries if > 100
    while (el.activityLog.children.length > 100) {
      el.activityLog.removeChild(el.activityLog.firstChild);
    }
  }

  // --- Keyboard Shortcuts & Event Listeners ---
  function attachEventListeners() {
    // Workstation selector dropdown
    el.reviewerSelect.addEventListener('change', (e) => {
      state.reviewerId = e.target.value;
      el.activeHeading.textContent = e.target.options[e.target.selectedIndex].text;
      initWebSocket();
    });

    // View toggles
    el.btnViewSingle.addEventListener('click', () => switchView('single'));
    el.btnViewGrid.addEventListener('click', () => switchView('grid'));

    // Desk action buttons
    el.btnClaimAction.addEventListener('click', claimItem);
    el.btnPass.addEventListener('click', () => decideItem('PASS'));
    el.btnFail.addEventListener('click', () => decideItem('FAIL'));
    el.btnRelease.addEventListener('click', releaseItem);

    // Walkaway toggle
    el.btnWalkaway.addEventListener('click', () => {
      state.walkawayPaused = !state.walkawayPaused;
      if (state.walkawayPaused) {
        el.btnWalkaway.classList.add('paused');
        el.btnWalkaway.textContent = '▶ Resume Heartbeats';
        logActivity('system', 'Heartbeats paused. Watch the 8s countdown expire for sweeper recovery demo.');
      } else {
        el.btnWalkaway.classList.remove('paused');
        el.btnWalkaway.textContent = '⏸ Simulate Walkaway (Pause Heartbeat)';
        logActivity('system', 'Heartbeats resumed.');
      }
    });

    // Generator controls
    el.btnBurst.addEventListener('click', triggerBurst);
    el.btnSteady.addEventListener('click', toggleSteadyFlow);
    el.btnSeed.addEventListener('click', seedTenItems);
    el.btnClaimAll.addEventListener('click', claimAllSixReviewers);

    // Global keyboard shortcuts
    window.addEventListener('keydown', (e) => {
      // Ignore if typing in notes input
      if (document.activeElement === el.inspectionNotes) return;

      if (e.code === 'Space') {
        e.preventDefault();
        if (!state.activeItem) claimItem();
      } else if (e.key === 'p' || e.key === 'P') {
        if (state.activeItem) decideItem('PASS');
      } else if (e.key === 'f' || e.key === 'F') {
        if (state.activeItem) decideItem('FAIL');
      } else if (e.key === 'r' || e.key === 'R') {
        if (state.activeItem) releaseItem();
      }
    });
  }

  function switchView(view) {
    state.currentView = view;
    if (view === 'single') {
      el.btnViewSingle.classList.add('active');
      el.btnViewGrid.classList.remove('active');
      el.viewSingle.classList.add('active');
      el.viewGrid.classList.remove('active');
      el.reviewerContainer.style.display = 'flex';
    } else {
      el.btnViewGrid.classList.add('active');
      el.btnViewSingle.classList.remove('active');
      el.viewGrid.classList.add('active');
      el.viewSingle.classList.remove('active');
      el.reviewerContainer.style.display = 'none';
      initGridView();
    }
  }

  // --- Initialization ---
  function init() {
    // Check URL parameters for ?reviewer=reviewer_X
    const urlParams = new URLSearchParams(window.location.search);
    const revParam = urlParams.get('reviewer');
    if (revParam) {
      state.reviewerId = revParam;
      if (!el.reviewerSelect.querySelector(`option[value="${revParam}"]`)) {
        const opt = document.createElement('option');
        opt.value = revParam;
        opt.textContent = revParam;
        el.reviewerSelect.appendChild(opt);
      }
      el.reviewerSelect.value = revParam;
    }

    const selectedOption = el.reviewerSelect.options[el.reviewerSelect.selectedIndex];
    el.activeHeading.textContent = selectedOption ? selectedOption.text : 'Reviewer 1 (Alpha)';

    attachEventListeners();
    initWebSocket();
  }

  // Boot on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
