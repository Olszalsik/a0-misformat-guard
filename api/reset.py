"""Reset the in-memory misformat counter snapshot."""

from helpers.api import ApiHandler
from usr.plugins.misformat_guard.api import misformat_stats


class Reset(ApiHandler):
    async def process(self, input_data, request):
        misformat_stats.reset()
        return {"ok": True, "counters": misformat_stats.snapshot(None)}
