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
            params['_mg_cascade_used_streak'] = 0
            params['_mg_cascade_used_total'] = 0
        except Exception:
            pass
