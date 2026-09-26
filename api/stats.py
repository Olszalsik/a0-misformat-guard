"""Read the live counter snapshot for the WebUI dashboard.

Registered as a POST handler (the framework default) so it keeps CSRF
protection, which the settings panel needs: it mutates nothing but the
dashboard store is a browser context and CSRF is the safe default. It is
also exposed on GET for read-only callers such as a manual health check;
GET routes are declared explicitly because the framework returns 405 for any
method missing from get_methods() (helpers/api.py). CSRF is not required for
GET -- a cross-site GET cannot mutate anything.
"""

from helpers.api import ApiHandler
from usr.plugins.misformat_guard.api import misformat_stats


class Stats(ApiHandler):
    @classmethod
    def get_methods(cls) -> list[str]:
        return ["GET", "POST"]

    async def process(self, input_data, request):
        snap = misformat_stats.snapshot(None)
        return {"ok": True, "counters": snap}
