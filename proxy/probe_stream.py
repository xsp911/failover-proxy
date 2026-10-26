"""Compact detector-only SSE collection, without buffering reasoning/events."""
from .engine import Completion, GatewayError


class ProbeCompletion(Completion):
    def __post_init__(self):
        pass

    def add(self, item):
        count = getattr(self, 'probe_event_count', 0) + 1
        self.probe_event_count = count
        if count > self.max_events:
            raise GatewayError('response_event_limit')
        # Reuse production protocol/error/finish validation, but never retain the
        # full SSE frame history: token-by-token reasoning can be megabytes.
        super().add(item)
        for choice in item.get('choices', []):
            if choice.get('index', 0) != 0:
                continue
            delta = choice.get('delta') or {}
            # Never execute model-requested tools or treat their arguments as
            # fingerprint numbers. A tool-only answer remains unscorable, not fake.
            content = delta.get('content') or ''
            self.probe_text = getattr(self, 'probe_text', '') + content
            if len(self.probe_text) > 65536:
                raise GatewayError('probe_content_limit')
        self.events.clear()

    @property
    def text(self):
        return getattr(self, 'probe_text', '')
