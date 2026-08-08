from helpers.extension import Extension


class DetectMisformat(Extension):
    def execute(self, loop_data=None, **kwargs):
        try:
            if not self.agent or loop_data is None:
                return
            params = getattr(loop_data, 'params_temporary', None)
            if not isinstance(params, dict):
                return
            history = getattr(self.agent, 'history', None)
            if history is None:
                return
            messages = getattr(history, 'messages', None)
            if not messages:
                params['_mg_streak'] = 0
                return
            last = messages[-1]
            content = getattr(last, 'content', '')
            if not isinstance(content, str):
                return
            if 'misformatted your message' in content.lower():
                params['_mg_streak'] = int(params.get('_mg_streak', 0) or 0) + 1
            else:
                params['_mg_streak'] = 0
        except Exception:
            pass
