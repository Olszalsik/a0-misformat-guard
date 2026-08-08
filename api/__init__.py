"""misformat_guard plugin - HTTP API handlers.

Endpoints exposed by this plugin (under /api/plugins/misformat_guard/*):
  - POST /config   : GET returns merged config; POST persists overrides.
  - POST /stats    : Returns the in-memory counter snapshot.
  - POST /reset    : Resets the in-memory counters.
  - POST /health   : Version + cascade summary + counter snapshot.
"""

# Public version constant. Bump in lockstep with plugin.yaml and
# hooks.PLUGIN_VERSION. The /health endpoint surfaces this so the
# dashboard and tests can pin the running version without parsing
# the manifest.
__version__ = "0.4.1"
