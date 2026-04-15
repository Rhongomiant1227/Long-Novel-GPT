from llm_api.chat_messages import ChatMessages
from llm_api import openai_api


def _drain_generator(gen):
    last = None
    while True:
        try:
            last = next(gen)
        except StopIteration as exc:
            return exc.value if exc.value is not None else last


def test_api_chain_skips_401_and_uses_next_endpoint(monkeypatch):
    openai_api._API_CHAIN_STATE.clear()
    calls = []

    def fake_single(*args, **kwargs):
        api_key = kwargs.get('api_key')
        calls.append(api_key)
        if api_key == 'bad-key':
            raise RuntimeError("Error code: 401 - {'code': 'INVALID_API_KEY', 'message': 'Invalid API key'}")
        result = ChatMessages([{'role': 'user', 'content': 'ping'}], model=kwargs.get('model'))
        result.append({'role': 'assistant', 'content': 'OK'})
        if False:
            yield None
        return result

    monkeypatch.setattr(openai_api, '_stream_chat_with_gpt_single_endpoint', fake_single)

    result = _drain_generator(
        openai_api.stream_chat_with_gpt(
            [{'role': 'user', 'content': 'ping'}],
            model='gpt-5.4',
            api_chain=[
                {'name': 'broken-primary', 'base_url': 'https://primary.invalid/v1', 'api_key': 'bad-key'},
                {'name': 'healthy-secondary', 'base_url': 'https://secondary.invalid/v1', 'api_key': 'good-key'},
            ],
            api_chain_rate_limit_retries=10,
        )
    )

    assert calls == ['bad-key', 'good-key']
    assert result.response == 'OK'
    assert result.api_endpoint_name == 'healthy-secondary'


def test_api_chain_retries_429_ten_times_before_succeeding(monkeypatch):
    openai_api._API_CHAIN_STATE.clear()
    attempts = {'count': 0}

    def fake_single(*args, **kwargs):
        attempts['count'] += 1
        if attempts['count'] <= 10:
            raise RuntimeError("Error code: 429 - {'message': 'Rate limit exceeded'}")
        result = ChatMessages([{'role': 'user', 'content': 'ping'}], model=kwargs.get('model'))
        result.append({'role': 'assistant', 'content': 'OK'})
        if False:
            yield None
        return result

    monkeypatch.setattr(openai_api, '_stream_chat_with_gpt_single_endpoint', fake_single)
    monkeypatch.setattr(openai_api.time, 'sleep', lambda *args, **kwargs: None)

    result = _drain_generator(
        openai_api.stream_chat_with_gpt(
            [{'role': 'user', 'content': 'ping'}],
            model='gpt-5.4',
            api_chain=[
                {'name': 'rate-limited-primary', 'base_url': 'https://primary.invalid/v1', 'api_key': 'good-key'},
                {'name': 'secondary', 'base_url': 'https://secondary.invalid/v1', 'api_key': 'fallback-key'},
            ],
            api_chain_rate_limit_retries=10,
        )
    )

    assert attempts['count'] == 11
    assert result.response == 'OK'
    assert result.api_endpoint_name == 'rate-limited-primary'


def test_api_chain_resets_to_highest_priority_after_rollover(monkeypatch):
    openai_api._API_CHAIN_STATE.clear()
    markers = iter([
        '2026-04-15 00:00',
        '2026-04-15 00:00',
        '2026-04-16 00:00',
        '2026-04-16 00:00',
    ])
    calls = []

    def fake_marker(reset_time):
        return next(markers)

    def fake_single(*args, **kwargs):
        calls.append(kwargs.get('api_key'))
        if kwargs.get('api_key') == 'primary-key':
            raise RuntimeError("Error code: 401 - {'code': 'INVALID_API_KEY'}")
        result = ChatMessages([{'role': 'user', 'content': 'ping'}], model=kwargs.get('model'))
        result.append({'role': 'assistant', 'content': 'OK'})
        if False:
            yield None
        return result

    monkeypatch.setattr(openai_api, '_current_rollover_marker', fake_marker)
    monkeypatch.setattr(openai_api, '_stream_chat_with_gpt_single_endpoint', fake_single)

    chain = [
        {'name': 'primary', 'base_url': 'https://primary.invalid/v1', 'api_key': 'primary-key'},
        {'name': 'secondary', 'base_url': 'https://secondary.invalid/v1', 'api_key': 'secondary-key'},
    ]

    _drain_generator(openai_api.stream_chat_with_gpt([{'role': 'user', 'content': 'ping'}], model='gpt-5.4', api_chain=chain))
    _drain_generator(openai_api.stream_chat_with_gpt([{'role': 'user', 'content': 'ping'}], model='gpt-5.4', api_chain=chain))

    assert calls == ['primary-key', 'secondary-key', 'primary-key', 'secondary-key']
