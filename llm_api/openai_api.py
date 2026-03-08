import httpx
from openai import OpenAI
from .chat_messages import ChatMessages

# Pricing reference: https://openai.com/api/pricing/
gpt_model_config = {
    "gpt-4o": {
        "Pricing": (2.50/1000, 10.00/1000),
        "currency_symbol": '$',
    },
    "gpt-4o-mini": {
        "Pricing": (0.15/1000, 0.60/1000),
        "currency_symbol": '$',
    },
    "o1-preview": {
        "Pricing": (15/1000, 60/1000),
        "currency_symbol": '$',
    },
    "o1-mini": {
        "Pricing": (3/1000, 12/1000),
        "currency_symbol": '$',
    },
}
# https://platform.openai.com/docs/guides/reasoning


def _build_openai_client(api_key=None, base_url=None, proxies=None, timeout=None, max_retries=None):
    if api_key is None:
        raise Exception('?????? api_key?')

    client_params = {
        'api_key': api_key,
    }

    if base_url:
        client_params['base_url'] = base_url

    if max_retries is not None:
        client_params['max_retries'] = max_retries

    httpx_kwargs = {
        'http2': False,
        'limits': httpx.Limits(max_connections=1, max_keepalive_connections=0),
        'headers': {'Connection': 'close'},
    }

    if proxies:
        httpx_kwargs['proxy'] = proxies

    if timeout:
        if isinstance(timeout, (int, float)):
            timeout = float(timeout)
            httpx_kwargs['timeout'] = httpx.Timeout(
                connect=min(timeout, 60.0),
                read=timeout,
                write=min(timeout, 120.0),
                pool=min(timeout, 120.0),
            )
        else:
            httpx_kwargs['timeout'] = timeout

    client_params['http_client'] = httpx.Client(**httpx_kwargs)
    return OpenAI(**client_params)


def _prepare_request_messages(messages, model):
    request_messages = [dict(message) for message in messages]
    if model in ['o1-preview'] and request_messages and request_messages[0]['role'] == 'system':
        request_messages[0:1] = [
            {'role': 'user', 'content': request_messages[0]['content']},
            {'role': 'assistant', 'content': ''},
        ]
    return request_messages


def _build_request_kwargs(messages, model, max_tokens, n, response_json, reasoning_effort, temperature, top_p, stream):
    request_kwargs = {
        'model': model,
        'messages': messages,
        'max_tokens': max_tokens,
        'n': n,
        'stream': stream,
    }

    if response_json:
        request_kwargs['response_format'] = {'type': 'json_object'}

    if reasoning_effort:
        request_kwargs['reasoning_effort'] = reasoning_effort

    if temperature not in (None, ''):
        request_kwargs['temperature'] = float(temperature)

    if top_p not in (None, ''):
        request_kwargs['top_p'] = float(top_p)

    return request_kwargs


def _content_to_text(content):
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                if item.get('type') == 'text':
                    parts.append(item.get('text', ''))
                elif 'content' in item:
                    parts.append(str(item.get('content', '')))
                elif 'text' in item:
                    parts.append(str(item.get('text', '')))
                continue
            text = getattr(item, 'text', None)
            if text is not None:
                parts.append(str(text))
        return ''.join(parts)
    return str(content)


def _extract_response_content(response, n):
    contents = []
    for choice in response.choices:
        message = getattr(choice, 'message', None)
        contents.append(_content_to_text(getattr(message, 'content', '')))
    if n > 1:
        return contents
    return contents[0] if contents else ''


def stream_chat_with_gpt(
    messages,
    model='gpt-3.5-turbo-1106',
    response_json=False,
    api_key=None,
    base_url=None,
    max_tokens=4_096,
    n=1,
    proxies=None,
    reasoning_effort=None,
    temperature=None,
    top_p=None,
    timeout=None,
    max_retries=None,
):
    request_messages = _prepare_request_messages(messages, model)
    result_messages = ChatMessages([dict(message) for message in request_messages], model=model)
    result_messages.append({'role': 'assistant', 'content': ['' for _ in range(n)] if n > 1 else ''})

    request_kwargs = _build_request_kwargs(
        request_messages,
        model,
        max_tokens,
        n,
        response_json,
        reasoning_effort,
        temperature,
        top_p,
        True,
    )

    stream_client = _build_openai_client(
        api_key=api_key,
        base_url=base_url,
        proxies=proxies,
        timeout=timeout,
        max_retries=max_retries,
    )

    content = ['' for _ in range(n)]
    try:
        try:
            chatstream = stream_client.chat.completions.create(**request_kwargs)
            for part in chatstream:
                for choice in part.choices:
                    delta = _content_to_text(getattr(choice.delta, 'content', ''))
                    if not delta:
                        continue
                    content[choice.index] += delta
                    result_messages[-1]['content'] = content if n > 1 else content[0]
                    yield result_messages
            return result_messages
        except Exception:
            fallback_client = _build_openai_client(
                api_key=api_key,
                base_url=base_url,
                proxies=proxies,
                timeout=timeout,
                max_retries=max_retries,
            )
            try:
                fallback_kwargs = _build_request_kwargs(
                    request_messages,
                    model,
                    max_tokens,
                    n,
                    response_json,
                    reasoning_effort,
                    temperature,
                    top_p,
                    False,
                )
                response = fallback_client.chat.completions.create(**fallback_kwargs)
                result_messages[-1]['content'] = _extract_response_content(response, n)
                yield result_messages
                return result_messages
            finally:
                fallback_client.close()
    finally:
        stream_client.close()


if __name__ == '__main__':
    pass
