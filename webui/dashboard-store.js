/**
 * misformat_guard WebUI store.
 *
 * Loaded on every page by extensions/webui/page-head/_10_misformat_guard_dashboard.html
 * as a module, which is what lets us import the framework's `fetchApi` and get
 * its X-CSRF-Token injection. The plugin's ApiHandlers default to
 * `requires_csrf() == requires_auth() == True`, so a raw `fetch()` without that
 * header is rejected with 403 -- which is how the whole panel used to fail
 * silently (the old call() had no token and refresh() swallowed the error).
 *
 * Polls /stats on a slow cadence and pauses while the document is hidden.
 * Exposes window.MisformatGuardDashboard for the settings panel in
 * webui/config.html.
 */

import { fetchApi } from "/js/api.js";
import { toastFrontendError } from "/components/notifications/notification-store.js";

(function () {
  'use strict';

  const BASE = "/api/plugins/misformat_guard";
  const REFRESH_MS = 60000;
  const MAX_BODY = 1 << 20; // 1 MiB guard against a runaway response

  async function call(path, body) {
    const request = {
      method: "POST",
      headers: { "content-type": "application/json" },
      credentials: "same-origin",
    };
    if (body !== undefined) request.body = JSON.stringify(body || {});

    const r = await fetchApi(BASE + path, request);
    if (!r.ok) {
      const detail = await r.text().catch(() => "");
      const err = new Error(
        "HTTP " + r.status + (detail ? " — " + detail.slice(0, 200) : "")
      );
      err.status = r.status;
      throw err;
    }
    const text = await r.text();
    if (!text) return {};
    if (text.length > MAX_BODY) {
      throw new Error("response too large (" + text.length + " bytes)");
    }
    return JSON.parse(text);
  }

  const MisformatGuardDashboard = {
    async stats() {
      return (await call("/stats")).counters || {};
    },
    async config() {
      return (await call("/config")) || {};
    },
    async setConfig(overrides) {
      return await call("/config", overrides);
    },
    async reset() {
      return await call("/reset");
    },
    async health() {
      return (await call("/health")) || {};
    },
  };

  window.MisformatGuardDashboard = MisformatGuardDashboard;

  // Live update of any element carrying data-mg-stat="<counter>".
  let warned = false;

  async function refresh() {
    try {
      const counters = await MisformatGuardDashboard.stats();
      document
        .querySelectorAll("[data-mg-stat]")
        .forEach(function (el) {
          const k = el.getAttribute("data-mg-stat");
          if (k in counters) el.textContent = counters[k];
        });
      warned = false;
    } catch (e) {
      // Surface it once instead of failing silently. A CSRF/auth/404 failure
      // here means the whole panel is non-functional, which the user must see.
      if (!warned) {
        warned = true;
        if (e && e.status === 403) {
          toastFrontendError(
            "misformat_guard: stats request rejected (403). The panel is read-only; " +
              "the framework CSRF token could not be applied."
          );
        } else {
          toastFrontendError("misformat_guard: stats unavailable — " + (e && e.message ? e.message : e));
        }
      }
    }
  }

  let pollTimer = null;

  function startPolling() {
    if (pollTimer !== null) return;
    refresh();
    pollTimer = setInterval(function () {
      // Pause while the tab is hidden: a hidden tab polling every minute
      // across many chats was the v0.3.0 UI-freeze cause.
      if (document.hidden) return;
      refresh();
    }, REFRESH_MS);
  }

  function stopPolling() {
    if (pollTimer === null) return;
    clearInterval(pollTimer);
    pollTimer = null;
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      stopPolling();
    } else {
      refresh();
      startPolling();
    }
  });

  startPolling();
})();
