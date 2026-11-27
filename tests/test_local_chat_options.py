from types import SimpleNamespace
from unittest.mock import Mock, patch

from llm_api.openai_api import _build_request_kwargs, stream_chat_with_gpt


MESSAGES = [{'role': 'user', 'content': 'Write one sentence.'}]


def test_thinking_toggle_is_opt_in():
    fields = _build_request_kwargs(MESSAGES, 'test-model', 100, 1, False, 'high', None, None, True)
    assert 'extra_body' not in fields
    assert fields['reasoning_effort'] == 'high'


def test_thinking_toggle_reaches_streaming_and_nonstreaming_fallback():
    streamed = Mock()
    streamed.chat.completions.create.side_effect = RuntimeError('Stream unavailable')
    blocking = Mock()
    blocking.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(index=0, message=SimpleNamespace(content='Some prose.'))],
    )
    with patch('llm_api.openai_api._build_openai_client', side_effect=[streamed, blocking]):
        result = list(stream_chat_with_gpt(
            MESSAGES, model='test-model', api_key='test-key', base_url='http://127.0.0.1:8000/v1',
            enable_thinking=False, reasoning_effort='high',
        ))
    assert result[-1].response == 'Some prose.'
    for client, is_stream in ((streamed, True), (blocking, False)):
        args = client.chat.completions.create.call_args.kwargs
        assert args['extra_body'] == {'chat_template_kwargs': {'enable_thinking': False}}
        assert 'reasoning_effort' not in args
        assert args['stream'] is is_stream
