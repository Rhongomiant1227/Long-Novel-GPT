"""Sample model output independently of the UI refresh rate and network chunk size."""

import math
import time


class GenerationSpeed:
    def __init__(self, count_tokens=None):
        self.started = time.monotonic()
        self.window_start = None
        self.fragments = []
        self.count_tokens = count_tokens
        self.template_tokens = None
        self.latest = None

    def _sample(self, rate, source):
        if rate > 0 and math.isfinite(rate):
            self.latest = {
                'tokens_per_second': round(rate, 1),
                'source': source,
                'sampled_at': time.time(),
            }
        return self.latest

    def observe(self, delta='', *, timings=None, output_tokens=None):
        now = time.monotonic()
        engine_rate = timings.get('predicted_per_second') if isinstance(timings, dict) else None
        if isinstance(engine_rate, (float, int)) and engine_rate > 0:
            return self._sample(engine_rate, 'provider')
        # Some gateways send a usage-only packet after the decoder timings.
        if self.latest and self.latest['source'] == 'provider':
            return self.latest
        if isinstance(output_tokens, int) and output_tokens > 0 and now > self.started:
            return self._sample(output_tokens / (now - self.started), 'usage')
        if not delta:
            return self.latest
        if self.window_start is None:
            self.window_start = now
            return self.latest
        self.fragments.append(delta)
        elapsed = now - self.window_start
        if elapsed < 1:
            return self.latest

        sample = ''.join(self.fragments)
        self.fragments.clear()
        self.window_start = now
        if self.count_tokens is not None:
            try:
                # Both calls use the same message template. Subtract its framing tokens.
                if self.template_tokens is None:
                    self.template_tokens = self.count_tokens('')
                count = self.count_tokens(sample) - self.template_tokens
                if count > 0:
                    return self._sample(count / elapsed, 'tokenizer')
            except Exception:
                # Optional telemetry must never interrupt or retry a generation.
                self.count_tokens = None
        return self._sample(len(sample.encode('utf-8')) / 4 / elapsed, 'estimate')


def measure_visible_stream(stream):
    """Providers without decoder telemetry still expose their incremental text."""
    meter = GenerationSpeed()
    length = 0
    while True:
        try:
            messages = next(stream)
        except StopIteration as done:
            messages = done.value
            if messages:
                usage = getattr(messages, 'usage', None) or {}
                messages.generation_speed = meter.observe(output_tokens=usage.get('output_tokens'))
            return messages
        value = messages.response
        if len(value) < length:
            meter = GenerationSpeed()
            length = 0
        messages.generation_speed = meter.observe(value[length:])
        length = len(value)
        yield messages
