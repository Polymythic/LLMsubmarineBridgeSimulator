/**
 * Submarine Bridge Simulator - Audio System
 *
 * Shared audio module for all stations. Handles:
 * - Ambient background sounds
 * - Alarms (critical, warning, diving, surface, general warning/alarm)
 * - Distance-based sound effects (pings, explosions)
 * - Event-triggered sounds (ship sinking, torpedo launch)
 */

class SubmarineAudio {
  constructor(stationName) {
    this.station = stationName;
    this.muted = false;
    this.masterVolume = 0.7;
    this.ambientVolume = 0.3;
    this.initialized = false;

    // Track state for triggering sounds
    this.lastDepth = null;
    this.wasAtPeriscopeDepth = false;
    this.wasSurfaced = false;          // for surface-alert rising edge
    this._wasWarning = false;          // for general-warning rising edge (degraded)
    this._wasCritical = false;         // for general-alarm rising edge (failed)
    this.playingAlarms = {};

    // Sound file paths
    this.soundPaths = {
      // Ambient
      ambience: '/assets/sounds/deep-sea-ambience-6933.mp3',

      // Alarms
      alarmCritical: '/assets/sounds/alarm_critical.mp3',
      alarmWarning: '/assets/sounds/alarm_warning.mp3',
      alarmDiving: '/assets/sounds/diving_alarm.wav',
      surfaceAlert: '/assets/sounds/surface_alert.wav',
      generalWarning: '/assets/sounds/general_warning.wav',
      generalAlarm: '/assets/sounds/general_alarm.wav',

      // Effects
      depthCharge: '/assets/sounds/depth_charge.mp3',
      shipSinking: '/assets/sounds/ship_sinking.mp3',
      torpedoFire: '/assets/sounds/torpedo_fire.wav',
      pingEnemy: '/assets/sounds/sonar_ping_enemy.mp3',
      pingFriendly: '/assets/sounds/sonar_ping_friendly.mp3',
      morseCode: '/assets/sounds/morse_code.mp3',
    };

    // Preloaded audio elements
    this.sounds = {};

    // Ambient loop element (separate for looping)
    this.ambientLoop = null;
  }

  /**
   * Initialize audio system. Must be called after user interaction (browser policy).
   */
  init() {
    if (this.initialized) return;

    // Preload all sounds
    for (const [name, path] of Object.entries(this.soundPaths)) {
      this.sounds[name] = new Audio(path);
      this.sounds[name].preload = 'auto';
    }

    // Setup ambient loop
    this.ambientLoop = new Audio(this.soundPaths.ambience);
    this.ambientLoop.loop = true;
    this.ambientLoop.volume = this.ambientVolume * this.masterVolume;

    this.initialized = true;
    console.log(`[Audio] Initialized for ${this.station} station`);

    // Try to start ambient immediately after init
    this.startAmbient();

    // Process any pending telemetry to start alarms immediately
    if (this._pendingTelemetry) {
      console.log(`[Audio] Processing pending telemetry for alarms`);
      this._processAlarms(this._pendingTelemetry);
      this._pendingTelemetry = null;
    }
  }

  /**
   * Internal: Process alarms without the initialized check.
   */
  _processAlarms(data) {
    if (this.station === 'captain') {
      this.processCaptainAlarms(data);
    }
  }

  /**
   * Setup automatic initialization on any user interaction.
   * Call this once after creating the audio instance.
   */
  autoInit() {
    const initOnInteraction = () => {
      this.init();
      this.setupMuteButton();
    };

    // Listen for any user interaction
    ['click', 'keydown', 'touchstart'].forEach(event => {
      document.addEventListener(event, initOnInteraction, { once: true, capture: true });
    });

    // Also try to init immediately (will work if user already interacted)
    try {
      this.init();
      this.setupMuteButton();
    } catch (e) {
      // Ignore - will init on first interaction
    }
  }

  /**
   * Start ambient background sound.
   */
  startAmbient() {
    if (!this.initialized || this.muted) return;

    if (this.ambientLoop && this.ambientLoop.paused) {
      this.ambientLoop.volume = this.ambientVolume * this.masterVolume;
      this.ambientLoop.play().catch(e => console.log('[Audio] Ambient autoplay blocked'));
    }
  }

  /**
   * Stop ambient background sound.
   */
  stopAmbient() {
    if (this.ambientLoop) {
      this.ambientLoop.pause();
    }
  }

  /**
   * Play a sound effect once.
   * @param {string} soundName - Key from soundPaths
   * @param {number} volume - Volume 0.0 to 1.0 (before master volume)
   */
  play(soundName, volume = 1.0) {
    if (!this.initialized || this.muted) return;

    const sound = this.sounds[soundName];
    if (!sound) {
      console.warn(`[Audio] Unknown sound: ${soundName}`);
      return;
    }

    // Clone the audio to allow overlapping plays
    const clone = sound.cloneNode();
    clone.volume = Math.min(1.0, volume * this.masterVolume);
    clone.play().catch(e => console.log(`[Audio] Play blocked: ${soundName}`));
  }

  /**
   * Play a looping alarm sound.
   * @param {string} alarmName - Key from soundPaths
   */
  startAlarm(alarmName) {
    if (!this.initialized || this.muted) return;
    if (this.playingAlarms[alarmName]) return; // Already playing

    const sound = this.sounds[alarmName];
    if (!sound) return;

    const alarm = sound.cloneNode();
    alarm.loop = true;
    alarm.volume = 0.6 * this.masterVolume;
    alarm.play().catch(e => console.log(`[Audio] Alarm blocked: ${alarmName}`));

    this.playingAlarms[alarmName] = alarm;
  }

  /**
   * Stop a looping alarm sound.
   * @param {string} alarmName - Key from soundPaths
   */
  stopAlarm(alarmName) {
    const alarm = this.playingAlarms[alarmName];
    if (alarm) {
      alarm.pause();
      alarm.currentTime = 0;
      delete this.playingAlarms[alarmName];
    }
  }

  /**
   * Stop all alarms.
   */
  stopAllAlarms() {
    for (const name of Object.keys(this.playingAlarms)) {
      this.stopAlarm(name);
    }
  }

  /**
   * Calculate volume based on distance (quantized to 25% increments).
   * @param {number} distance - Distance in meters
   * @param {number} maxDistance - Distance at which sound is inaudible
   * @returns {number} Volume 0.0, 0.25, 0.50, 0.75, or 1.0
   */
  volumeByDistance(distance, maxDistance = 10000) {
    if (distance <= 0) return 1.0;
    if (distance >= maxDistance) return 0.0;

    // Calculate continuous volume first
    const normalized = distance / maxDistance;
    const continuous = Math.max(0, 1.0 - Math.pow(normalized, 0.5));

    // Quantize to 25% increments (0, 0.25, 0.5, 0.75, 1.0)
    return Math.round(continuous * 4) / 4;
  }

  /**
   * Set master volume.
   * @param {number} vol - Volume 0.0 to 1.0
   */
  setMasterVolume(vol) {
    this.masterVolume = Math.max(0, Math.min(1, vol));
    if (this.ambientLoop) {
      this.ambientLoop.volume = this.ambientVolume * this.masterVolume;
    }
  }

  /**
   * Toggle mute state.
   */
  toggleMute() {
    this.muted = !this.muted;
    if (this.muted) {
      this.stopAmbient();
      this.stopAllAlarms();
    } else {
      this.startAmbient();
    }
    return this.muted;
  }

  /**
   * Process telemetry data and trigger appropriate sounds.
   * Call this from ws.onmessage handler.
   * @param {object} data - Telemetry data object
   */
  processTelemetry(data) {
    // Store telemetry for processing when audio initializes
    this._pendingTelemetry = data;

    if (!this.initialized) return;

    // === AMBIENT (all stations) ===
    this.startAmbient();

    // === DIVING / SURFACE ALARMS (all stations) ===
    // Diving: going from periscope depth (<=20m) to deeper.
    // Surface: breaking the surface (depth <= 2m) after being submerged.
    const PERISCOPE_DEPTH = 20;
    const SURFACE_DEPTH = 2;
    if (data.ownship && typeof data.ownship.depth === 'number') {
      const currentDepth = data.ownship.depth;
      const atPeriscope = currentDepth <= PERISCOPE_DEPTH;
      const surfaced = currentDepth <= SURFACE_DEPTH;

      if (this.lastDepth !== null) {
        // Was at periscope, now going deeper → diving alarm
        if (this.wasAtPeriscopeDepth && !atPeriscope && currentDepth > this.lastDepth) {
          this.play('alarmDiving', 0.8);
        }
        // Was submerged, now broken the surface → surface alert
        if (!this.wasSurfaced && surfaced) {
          this.play('surfaceAlert', 0.8);
        }
      }

      this.lastDepth = currentDepth;
      this.wasAtPeriscopeDepth = atPeriscope;
      this.wasSurfaced = surfaced;
    }

    // === FRIENDLY PING (all stations) ===
    if (data.lastPingAt) {
      // Check if this is a new ping (compare timestamps)
      if (this._lastFriendlyPingAt !== data.lastPingAt) {
        this._lastFriendlyPingAt = data.lastPingAt;
        this.play('pingFriendly', 0.7);
      }
    }

    // === ENEMY PINGS (all stations) - distance based ===
    if (data.enemyPings && Array.isArray(data.enemyPings)) {
      for (const ping of data.enemyPings) {
        const pingId = ping.id || ping.at;
        if (!this._playedEnemyPings) this._playedEnemyPings = new Set();

        if (!this._playedEnemyPings.has(pingId)) {
          this._playedEnemyPings.add(pingId);
          const distance = ping.distance || 5000;
          const volume = this.volumeByDistance(distance, 15000);
          if (volume > 0.05) {
            this.play('pingEnemy', volume);
          }

          // Cleanup old ping IDs (keep last 50)
          if (this._playedEnemyPings.size > 50) {
            const arr = Array.from(this._playedEnemyPings);
            this._playedEnemyPings = new Set(arr.slice(-25));
          }
        }
      }
    }

    // === DEPTH CHARGE EXPLOSIONS (all stations) - distance based ===
    if (data.explosions && Array.isArray(data.explosions)) {
      for (const exp of data.explosions) {
        const expId = exp.at || `${exp.bearing}_${Date.now()}`;
        if (!this._playedExplosions) this._playedExplosions = new Set();

        if (!this._playedExplosions.has(expId)) {
          this._playedExplosions.add(expId);
          // Explosions from sonar don't have distance, estimate from bearing confidence
          const distance = exp.distance || 3000;
          const volume = this.volumeByDistance(distance, 8000);
          if (volume > 0.1) {
            this.play('depthCharge', volume);
          }

          // Cleanup
          if (this._playedExplosions.size > 50) {
            const arr = Array.from(this._playedExplosions);
            this._playedExplosions = new Set(arr.slice(-25));
          }
        }
      }
    }

    // === TORPEDO DETONATIONS (all stations) - distance based ===
    this.processTorpedoEvents(data);

    // === OWNSHIP TORPEDO LAUNCH (all stations) ===
    this.processWeaponsFireEvents(data);

    // === CAPTAIN-ONLY ALARMS ===
    if (this.station === 'captain') {
      this.processCaptainAlarms(data);
    }

    // === SONAR-ONLY: SHIP SINKING (longer duration sound) ===
    if (this.station === 'sonar') {
      this.processSonarEvents(data);
    }
  }

  /**
   * Process torpedo detonation events (all stations hear explosions).
   */
  processTorpedoEvents(data) {
    // Get ownship position for distance calculation
    const ownX = data.ownship?.x || 0;
    const ownY = data.ownship?.y || 0;

    if (data.events && Array.isArray(data.events)) {
      for (const event of data.events) {
        if (event.type === 'torpedo.detonated') {
          if (!this._playedTorpedoExplosions) this._playedTorpedoExplosions = new Set();
          const expId = `torp_${event.target}_${event.at || Date.now()}`;

          if (!this._playedTorpedoExplosions.has(expId)) {
            this._playedTorpedoExplosions.add(expId);

            // Calculate distance for volume
            let volume = 0.9;
            if (typeof event.x === 'number' && typeof event.y === 'number') {
              const dx = event.x - ownX;
              const dy = event.y - ownY;
              const distance = Math.sqrt(dx * dx + dy * dy);
              volume = this.volumeByDistance(distance, 15000);
            }
            if (volume > 0.1) {
              this.play('depthCharge', volume); // Use depth charge sound for explosion
            }

            // Cleanup
            if (this._playedTorpedoExplosions.size > 50) {
              const arr = Array.from(this._playedTorpedoExplosions);
              this._playedTorpedoExplosions = new Set(arr.slice(-25));
            }
          }
        }
      }
    }
  }

  /**
   * Process ownship torpedo launches (heard ship-wide on all stations).
   * The backend emits a one-shot 'weapons.fire' transient event when the
   * player fires; we dedupe by its timestamp so it plays exactly once.
   */
  processWeaponsFireEvents(data) {
    if (!data.events || !Array.isArray(data.events)) return;
    for (const event of data.events) {
      if (event.type !== 'weapons.fire') continue;
      if (!this._playedTorpedoFires) this._playedTorpedoFires = new Set();
      const fireId = event.at || `fire_${event.tube}_${Date.now()}`;
      if (this._playedTorpedoFires.has(fireId)) continue;
      this._playedTorpedoFires.add(fireId);
      this.play('torpedoFire', 0.85);

      // Cleanup old ids
      if (this._playedTorpedoFires.size > 50) {
        const arr = Array.from(this._playedTorpedoFires);
        this._playedTorpedoFires = new Set(arr.slice(-25));
      }
    }
  }

  /**
   * Process captain-specific alarm conditions.
   */
  processCaptainAlarms(data) {
    // Check station status strings (Failed/Degraded/OK)
    const stationStatus = data.stationStatus || {};
    // Check system booleans
    const systems = data.systems || {};
    // Check maintenance levels
    const maintenance = data.maintenance?.levels || data.maintenance || {};

    let hasCritical = false;
    let hasWarning = false;

    // Check stationStatus for Failed/Degraded (most reliable indicator)
    for (const [station, status] of Object.entries(stationStatus)) {
      if (status === 'Failed') {
        hasCritical = true;
      } else if (status === 'Degraded') {
        hasWarning = true;
      }
    }

    // Check systems status booleans (rudder_ok, sonar_ok, etc.)
    for (const [system, ok] of Object.entries(systems)) {
      if (ok === false) {
        hasCritical = true;
      }
    }

    // Check maintenance levels as fallback (0.0 = failed, <0.5 = degraded)
    for (const [system, level] of Object.entries(maintenance)) {
      if (typeof level === 'number') {
        if (level <= 0.1) {
          hasCritical = true;
        } else if (level < 0.5) {
          hasWarning = true;
        }
      }
    }

    // Manage alarm states - critical supersedes warning
    if (hasCritical) {
      this.startAlarm('alarmCritical');
      this.stopAlarm('alarmWarning');
    } else if (hasWarning) {
      this.stopAlarm('alarmCritical');
      this.startAlarm('alarmWarning');
    } else {
      this.stopAlarm('alarmCritical');
      this.stopAlarm('alarmWarning');
    }

    // One-shot announcements on the rising edge of each fault tier: a system
    // going degraded sounds the general warning; going critical/failed sounds
    // the general alarm. Critical supersedes warning so a straight escalation
    // doesn't double-announce. The looping klaxons above persist the state.
    if (hasCritical) {
      if (!this._wasCritical) this.play('generalAlarm', 0.8);
    } else if (hasWarning) {
      if (!this._wasWarning) this.play('generalWarning', 0.8);
    }
    this._wasCritical = hasCritical;
    this._wasWarning = hasWarning;
  }

  /**
   * Process sonar-specific events (ship sinking sounds - longer duration).
   * Explosion sounds are handled globally by processTorpedoEvents.
   */
  processSonarEvents(data) {
    // Get ownship position for distance calculation
    const ownX = data.ownship?.x || 0;
    const ownY = data.ownship?.y || 0;

    if (data.events && Array.isArray(data.events)) {
      for (const event of data.events) {
        // Torpedo detonation that destroyed a target - play sinking sound after delay
        if (event.type === 'torpedo.detonated' && event.target_destroyed) {
          if (!this._playedSinkings) this._playedSinkings = new Set();
          const sinkId = `torp_sink_${event.target}_${event.at || Date.now()}`;

          if (!this._playedSinkings.has(sinkId)) {
            this._playedSinkings.add(sinkId);

            // Calculate distance for volume
            let volume = 0.8;
            if (typeof event.x === 'number' && typeof event.y === 'number') {
              const dx = event.x - ownX;
              const dy = event.y - ownY;
              const distance = Math.sqrt(dx * dx + dy * dy);
              volume = this.volumeByDistance(distance, 15000);
            }
            if (volume > 0.1) {
              // Delay sinking sound by 1.5 seconds after explosion
              setTimeout(() => {
                this.play('shipSinking', volume);
              }, 1500);
            }

            // Cleanup
            if (this._playedSinkings.size > 20) {
              const arr = Array.from(this._playedSinkings);
              this._playedSinkings = new Set(arr.slice(-10));
            }
          }
        }

        // Ship destroyed event (may come separately if ship was already damaged from multiple hits)
        if (event.type === 'ship.destroyed') {
          if (!this._playedSinkings) this._playedSinkings = new Set();
          const sinkId = event.target || event.id || Date.now();

          if (!this._playedSinkings.has(sinkId)) {
            this._playedSinkings.add(sinkId);

            // Calculate distance for volume
            let volume = 0.8;
            if (typeof event.x === 'number' && typeof event.y === 'number') {
              const dx = event.x - ownX;
              const dy = event.y - ownY;
              const distance = Math.sqrt(dx * dx + dy * dy);
              volume = this.volumeByDistance(distance, 15000);
            }
            if (volume > 0.1) {
              this.play('shipSinking', volume);
            }

            // Cleanup
            if (this._playedSinkings.size > 20) {
              const arr = Array.from(this._playedSinkings);
              this._playedSinkings = new Set(arr.slice(-10));
            }
          }
        }
      }
    }
  }

  /**
   * Create mute toggle button HTML.
   * @returns {string} HTML for mute button
   */
  static createMuteButtonHTML() {
    return `
      <button id="audioMuteBtn" title="Toggle audio mute" style="
        position: fixed;
        bottom: 10px;
        right: 10px;
        padding: 8px 12px;
        background: #26406E;
        border: 1px solid #1B2440;
        color: #D7E3FF;
        border-radius: 6px;
        cursor: pointer;
        z-index: 1000;
        font-size: 14px;
      ">🔊 Audio</button>
    `;
  }

  /**
   * Setup mute button click handler.
   */
  setupMuteButton() {
    const btn = document.getElementById('audioMuteBtn');
    if (btn) {
      btn.onclick = () => {
        const muted = this.toggleMute();
        btn.textContent = muted ? '🔇 Muted' : '🔊 Audio';
        btn.style.background = muted ? '#4A2020' : '#26406E';
      };
    }
  }
}

// Export for use in station pages
window.SubmarineAudio = SubmarineAudio;
