/**
 * misformat_guard WebUI store (v0.4.1).
 *
 * - Polls /api/plugins/misformat_guard/stats every 60s for live counters.
 *   Polling pauses when the document is hidden (tab in background) to
 *   avoid the v0.3.0 UI freeze that happened when polling ran while the
 *   dashboard page was being constructed.
 * - Exposes window.MisformatGuardDashboard with .config(), .setConfig(),
 *   .reset(), and .stats() helpers for the settings page to call.
 */

(function () {
  'use strict';
  const BASE = '/api/plugins/misformat_guard';
  // 60s cadence + visibility-pause: counters change slowly, and 3-5s
  // + always-on was the v0.3.0 freeze cause. Polling only matters while
  // the dashboard page is mounted and visible.
  const REFRESH_MS = 60000;

  async function call(path, body) {
    const opts = {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      credentials: 'same-origin',
    };
    opts.body = JSON.stringify(body || {});
    const r = await fetch(BASE + path, opts);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  const MisformatGuardDashboard = {
    async stats() { return (await call('/stats')).counters || {}; },
    async config() { return (await call('/config')) || {}; },
    async setConfig(overrides) { return await call('/config', overrides); },
    async reset() { return await call('/reset'); },
    async health() { return (await call('/health')) || {}; },
  };

  window.MisformatGuardDashboard = MisformatGuardDashboard;

  // Lightweight auto-refresh: find elements with data-mg-stat="key"
  // and update their textContent whenever stats come back.
  async function refresh() {
    try {
      const counters = await MisformatGuardDashboard.stats();
      document.querySelectorAll('[data-mg-stat]').forEach(function (el) {
        const k = el.getAttribute('data-mg-stat');
        if (k in counters) el.textContent = counters[k];
      });
    } catch (_e) { /* swallow transient failures */ }
  }

  // v0.4.1: re-enable polling with a visibility-pause guard so the
  // v0.3.0 freeze does not return. When the tab is hidden we skip
  // the refresh and the next visible tick will re-fetch on schedule.
  let pollTimer = null;
  function startPolling() {
    if (pollTimer !== null) return;
    refresh();
    pollTimer = setInterval(function () {
      if (document.visibilityState === 'visible') {
        refresh();
      }
    }, REFRESH_MS);
  }
  function stopPolling() {
    if (pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') {
      startPolling();
    } else {
      stopPolling();
    }
  });
  document.addEventListener('DOMContentLoaded', function () {
    if (document.visibilityState === 'visible') {
      startPolling();
    }
  });
  console.log('[misformat_guard] dashboard loaded; API at ' + BASE);
})();
