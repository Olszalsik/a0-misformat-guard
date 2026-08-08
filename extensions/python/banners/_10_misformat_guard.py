from helpers.extension import Extension


class MisformatGuardBanner(Extension):
    def execute(self, banners=None, **kwargs):
        try:
            if banners is None:
                return
            banners.append({
                'title': 'Misformat Guard',
                'description': 'Repairs broken LLM responses with the utility model. Never gives up.',
                'icon': 'shield-check',
                'color': 'green',
                'link': '/plugins/misformat_guard',
            })
        except Exception:
            pass
