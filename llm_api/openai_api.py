import os
import re
import json
import time
import math
import hashlib
import threading
from datetime import datetime, timedelta
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


_API_CHAIN_STATE = {}
_API_CHAIN_STATE_LOCK = threading.Lock()
_PROMPT_CORRUPTION_PATTERNS = (
    re.compile(r"your message (?:got )?garbled", re.IGNORECASE),
    re.compile(r"(?:string|bunch) of question marks", re.IGNORECASE),
    re.compile(r"could(?:n['’]t| not) make sense of your message", re.IGNORECASE),
    re.compile(r"unable to (?:recognize|understand).{0,80}(?:message|question)", re.IGNORECASE),
    re.compile(r"无法理解您?发出的消息"),
    re.compile(r"无法识别这段内容"),
    re.compile(r"消息.*(?:问号|乱码)"),
    re.compile(r"请重新发送(?:你的)?问题"),
)


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


def _read_positive_int_env(name):
    value = os.getenv(name, '').strip()
    if not value:
        return None
    parsed = int(value)
    if parsed <= 0:
        return None
    return parsed


def _read_bool_env(name):
    value = os.getenv(name, '').strip().lower()
    if not value:
        return None
    if value in ('1', 'true', 'yes', 'on'):
        return True
    if value in ('0', 'false', 'no', 'off'):
        return False
    return None


def _normalize_base_url(base_url):
    if not base_url:
        return base_url
    normalized = str(base_url).strip().rstrip('/')
    parsed = urlparse(normalized)
    if parsed.scheme and parsed.netloc and parsed.path in ('', '/'):
        return urlunparse(parsed._replace(path='/v1'))
    return normalized


def _derive_chain_entry_name(entry, index):
    explicit_name = str(entry.get('name', '')).strip()
    if explicit_name:
        return explicit_name

    parsed = urlparse(str(entry.get('base_url', '') or '').strip())
    if parsed.netloc:
        return parsed.netloc
    return f'endpoint_{index + 1}'


def _normalize_api_chain(api_chain, api_key=None, base_url=None, proxies=None):
    normalized_chain = []
    if isinstance(api_chain, (list, tuple)):
        for index, entry in enumerate(api_chain):
            if not isinstance(entry, dict):
                continue
            entry_api_key = str(entry.get('api_key') or '').strip()
            if not entry_api_key:
                continue

            entry_base_url = entry.get('base_url') or entry.get('endpoint') or entry.get('url') or base_url
            normalized_chain.append({
                'name': _derive_chain_entry_name(entry, index),
                'api_key': entry_api_key,
                'base_url': _normalize_base_url(entry_base_url),
                'proxies': str(entry.get('proxies') or proxies or '').strip(),
                'cooldown_overrides': entry.get('cooldown_overrides') if isinstance(entry.get('cooldown_overrides'), dict) else None,
            })

    if normalized_chain:
        return normalized_chain

    if api_key is None:
        return []

    return [{
        'name': 'default',
        'api_key': api_key,
        'base_url': _normalize_base_url(base_url),
        'proxies': str(proxies or '').strip(),
        'cooldown_overrides': None,
    }]


def _api_chain_signature(model, api_chain):
    payload = []
    for entry in api_chain:
        payload.append({
            'name': str(entry.get('name', '')).strip(),
            'base_url': _normalize_base_url(entry.get('base_url')),
            'api_key_hash': hashlib.sha1(str(entry.get('api_key', '')).encode('utf-8')).hexdigest(),
            'cooldown_overrides': entry.get('cooldown_overrides') if isinstance(entry.get('cooldown_overrides'), dict) else None,
        })
    return json.dumps({'model': model, 'chain': payload}, ensure_ascii=False, sort_keys=True)


def _parse_rollover_time(value):
    text = str(value or '').strip()
    if not text:
        return 0, 0
    parts = text.split(':', 1)
    if len(parts) != 2:
        return 0, 0
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return 0, 0
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return 0, 0
    return hour, minute


def _current_rollover_marker(reset_time):
    hour, minute = _parse_rollover_time(reset_time)
    now = datetime.now()
    boundary = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < boundary:
        boundary -= timedelta(days=1)
    return boundary.strftime('%Y-%m-%d %H:%M')


def _get_api_chain_start_index(signature, api_chain, reset_time):
    marker = _current_rollover_marker(reset_time)
    with _API_CHAIN_STATE_LOCK:
        state = _API_CHAIN_STATE.setdefault(signature, {
            'active_index': 0,
            'rollover_marker': marker,
            'cooldowns': {},
        })
        if state.get('rollover_marker') != marker:
            state['active_index'] = 0
            state['rollover_marker'] = marker
            state['cooldowns'] = {}
        if not isinstance(state.get('cooldowns'), dict):
            state['cooldowns'] = {}
        active_index = int(state.get('active_index', 0) or 0)
    if not api_chain:
        return 0, marker
    return max(0, min(active_index, len(api_chain) - 1)), marker


def _build_api_chain_attempt_plan(active_index, chain_length, rate_limit_retries):
    if chain_length <= 0:
        return []

    normalized_active_index = max(0, min(int(active_index or 0), chain_length - 1))
    normalized_rate_limit_retries = max(0, int(rate_limit_retries or 0))
    plan = []

    if normalized_active_index > 0:
        # When already running on a lower-priority fallback endpoint, probe higher-priority
        # endpoints once per new request so recovery switches back immediately without
        # blocking the active fallback behind repeated 429 retries.
        for endpoint_index in range(0, normalized_active_index):
            plan.append({
                'endpoint_index': endpoint_index,
                'rate_limit_retries': 0,
                'mode': 'higher_priority_probe',
            })

    plan.append({
        'endpoint_index': normalized_active_index,
        'rate_limit_retries': normalized_rate_limit_retries,
        'mode': 'active',
    })

    for endpoint_index in range(normalized_active_index + 1, chain_length):
        plan.append({
            'endpoint_index': endpoint_index,
            'rate_limit_retries': normalized_rate_limit_retries,
            'mode': 'lower_priority_fallback',
        })

    return plan


def _set_api_chain_active_index(signature, index, reset_time, marker=None):
    with _API_CHAIN_STATE_LOCK:
        state = _API_CHAIN_STATE.setdefault(signature, {
            'active_index': 0,
            'rollover_marker': _current_rollover_marker(reset_time),
            'cooldowns': {},
        })
        state['active_index'] = max(0, int(index or 0))
        state['rollover_marker'] = marker or _current_rollover_marker(reset_time)
        if not isinstance(state.get('cooldowns'), dict):
            state['cooldowns'] = {}


def _get_api_chain_endpoint_cooldown(signature, endpoint_index, reset_time, marker=None):
    resolved_marker = marker or _current_rollover_marker(reset_time)
    now = time.time()
    with _API_CHAIN_STATE_LOCK:
        state = _API_CHAIN_STATE.setdefault(signature, {
            'active_index': 0,
            'rollover_marker': resolved_marker,
            'cooldowns': {},
        })
        if state.get('rollover_marker') != resolved_marker:
            state['active_index'] = 0
            state['rollover_marker'] = resolved_marker
            state['cooldowns'] = {}
            return None
        if not isinstance(state.get('cooldowns'), dict):
            state['cooldowns'] = {}
        info = state['cooldowns'].get(int(endpoint_index))
        if not info:
            return None
        until = float(info.get('until', 0) or 0)
        if until <= now:
            state['cooldowns'].pop(int(endpoint_index), None)
            return None
        return {
            'until': until,
            'category': str(info.get('category', '') or '').strip(),
            'detail': str(info.get('detail', '') or '').strip(),
        }


def _set_api_chain_endpoint_cooldown(signature, endpoint_index, reset_time, cooldown_seconds, category='', detail='', marker=None):
    duration = max(0.0, float(cooldown_seconds or 0))
    if duration <= 0:
        return
    resolved_marker = marker or _current_rollover_marker(reset_time)
    with _API_CHAIN_STATE_LOCK:
        state = _API_CHAIN_STATE.setdefault(signature, {
            'active_index': 0,
            'rollover_marker': resolved_marker,
            'cooldowns': {},
        })
        if state.get('rollover_marker') != resolved_marker:
            state['active_index'] = 0
            state['rollover_marker'] = resolved_marker
            state['cooldowns'] = {}
        if not isinstance(state.get('cooldowns'), dict):
            state['cooldowns'] = {}
        state['cooldowns'][int(endpoint_index)] = {
            'until': time.time() + duration,
            'category': str(category or '').strip(),
            'detail': str(detail or '').strip(),
        }


def _get_api_chain_next_retry_after_seconds(signature, reset_time, marker=None):
    resolved_marker = marker or _current_rollover_marker(reset_time)
    now = time.time()
    with _API_CHAIN_STATE_LOCK:
        state = _API_CHAIN_STATE.get(signature)
        if not state:
            return None
        if state.get('rollover_marker') != resolved_marker:
            return None
        cooldowns = state.get('cooldowns')
        if not isinstance(cooldowns, dict) or not cooldowns:
            return None
        future_untils = []
        expired_keys = []
        for key, info in cooldowns.items():
            try:
                until = float((info or {}).get('until', 0) or 0)
            except Exception:
                until = 0
            if until <= now:
                expired_keys.append(key)
            else:
                future_untils.append(until)
        for key in expired_keys:
            cooldowns.pop(key, None)
        if not future_untils:
            return None
        return max(0.0, min(future_untils) - now)


def _clear_api_chain_endpoint_cooldown(signature, endpoint_index, reset_time, marker=None):
    resolved_marker = marker or _current_rollover_marker(reset_time)
    with _API_CHAIN_STATE_LOCK:
        state = _API_CHAIN_STATE.setdefault(signature, {
            'active_index': 0,
            'rollover_marker': resolved_marker,
            'cooldowns': {},
        })
        if state.get('rollover_marker') != resolved_marker:
            state['active_index'] = 0
            state['rollover_marker'] = resolved_marker
            state['cooldowns'] = {}
            return
        if not isinstance(state.get('cooldowns'), dict):
            state['cooldowns'] = {}
        state['cooldowns'].pop(int(endpoint_index), None)


def _extract_status_code(exc):
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        direct_status = getattr(current, 'status_code', None)
        if isinstance(direct_status, int):
            return direct_status
        response = getattr(current, 'response', None)
        response_status = getattr(response, 'status_code', None)
        if isinstance(response_status, int):
            return response_status
        body = getattr(current, 'body', None)
        if isinstance(body, dict):
            for key in ('status', 'status_code', 'code'):
                value = body.get(key)
                if isinstance(value, int):
                    return value
                if isinstance(value, str) and value.isdigit():
                    return int(value)
        current = getattr(current, '__cause__', None) or getattr(current, '__context__', None)

    match = re.search(r'error code:\s*(\d{3})', _exception_chain_text(exc), re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def _classify_api_chain_error(exc):
    status_code = _extract_status_code(exc)
    lowered = _exception_chain_text(exc).lower()
    quota_markers = (
        'usage_limit_exceeded',
        'insufficient_quota',
        'api_key_quota_exhausted',
        'quota_exhausted',
        'quota exceeded',
        'quota_exceeded',
        'credit balance',
        'balance exhausted',
        '余额已用尽',
        '余额用尽',
        '订阅余额已用尽',
        'subscription balance has been exhausted',
    )

    if any(marker in lowered for marker in quota_markers):
        return 'quota_exhausted'

    gateway_markers = (
        'cloudflare_524_timeout',
        'cloudflare_gateway_html_error',
        'unexpected_html_gateway_response',
    )
    for marker in gateway_markers:
        if marker in lowered:
            return marker

    normalized = _normalize_provider_error(exc)
    if normalized in gateway_markers:
        return normalized

    if (
        status_code == 401
        or 'invalid_api_key' in lowered
        or 'unauthorized' in lowered
        or 'authentication' in lowered
    ):
        return 'auth'

    if (
        status_code == 429
        or 'rate limit' in lowered
        or 'too many requests' in lowered
    ):
        return 'rate_limit'

    if status_code is not None and 400 <= status_code < 500:
        return 'client_error'

    if status_code in {500, 502, 503, 504, 520, 522, 524}:
        return 'server_error'

    if normalized:
        return normalized

    if _is_retryable_responses_stream_error(exc):
        return 'transport'

    return 'other'


def _resolve_rate_limit_retry_count(value):
    if value not in (None, ''):
        return max(0, int(value))
    value = _read_positive_int_env('GPT_API_CHAIN_RATE_LIMIT_RETRIES')
    return value if value is not None else 0


def _resolve_api_chain_cooldown_seconds(category, endpoint=None):
    endpoint = endpoint if isinstance(endpoint, dict) else {}
    endpoint_overrides = endpoint.get('cooldown_overrides')
    if isinstance(endpoint_overrides, dict):
        override_value = endpoint_overrides.get(category)
        if override_value not in (None, ''):
            try:
                return max(0.0, float(override_value))
            except Exception:
                pass

    env_names = {
        'auth': 'GPT_API_CHAIN_AUTH_COOLDOWN_SECONDS',
        'client_error': 'GPT_API_CHAIN_CLIENT_ERROR_COOLDOWN_SECONDS',
        'quota_exhausted': 'GPT_API_CHAIN_QUOTA_COOLDOWN_SECONDS',
        'rate_limit': 'GPT_API_CHAIN_RATE_LIMIT_COOLDOWN_SECONDS',
        'server_error': 'GPT_API_CHAIN_SERVER_ERROR_COOLDOWN_SECONDS',
        'transport': 'GPT_API_CHAIN_TRANSPORT_COOLDOWN_SECONDS',
        'provider_prompt_corrupted': 'GPT_API_CHAIN_PROMPT_CORRUPTION_COOLDOWN_SECONDS',
        'cloudflare_524_timeout': 'GPT_API_CHAIN_GATEWAY_COOLDOWN_SECONDS',
        'cloudflare_gateway_html_error': 'GPT_API_CHAIN_GATEWAY_COOLDOWN_SECONDS',
        'unexpected_html_gateway_response': 'GPT_API_CHAIN_GATEWAY_COOLDOWN_SECONDS',
    }
    default_values = {
        # Auth and gateway/client blocks usually require operator action or a provider reset.
        # Do not hammer the same bad endpoint on every outer retry.
        'auth': 86400,
        'client_error': 3600,
        'quota_exhausted': 3600,
        'rate_limit': 3600,
        'server_error': 120,
        'transport': 60,
        'provider_prompt_corrupted': 300,
        'cloudflare_524_timeout': 3600,
        'cloudflare_gateway_html_error': 3600,
        'unexpected_html_gateway_response': 3600,
    }
    env_name = env_names.get(category)
    if env_name:
        raw_value = os.getenv(env_name, '').strip()
        if raw_value:
            return max(0.0, float(raw_value))
    return float(default_values.get(category, 0))


def _rate_limit_backoff_seconds(attempt):
    base_delay = _read_positive_float_env('GPT_API_CHAIN_RATE_LIMIT_BACKOFF_SECONDS') or 3.0
    return min(30.0, base_delay * attempt)


def _format_api_chain_errors(errors):
    parts = []
    for item in errors:
        endpoint_name = item.get('endpoint_name', 'unknown')
        category = item.get('category', 'other')
        detail = str(item.get('detail', '') or '').strip()
        if detail:
            parts.append(f'{endpoint_name} [{category}]: {detail}')
        else:
            parts.append(f'{endpoint_name} [{category}]')
    return ' | '.join(parts)


def _apply_api_chain_metadata(messages, endpoint_name, endpoint_base_url):
    messages.api_endpoint_name = endpoint_name
    messages.api_endpoint_base_url = endpoint_base_url
    return messages


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

    if 'provider_prompt_corrupted' in lowered:
        return 'provider_prompt_corrupted'

    return None


def _looks_like_prompt_corruption_response(text):
    candidate = str(text or "").strip()
    if not candidate or len(candidate) > 1_200:
        return False
    return any(pattern.search(candidate) for pattern in _PROMPT_CORRUPTION_PATTERNS)


def _ensure_valid_model_response(text):
    if _looks_like_prompt_corruption_response(text):
        raise RuntimeError("provider_prompt_corrupted: gateway returned a prompt-garbled refusal")
    return text


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


def _is_official_openai_base_url(base_url):
    normalized = _normalize_base_url(base_url)
    if not normalized:
        return True

    parsed = urlparse(normalized)
    host = parsed.netloc.lower()
    if not host:
        return True

    return host == 'api.openai.com' or host.endswith('.openai.com')


def _should_use_responses_api(model, response_json, n, base_url=None):
    if not model.startswith('gpt-5') or response_json or n != 1:
        return False

    force_responses = _read_bool_env('GPT_FORCE_RESPONSES_API')
    if force_responses is True:
        return True

    disable_responses = _read_bool_env('GPT_DISABLE_RESPONSES_API')
    if disable_responses is True:
        return False

    # Third-party OpenAI-compatible gateways are much more stable on
    # chat.completions than on the newer responses wire format.
    return _is_official_openai_base_url(base_url)


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
        'jsondecodeerror',
        'expecting value: line 1 column 1',
    ]
    return any(
        marker in message or marker in class_name_text
        for marker in retryable_markers
    )


def _should_fallback_from_responses_error(exc, base_url):
    if _normalize_provider_error(exc):
        return True

    if _is_retryable_responses_stream_error(exc):
        return True

    combined = _exception_chain_text(exc).lower()
    fallback_markers = [
        'jsondecodeerror',
        'expecting value: line 1 column 1',
        'response ended prematurely',
        'remoteprotocolerror',
        'sse',
        '/responses',
        'responses api',
        'unsupported',
        'not found',
    ]
    if any(marker in combined for marker in fallback_markers):
        return True

    return not _is_official_openai_base_url(base_url)


def _create_with_chat_completions_api(
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
    response_json=False,
    n=1,
):
    request_kwargs = _build_request_kwargs(
        request_messages,
        model,
        max_output_tokens,
        n,
        response_json,
        reasoning_effort,
        temperature,
        top_p,
        False,
    )

    completion_client = _build_openai_client(
        api_key=api_key,
        base_url=base_url,
        proxies=proxies,
        timeout=timeout,
        max_retries=max_retries,
        streaming=False,
    )

    try:
        response = completion_client.chat.completions.create(**request_kwargs)
        content = _extract_response_content(response, n)
        if n == 1:
            _ensure_valid_model_response(content)
        elif isinstance(content, list):
            for item in content:
                _ensure_valid_model_response(item)
        result_messages[-1]['content'] = content
        yield result_messages
        return result_messages
    finally:
        completion_client.close()


def _stream_chat_with_chat_completions_api(
    request_messages,
    result_messages,
    model,
    api_key,
    base_url,
    max_output_tokens,
    n,
    response_json,
    proxies,
    reasoning_effort,
    temperature,
    top_p,
    timeout,
    max_retries,
):
    request_kwargs = _build_request_kwargs(
        request_messages,
        model,
        max_output_tokens,
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
            if any((item or '').strip() for item in content):
                for item in content:
                    _ensure_valid_model_response(item)
                return result_messages
            return result_messages
        except Exception:
            pass

        yield from _create_with_chat_completions_api(
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
            response_json=response_json,
            n=n,
        )
        return result_messages
    finally:
        stream_client.close()


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
        _ensure_valid_model_response(final_text)
        result_messages[-1]['content'] = final_text
        yield result_messages
        return result_messages
    except Exception as exc:
        if _should_fallback_from_responses_error(exc, base_url):
            result_messages.responses_fallback = True
            result_messages.responses_fallback_reason = _normalize_provider_error(exc) or str(exc)
            yield from _create_with_chat_completions_api(
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
                response_json=False,
                n=1,
            )
            return result_messages

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
            _ensure_valid_model_response(final_text)
            result_messages[-1]['content'] = final_text
            yield result_messages
            return result_messages
    except Exception as exc:
        normalized = _normalize_provider_error(exc)
        if _should_fallback_from_responses_error(exc, base_url):
            result_messages.stream_fallback = True
            result_messages.stream_fallback_reason = normalized or str(exc)

            if content:
                result_messages[-1]['content'] = content
                yield result_messages

            yield from _create_with_chat_completions_api(
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
                response_json=False,
                n=1,
            )
            return result_messages

        if normalized:
            raise RuntimeError(normalized) from None
        raise
    finally:
        response_client.close()


def _stream_chat_with_gpt_single_endpoint(
    messages,
    model='gpt-3.5-turbo-1106',
    response_json=False,
    endpoint_name=None,
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
    _apply_api_chain_metadata(
        result_messages,
        endpoint_name=str(endpoint_name or '').strip() or 'default',
        endpoint_base_url=str(base_url or '').strip(),
    )
    yield result_messages

    if _should_use_responses_api(model, response_json, n, base_url=base_url):
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

    yield from _stream_chat_with_chat_completions_api(
        request_messages=request_messages,
        result_messages=result_messages,
        model=model,
        api_key=api_key,
        base_url=base_url,
        max_output_tokens=output_token_limit,
        n=n,
        response_json=response_json,
        proxies=proxies,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        top_p=top_p,
        timeout=timeout,
        max_retries=max_retries,
    )
    return result_messages


def _stream_chat_with_gpt_via_api_chain(
    messages,
    model='gpt-3.5-turbo-1106',
    response_json=False,
    api_chain=None,
    api_chain_reset_time=None,
    api_chain_rate_limit_retries=None,
    max_tokens=4_096,
    max_input_tokens=None,
    max_output_tokens=None,
    n=1,
    reasoning_effort=None,
    temperature=None,
    top_p=None,
    timeout=None,
    max_retries=None,
):
    normalized_chain = _normalize_api_chain(api_chain)
    if not normalized_chain:
        raise Exception('?????? api_key?')

    chain_signature = _api_chain_signature(model, normalized_chain)
    start_index, request_marker = _get_api_chain_start_index(
        chain_signature,
        normalized_chain,
        api_chain_reset_time,
    )
    rate_limit_retries = _resolve_rate_limit_retry_count(api_chain_rate_limit_retries)
    attempt_plan = _build_api_chain_attempt_plan(
        start_index,
        len(normalized_chain),
        rate_limit_retries,
    )
    effective_max_retries = 0 if len(normalized_chain) > 1 else max_retries
    errors = []

    for attempt in attempt_plan:
        endpoint_index = int(attempt.get('endpoint_index', 0) or 0)
        endpoint = normalized_chain[endpoint_index]
        cooldown_info = _get_api_chain_endpoint_cooldown(
            chain_signature,
            endpoint_index,
            api_chain_reset_time,
            marker=request_marker,
        )
        # A higher-priority probe is deliberately one request even while that
        # endpoint is cooling down. This lets a recovered provider take over
        # immediately; its rate-limit retry budget remains zero below.
        endpoint_overrides = endpoint.get('cooldown_overrides')
        probe_rate_limit = (
            attempt.get('mode') == 'higher_priority_probe'
            and cooldown_info.get('category') == 'rate_limit'
            and not (
                isinstance(endpoint_overrides, dict)
                and endpoint_overrides.get('rate_limit') not in (None, '')
            )
        ) if cooldown_info else False
        if cooldown_info and not probe_rate_limit:
            errors.append({
                'endpoint_name': str(endpoint.get('name', '')).strip() or f'endpoint_{endpoint_index + 1}',
                'category': f"cooldown/{cooldown_info.get('category') or 'hold'}",
                'detail': f"skip until {datetime.fromtimestamp(cooldown_info['until']).strftime('%Y-%m-%d %H:%M:%S')}",
            })
            continue
        endpoint_rate_limit_retries = max(0, int(attempt.get('rate_limit_retries', 0) or 0))
        total_attempts = 1 + endpoint_rate_limit_retries
        for endpoint_attempt in range(1, total_attempts + 1):
            try:
                result = yield from _stream_chat_with_gpt_single_endpoint(
                    messages,
                    model=model,
                    response_json=response_json,
                    endpoint_name=str(endpoint.get('name', '')).strip(),
                    api_key=endpoint.get('api_key'),
                    base_url=endpoint.get('base_url'),
                    max_tokens=max_tokens,
                    max_input_tokens=max_input_tokens,
                    max_output_tokens=max_output_tokens,
                    n=n,
                    proxies=endpoint.get('proxies'),
                    reasoning_effort=reasoning_effort,
                    temperature=temperature,
                    top_p=top_p,
                    timeout=timeout,
                    max_retries=effective_max_retries,
                )
                _clear_api_chain_endpoint_cooldown(
                    chain_signature,
                    endpoint_index,
                    api_chain_reset_time,
                    marker=request_marker,
                )
                _set_api_chain_active_index(
                    chain_signature,
                    endpoint_index,
                    api_chain_reset_time,
                    marker=request_marker,
                )
                _apply_api_chain_metadata(
                    result,
                    endpoint_name=str(endpoint.get('name', '')).strip(),
                    endpoint_base_url=str(endpoint.get('base_url', '')).strip(),
                )
                return result
            except Exception as exc:
                category = _classify_api_chain_error(exc)
                detail = _normalize_provider_error(exc) or str(exc)

                if category == 'rate_limit' and endpoint_attempt <= endpoint_rate_limit_retries:
                    time.sleep(_rate_limit_backoff_seconds(endpoint_attempt))
                    continue

                cooldown_seconds = _resolve_api_chain_cooldown_seconds(category, endpoint=endpoint)
                if cooldown_seconds > 0:
                    _set_api_chain_endpoint_cooldown(
                        chain_signature,
                        endpoint_index,
                        api_chain_reset_time,
                        cooldown_seconds,
                        category=category,
                        detail=detail,
                        marker=request_marker,
                    )

                errors.append({
                    'endpoint_name': str(endpoint.get('name', '')).strip() or f'endpoint_{endpoint_index + 1}',
                    'category': category,
                    'detail': detail,
                })
                break

    _set_api_chain_active_index(chain_signature, 0, api_chain_reset_time, marker=request_marker)
    next_retry_after_seconds = _get_api_chain_next_retry_after_seconds(
        chain_signature,
        api_chain_reset_time,
        marker=request_marker,
    )
    retry_hint = ''
    if next_retry_after_seconds is not None:
        retry_hint = f' earliest_retry_after={int(max(1, math.ceil(next_retry_after_seconds)))}s.'
    raise RuntimeError(
        'all configured API endpoints failed; next retry will restart from the highest priority endpoint. '
        + _format_api_chain_errors(errors)
        + retry_hint
    )


def stream_chat_with_gpt(
    messages,
    model='gpt-3.5-turbo-1106',
    response_json=False,
    api_key=None,
    base_url=None,
    api_chain=None,
    api_chain_reset_time=None,
    api_chain_rate_limit_retries=None,
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
    normalized_chain = _normalize_api_chain(api_chain, api_key=api_key, base_url=base_url, proxies=proxies)
    if normalized_chain and api_chain is not None:
        result = yield from _stream_chat_with_gpt_via_api_chain(
            messages,
            model=model,
            response_json=response_json,
            api_chain=normalized_chain,
            api_chain_reset_time=api_chain_reset_time,
            api_chain_rate_limit_retries=api_chain_rate_limit_retries,
            max_tokens=max_tokens,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            n=n,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            top_p=top_p,
            timeout=timeout,
            max_retries=max_retries,
        )
        return result

    if len(normalized_chain) <= 1:
        endpoint = normalized_chain[0] if normalized_chain else {
            'api_key': api_key,
            'base_url': base_url,
            'proxies': proxies,
        }
        result = yield from _stream_chat_with_gpt_single_endpoint(
            messages,
            model=model,
            response_json=response_json,
            endpoint_name=str(endpoint.get('name', '')).strip() or 'default',
            api_key=endpoint.get('api_key'),
            base_url=endpoint.get('base_url'),
            max_tokens=max_tokens,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            n=n,
            proxies=endpoint.get('proxies'),
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            top_p=top_p,
            timeout=timeout,
            max_retries=max_retries,
        )
        _apply_api_chain_metadata(
            result,
            endpoint_name=str(endpoint.get('name', '')).strip() or 'default',
            endpoint_base_url=str(endpoint.get('base_url', '')).strip(),
        )
        return result


if __name__ == '__main__':
    pass
