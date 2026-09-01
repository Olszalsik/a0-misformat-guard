from helpers.extension import Extension


class ResetStreak(Extension):
    def execute(self, loop_data=None, **kwargs):
        try:
            if not self.agent or loop_data is None:
                return
            params = getattr(loop_data, 'params_temporary', None)
            if not isinstance(params, dict):
                return
            params['_mg_streak'] = 0
            # v0.5.1 fix: these are the keys the cascade hooks actually
            # read (the old _mg_cascade_used_* names never existed, so
            # the budget was never reset between chats).
            params['_misformat_guard_cascade_used_in_streak'] = 0
            params['_misformat_guard_cascade_used_total'] = 0
        except Exception:
            pass
