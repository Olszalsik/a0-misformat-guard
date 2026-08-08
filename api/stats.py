"""GET the live counter snapshot for the WebUI dashboard."""

from helpers.api import ApiHandler
from usr.plugins.misformat_guard.api import misformat_stats


class Stats(ApiHandler):
    async def process(self, input_data, request):
        snap = misformat_stats.snapshot(None)
        return {"ok": True, "counters": snap}
