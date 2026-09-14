"""Bound web model calls without changing standalone CLI behavior."""
from types import SimpleNamespace


class LimitedClient:
    def __init__(self, client, charge, preflight=None):
        self.client = client
        self.charge = charge
        self.preflight = preflight
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def ensure_budget(self, needed):
        if self.preflight is not None:
            self.preflight(needed)

    def create(self, **kwargs):
        self.charge()
        kwargs['max_tokens'] = min(kwargs.get('max_tokens', 2048), 2048)
        try:
            return self.client.chat.completions.create(**kwargs)
        except Exception:
            raise RuntimeError('Model request failed. Contact the administrator or try later.') from None
