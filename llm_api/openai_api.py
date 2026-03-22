import os
import re
from urllib.parse import urlparse, urlunparse

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


def _read_positive_float_env(name):
    value = os.getenv(name, '').strip()
    if not value:
        return None
    parsed = float(value)
    if parsed <= 0:
        return None
    return parsed


def _read_non_empty_env(name):
    value = os.getenv(name, '').strip()
    return value or None


def _normalize_base_url(base_url):
    if not base_url:
        return base_url
    normalized = str(base_url).strip().rstrip('/')
    parsed = urlparse(normalized)
    if parsed.scheme and parsed.netloc and parsed.path in ('', '/'):
        return urlunparse(parsed._replace(path='/v1'))
    return normalized


def _exception_chain_text(exc):
    parts = []
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).strip()
        if text:
            parts.append(text)
        current = getattr(current, '__cause__', None) or getattr(current, '__context__', None)
    return ' | '.join(parts)


def _normalize_provider_error(exc):
    combined = _exception_chain_text(exc)
    lowered = combined.lower()
    if (
        'error code: 524' in lowered
        or 'error 524' in lowered
        or 'origin web server timed out responding to this request' in lowered
        or ('cloudflare' in lowered and 'timed out' in lowered)
        or ('cf-ray' in lowered and '<html' in lowered)
    ):
        return 'cloudflare_524_timeout'

    if '<html' in lowered and 'cloudflare' in lowered:
        return 'cloudflare_gateway_html_error'

    if re.search(r'<html[\s>]', lowered) and re.search(r'</html>', lowered):
        return 'unexpected_html_gateway_response'

    return None


def _build_timeout(timeout=None, streaming=False):
    if isinstance(timeout, (int, float)):
        timeout = float(timeout)
        stream_read_timeout = _read_positive_float_env('GPT_STREAM_READ_TIMEOUT_SECONDS') or 120.0
        read_timeout = min(timeout, stream_read_timeout) if streaming else timeout
        return httpx.Timeout(
            connect=min(timeout, 60.0),
            read=read_timeout,
            write=min(timeout, 120.0),
            pool=min(timeout, 120.0),
        )
    return timeout


def _build_openai_client(api_key=None, base_url=None, proxies=None, timeout=None, max_retries=None, streaming=False):
    if api_key is None:
        raise Exception('?????? api_key?')

    client_params = {
        'api_key': api_key,
    }

    if base_url:
        client_params['base_url'] = _normalize_base_url(base_url)

    if max_retries is not None:
        client_params['max_retries'] = max_retries

    default_headers = {}
    project_tag = _read_non_empty_env('LONG_NOVEL_PROJECT_TAG')
    if project_tag:
        default_headers['X-Long-Novel-Project'] = project_tag
        default_headers['X-Long-Novel-Client'] = 'long-novel-gpt'
    user_agent = _read_non_empty_env('LONG_NOVEL_USER_AGENT')
    if user_agent:
        default_headers['User-Agent'] = user_agent
    if default_headers:
        client_params['default_headers'] = default_headers

    httpx_kwargs = {
        'http2': False,
        'limits': httpx.Limits(max_connections=1, max_keepalive_connections=0),
        'headers': {
            'Connection': 'close',
            'Accept-Encoding': 'identity',
        },
    }

    if proxies:
        httpx_kwargs['proxy'] = proxies

    resolved_timeout = _build_timeout(timeout, streaming=streaming)
    if resolved_timeout:
        httpx_kwargs['timeout'] = resolved_timeout

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


def _should_use_responses_api(model, response_json, n):
    return model.startswith('gpt-5') and not response_json and n == 1


def _should_bypass_responses_stream(reasoning_effort, max_output_tokens):
    force_stream = os.getenv('GPT_FORCE_RESPONSES_STREAM', '').strip().lower() in ('1', 'true', 'yes', 'on')
    if force_stream:
        return False
    effort = str(reasoning_effort or '').strip().lower()
    return effort == 'xhigh' and int(max_output_tokens or 0) >= 12_000


def _prepare_responses_input(messages):
    instructions = []
    input_items = []

    for message in messages:
        role = message.get('role', 'user')
        content = _content_to_text(message.get('content', ''))
        if not content:
            continue
        if role == 'system':
            instructions.append(content)
            continue
        input_items.append({'role': role, 'content': content})

    return '\n\n'.join(instructions).strip(), input_items


def _build_request_kwargs(messages, model, max_output_tokens, n, response_json, reasoning_effort, temperature, top_p, stream):
    request_kwargs = {
        'model': model,
        'messages': messages,
        'max_tokens': max_output_tokens,
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


def _build_responses_request_kwargs(messages, model, max_output_tokens, reasoning_effort, temperature, top_p):
    instructions, input_items = _prepare_responses_input(messages)

    request_kwargs = {
        'model': model,
        'input': input_items or '',
        'max_output_tokens': max_output_tokens,
        'store': False,
    }

    if instructions:
        request_kwargs['instructions'] = instructions

    if reasoning_effort:
        request_kwargs['reasoning'] = {'effort': reasoning_effort}

    if temperature not in (None, ''):
        request_kwargs['temperature'] = float(temperature)

    if top_p not in (None, ''):
        request_kwargs['top_p'] = float(top_p)

    return request_kwargs


def _extract_responses_output_text(response):
    text = _content_to_text(getattr(response, 'output_text', ''))
    if text:
        return text

    outputs = getattr(response, 'output', None)
    if not outputs:
        return ''

    parts = []
    for item in outputs:
        content_items = getattr(item, 'content', None)
        if content_items is None and isinstance(item, dict):
            content_items = item.get('content')
        if not content_items:
            continue
        parts.append(_content_to_text(content_items))
    return ''.join(parts)


def _is_retryable_responses_stream_error(exc):
    normalized = _normalize_provider_error(exc)
    if normalized in {
        'cloudflare_524_timeout',
        'cloudflare_gateway_html_error',
        'unexpected_html_gateway_response',
    }:
        return True

    current = exc
    seen = set()
    messages = []
    class_names = []
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        class_names.append(current.__class__.__name__.lower())
        messages.append(str(current).lower())
        if isinstance(current, httpx.TransportError):
            return True
        current = getattr(current, '__cause__', None) or getattr(current, '__context__', None)

    message = ' | '.join(messages)
    class_name_text = ' | '.join(class_names)
    retryable_markers = [
        'decryption failed or bad record mac',
        'bad record mac',
        'ssl',
        'stream_read_error',
        'read error',
        'timeout',
        'timed out',
        'connection reset',
        'connection aborted',
        'broken pipe',
        'eof occurred in violation of protocol',
        'apierror',
        'apitimeouterror',
        'apiconnectionerror',
    ]
    return any(
        marker in message or marker in class_name_text
        for marker in retryable_markers
    )


def _create_with_responses_api(
    request_messages,
    result_messages,
    model,
    api_key,
    base_url,
    max_output_tokens,
    proxies,
    reasoning_effort,
    temperature,
    top_p,
    timeout,
    max_retries,
):
    request_kwargs = _build_responses_request_kwargs(
        request_messages,
        model,
        max_output_tokens,
        reasoning_effort,
        temperature,
        top_p,
    )

    response_client = _build_openai_client(
        api_key=api_key,
        base_url=base_url,
        proxies=proxies,
        timeout=timeout,
        max_retries=max_retries,
        streaming=False,
    )

    try:
        response = response_client.responses.create(**request_kwargs)
        final_text = _extract_responses_output_text(response)
        result_messages[-1]['content'] = final_text
        yield result_messages
        return result_messages
    except Exception as exc:
        normalized = _normalize_provider_error(exc)
        if normalized:
            raise RuntimeError(normalized) from None
        raise
    finally:
        response_client.close()


def _stream_chat_with_responses_api(
    request_messages,
    result_messages,
    model,
    api_key,
    base_url,
    max_output_tokens,
    proxies,
    reasoning_effort,
    temperature,
    top_p,
    timeout,
    max_retries,
):
    request_kwargs = _build_responses_request_kwargs(
        request_messages,
        model,
        max_output_tokens,
        reasoning_effort,
        temperature,
        top_p,
    )

    response_client = _build_openai_client(
        api_key=api_key,
        base_url=base_url,
        proxies=proxies,
        timeout=timeout,
        max_retries=max_retries,
        streaming=True,
    )

    content = ''
    try:
        with response_client.responses.stream(**request_kwargs) as stream:
            for event in stream:
                if getattr(event, 'type', '') != 'response.output_text.delta':
                    continue
                delta = _content_to_text(getattr(event, 'delta', ''))
                if not delta:
                    continue
                content += delta
                result_messages[-1]['content'] = content
                yield result_messages

            final_response = stream.get_final_response()
            final_text = getattr(final_response, 'output_text', '') or content
            result_messages[-1]['content'] = final_text
            yield result_messages
            return result_messages
    except Exception as exc:
        normalized = _normalize_provider_error(exc)
        if not _is_retryable_responses_stream_error(exc):
            if normalized:
                raise RuntimeError(normalized) from None
            raise

        result_messages.stream_fallback = True
        result_messages.stream_fallback_reason = normalized or str(exc)

        if content:
            result_messages[-1]['content'] = content
            yield result_messages

        yield from _create_with_responses_api(
            request_messages=request_messages,
            result_messages=result_messages,
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_output_tokens=max_output_tokens,
            proxies=proxies,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            top_p=top_p,
            timeout=timeout,
            max_retries=max_retries,
        )
        return result_messages
    finally:
        response_client.close()


def stream_chat_with_gpt(
    messages,
    model='gpt-3.5-turbo-1106',
    response_json=False,
    api_key=None,
    base_url=None,
    max_tokens=4_096,
    max_input_tokens=None,
    max_output_tokens=None,
    n=1,
    proxies=None,
    reasoning_effort=None,
    temperature=None,
    top_p=None,
    timeout=None,
    max_retries=None,
):
    output_token_limit = int(max_output_tokens or max_tokens)
    request_messages = _prepare_request_messages(messages, model)
    result_messages = ChatMessages([dict(message) for message in request_messages], model=model)
    result_messages.append({'role': 'assistant', 'content': ['' for _ in range(n)] if n > 1 else ''})

    if _should_use_responses_api(model, response_json, n):
        if _should_bypass_responses_stream(reasoning_effort, output_token_limit):
            yield from _create_with_responses_api(
                request_messages=request_messages,
                result_messages=result_messages,
                model=model,
                api_key=api_key,
                base_url=base_url,
                max_output_tokens=output_token_limit,
                proxies=proxies,
                reasoning_effort=reasoning_effort,
                temperature=temperature,
                top_p=top_p,
                timeout=timeout,
                max_retries=max_retries,
            )
        else:
            yield from _stream_chat_with_responses_api(
                request_messages=request_messages,
                result_messages=result_messages,
                model=model,
                api_key=api_key,
                base_url=base_url,
                max_output_tokens=output_token_limit,
                proxies=proxies,
                reasoning_effort=reasoning_effort,
                temperature=temperature,
                top_p=top_p,
                timeout=timeout,
                max_retries=max_retries,
            )
        return result_messages

    request_kwargs = _build_request_kwargs(
        request_messages,
        model,
        output_token_limit,
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
        streaming=True,
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
                streaming=False,
            )
            try:
                fallback_kwargs = _build_request_kwargs(
                    request_messages,
                    model,
                    output_token_limit,
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
