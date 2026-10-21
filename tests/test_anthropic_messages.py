from llm_api.chat_messages import ChatMessages
from llm_api import anthropic_api


def _drain_generator(gen):
    last = None
    while True:
        try:
            last = next(gen)
        except StopIteration as exc:
            return exc.value if exc.value is not None else last


def test_stream_chat_with_anthropic_non_stream_fallback(monkeypatch):
    class FakeStreamResponse:
        def raise_for_status(self):
            return None

        def iter_lines(self):
            return iter(())

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "content": [{"type": "text", "text": "OK"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def stream(self, method, url, json):
            raise RuntimeError("stream unsupported")

        def post(self, url, json):
            return FakeResponse()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(anthropic_api.httpx, "Client", FakeClient)

    result = _drain_generator(
        anthropic_api.stream_chat_with_anthropic(
            ChatMessages([{"role": "user", "content": "Return only OK."}], model="claude-opus-4-8"),
            model="claude-opus-4-8",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            max_tokens=16,
        )
    )

    assert result.response == "OK"
    assert result.api_endpoint_base_url == "https://example.invalid/v1"
