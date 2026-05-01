/**
 * station-common.js — Shared station logic (WebSocket, overlays, damage LEDs, dB meter, tasks)
 */
class StationBase {
  constructor(stationName) {
    this.stationName = stationName;
    this.ws = null;
    this.receivedTelemetry = false;
    this.existingTaskElements = new Map();
    this.peakHold = 0;
    this.audio = null;
    this._knownMissionVersion = null;
    this._reconnectDelay = 1000;
    this._commandQueue = [];
  }

  /** Create WebSocket and wire up lifecycle handlers. */
  connect() {
    const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    this.ws = new WebSocket(proto + location.host + '/ws/' + this.stationName);

    const wsEl = document.getElementById('wsStatus') || document.getElementById('wsStatusTitle');
    if (wsEl) {
      this.ws.onopen = () => {
        wsEl.textContent = 'WebSocket: Connected';
        wsEl.style.color = '#8DEB8D';
        wsEl.style.borderColor = '#8DEB8D';
        this._reconnectDelay = 1000; // reset backoff on successful connect
        // Flush queued commands
        while (this._commandQueue.length > 0) {
          const cmd = this._commandQueue.shift();
          this.ws.send(JSON.stringify(cmd));
        }
      };
      this.ws.onerror = () => {
        wsEl.textContent = 'WebSocket: Error';
        wsEl.style.color = '#FF6B6B';
        wsEl.style.borderColor = '#FF6B6B';
      };
    }

    // Auto-reconnect with exponential backoff
    this.ws.onclose = () => {
      if (wsEl) {
        wsEl.textContent = 'WebSocket: Disconnected';
        wsEl.style.color = '#FF6B6B';
        wsEl.style.borderColor = '#FF6B6B';
      }
      // Reset latched state so it re-syncs on reconnect
      this._knownMissionVersion = null;
      this.receivedTelemetry = false;
      const delay = this._reconnectDelay;
      this._reconnectDelay = Math.min(delay * 2, 10000);
      setTimeout(() => this.connect(), delay);
    };

    this.ws.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data);
        if (m.topic === 'status') {
          this.handleStatusMessage(m.data);
          return;
        }
        if (m.topic === 'telemetry') {
          // Mission version change detection
          const ver = m.data.missionVersion;
          if (ver !== undefined) {
            if (this._knownMissionVersion === null) {
              this._knownMissionVersion = ver;
            } else if (ver !== this._knownMissionVersion) {
              // Mission changed - gracefully transition instead of requiring page refresh
              this._knownMissionVersion = ver;
              this.receivedTelemetry = false;
              // Clear any mission-ended overlay from previous mission
              const endedOverlay = document.getElementById('missionEndedOverlay');
              if (endedOverlay) endedOverlay.classList.remove('active');
              // Continue processing to show the new mission state
            }
          }
          this.onTelemetryReceived();
          this.onTelemetry(m.data);
        } else if (m.topic === 'error') {
          console.error('Server error:', m.error);
        }
      } catch (e) {
        console.error('Error processing message:', e);
      }
    };
  }

  /** Override in station-specific code to handle telemetry payloads. */
  onTelemetry(data) {}

  /** Send a command to the server. */
  sendCommand(topic, data) {
    const cmd = { topic, data };
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(cmd));
    } else {
      // Queue command for when connection is re-established (up to 10 commands)
      this._commandQueue.push(cmd);
      if (this._commandQueue.length > 10) {
        this._commandQueue.shift(); // Drop oldest command if queue is full
      }
    }
  }

  // ---- Mission state overlays ----

  handleStatusMessage(d) {
    const idleBanner = document.getElementById('idleBanner');
    const endedOverlay = document.getElementById('missionEndedOverlay');

    if (d.status === 'idle' || d.status === 'loading') {
      // Show idle banner during both idle and loading states
      if (idleBanner) idleBanner.classList.add('active');
      if (endedOverlay) endedOverlay.classList.remove('active');
    } else if (d.status === 'active') {
      // Mission is active - clear idle banner and latch mission version
      if (idleBanner) idleBanner.classList.remove('active');
      if (endedOverlay) endedOverlay.classList.remove('active');
      if (d.missionVersion !== undefined) {
        this._knownMissionVersion = d.missionVersion;
      }
    } else if (d.status === 'ended') {
      const title = document.getElementById('endedTitle');
      const reason = document.getElementById('endedReason');
      const outcome = d.outcome || {};
      if (title) {
        title.className = outcome.status === 'victory' ? 'victory-title' : outcome.status === 'defeat' ? 'defeat-title' : '';
        title.textContent = outcome.status === 'victory' ? 'VICTORY' : outcome.status === 'defeat' ? 'DEFEAT' : 'MISSION ENDED';
      }
      if (reason) reason.textContent = outcome.reason || '';
      if (endedOverlay) endedOverlay.classList.add('active');
      if (idleBanner) idleBanner.classList.remove('active');
    }
  }

  onTelemetryReceived() {
    if (!this.receivedTelemetry) {
      this.receivedTelemetry = true;
      document.getElementById('idleBanner').classList.remove('active');
    }
  }

  // ---- Damage LEDs ----

  updateDamageLEDs(damage) {
    const hullLed = document.getElementById('hullDamage');
    const floodLed = document.getElementById('flooding');
    if (!hullLed || !floodLed) return;

    if (damage.hull > 0.1) {
      hullLed.className = 'damage-led hull-damage flashing';
      hullLed.title = `Hull Damage: ${(damage.hull * 100).toFixed(0)}%`;
    } else {
      hullLed.className = 'damage-led normal';
      hullLed.title = 'Hull Damage: OK';
    }

    if (damage.flooding_rate > 0.5) {
      floodLed.className = 'damage-led flooding flashing';
      floodLed.title = `Flooding Rate: ${damage.flooding_rate.toFixed(1)}`;
    } else {
      floodLed.className = 'damage-led normal';
      floodLed.title = 'Flooding: OK';
    }
  }

  // ---- dB Meter ----

  setDbMeter(db) {
    const dbWrap = document.getElementById('dbWrap');
    const dbFill = document.getElementById('dbFill');
    const dbPeak = document.getElementById('dbPeak');
    if (!dbWrap || !dbFill || !dbPeak) return;
    const h = dbWrap.clientHeight || 160;
    const clamped = Math.max(0, Math.min(100, db));
    const px = Math.floor((clamped / 100) * h);
    dbFill.style.height = px + 'px';
    this.peakHold = Math.max(this.peakHold - 1, px);
    dbPeak.style.bottom = (this.peakHold - 2) + 'px';
  }

  // ---- Task management ----

  updateTasks(tasks, containerSelector) {
    const row = typeof containerSelector === 'string'
      ? document.querySelector(containerSelector)
      : document.getElementById('tasksRow');
    if (!row) return;

    if (!tasks || tasks.length === 0) {
      this.existingTaskElements.clear();
      row.innerHTML = '';
      const pill = document.createElement('div');
      pill.className = 'pill';
      pill.textContent = 'No Active Tasks';
      pill.id = 'task';
      pill.style.color = '#D7E3FF';
      pill.style.borderColor = '#1B2440';
      pill.style.backgroundColor = '#0F162A';
      row.appendChild(pill);
      return;
    }

    // Remove placeholder
    const placeholder = document.getElementById('task');
    if (placeholder) {
      placeholder.parentElement && placeholder.parentElement.removeChild(placeholder);
    }

    // Remove stale tasks
    const currentIds = new Set(tasks.map(t => t.id));
    for (const [tid, el] of Array.from(this.existingTaskElements.entries())) {
      if (!currentIds.has(tid)) {
        if (el && el.parentElement) el.parentElement.removeChild(el);
        this.existingTaskElements.delete(tid);
      }
    }

    const station = this.stationName;
    const ws = this.ws;

    tasks.forEach((task) => {
      const taskId = task.id;
      const label = task.stage === 'task' ? 'MAINTENANCE' : (task.stage === 'failing' ? 'FAILING' : 'FAILED');
      const text = `🔧 ${task.title} — ${label} ${(task.progress * 100).toFixed(0)}% — time to complete ${Math.max(0, task.time_remaining_s).toFixed(0)}s${task.started ? ' (REPAIRING)' : ''}`;

      if (this.existingTaskElements.has(taskId)) {
        const existingPill = this.existingTaskElements.get(taskId);
        const textSpan = existingPill.querySelector('.task-text');
        if (textSpan) textSpan.textContent = text;
      } else {
        const color = task.stage === 'task' ? '#FACC15' : (task.stage === 'failing' ? '#F59E0B' : '#EF4444');
        const bg = task.stage === 'task' ? 'rgba(250, 204, 21, 0.1)' : (task.stage === 'failing' ? 'rgba(245, 158, 11, 0.1)' : 'rgba(239, 68, 68, 0.1)');

        const pill = document.createElement('div');
        pill.className = 'pill';
        pill.style.color = color;
        pill.style.borderColor = color;
        pill.style.backgroundColor = bg;

        const textSpan = document.createElement('span');
        textSpan.className = 'task-text';
        textSpan.textContent = text;

        const btn = document.createElement('button');
        btn.textContent = 'Repair';
        btn.style.marginLeft = '8px';
        btn.onclick = () => {
          ws.send(JSON.stringify({ topic: 'station.task.start', data: { station, task_id: taskId } }));
        };

        pill.appendChild(textSpan);
        pill.appendChild(btn);
        row.appendChild(pill);
        this.existingTaskElements.set(taskId, pill);
      }
    });
  }

  // ---- Audio ----

  initAudio() {
    this.audio = new SubmarineAudio(this.stationName);
    document.body.insertAdjacentHTML('beforeend', SubmarineAudio.createMuteButtonHTML());
    this.audio.autoInit();
  }
}
