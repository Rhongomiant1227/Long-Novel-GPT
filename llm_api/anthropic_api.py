import json
from urllib.parse import urlparse, urlunparse

import httpx

from .chat_messages import ChatMessages


anthropic_model_config = {
    "claude-opus-4-8": {
        "Pricing": (0, 0),
        "currency_symbol": "$",
    },
}


def _normalize_base_url(base_url):
    text = str(base_url or "https://api.anthropic.com/v1").strip().rstrip("/")
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc and parsed.path in ("", "/"):
        return urlunparse(parsed._replace(path="/v1"))
    return text


def _content_to_text(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif "content" in item:
                    parts.append(str(item.get("content", "")))
                elif "text" in item:
                    parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


def _prepare_anthropic_messages(messages):
    system_parts = []
    request_messages = []
    for message in messages:
        role = str(message.get("role", "user") or "user").strip()
        content = _content_to_text(message.get("content", ""))
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        if request_messages and request_messages[-1]["role"] == role:
            request_messages[-1]["content"] += "\n\n" + content
        else:
            request_messages.append({"role": role, "content": content})
    return "\n\n".join(system_parts).strip(), request_messages


def _extract_content_text(content_items):
    parts = []
    for item in content_items or []:
        if isinstance(item, dict):
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            continue
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(str(text))
    return "".join(parts)


def _build_request_payload(messages, model, max_tokens, response_json, temperature, top_p):
    system_text, request_messages = _prepare_anthropic_messages(messages)
    payload = {
        "model": model,
        "max_tokens": int(max_tokens),
        "messages": request_messages or [{"role": "user", "content": ""}],
    }
    if system_text:
        payload["system"] = system_text
    if temperature not in (None, ""):
        payload["temperature"] = float(temperature)
    if top_p not in (None, ""):
        payload["top_p"] = float(top_p)
    if response_json:
        payload["system"] = (
            (payload.get("system", "") + "\n\n" if payload.get("system") else "")
            + "Return only valid JSON. Do not wrap the response in markdown fences."
        )
    return payload


def _build_headers(api_key, extra_headers=None):
    headers = {
        "x-api-key": str(api_key or "").strip(),
        "Authorization": f"Bearer {str(api_key or '').strip()}",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "accept": "application/json",
    }
    if isinstance(extra_headers, dict):
        headers.update({str(key): str(value) for key, value in extra_headers.items()})
    return headers


def _raise_for_error(response):
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = response.text
        try:
            payload = response.json()
            detail = json.dumps(payload, ensure_ascii=False)
        except Exception:
            pass
        raise RuntimeError(f"Anthropic API error {response.status_code}: {detail}") from exc


def stream_chat_with_anthropic(
    messages,
    model="claude-opus-4-8",
    response_json=False,
    api_key=None,
    base_url=None,
    max_tokens=4096,
    proxies=None,
    temperature=None,
    top_p=None,
    timeout=None,
    extra_headers=None,
):
    if api_key is None or not str(api_key).strip():
        raise Exception("Anthropic missing api_key")

    request_payload = _build_request_payload(
        messages,
        model,
        max_tokens,
        response_json,
        temperature,
        top_p,
    )
    result_messages = ChatMessages([dict(message) for message in messages], model=model)
    result_messages.append({"role": "assistant", "content": ""})
    result_messages.api_endpoint_name = "anthropic"
    result_messages.api_endpoint_base_url = _normalize_base_url(base_url)
    yield result_messages

    httpx_kwargs = {
        "timeout": float(timeout or 3600),
        "headers": _build_headers(api_key, extra_headers=extra_headers),
    }
    if proxies:
        httpx_kwargs["proxy"] = proxies

    url = f"{_normalize_base_url(base_url)}/messages"
    stream_payload = dict(request_payload)
    stream_payload["stream"] = True
    content = ""

    try:
        with httpx.Client(**httpx_kwargs) as client:
            with client.stream("POST", url, json=stream_payload) as response:
                _raise_for_error(response)
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except Exception:
                        continue
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta") or {}
                        text = delta.get("text") or ""
                        if text:
                            content += text
                            result_messages[-1]["content"] = content
                            yield result_messages
                    elif event.get("type") == "message_delta":
                        usage = event.get("usage") or {}
                        if usage:
                            result_messages.usage = usage
            if content:
                return result_messages
    except Exception:
        # Some third-party Claude gateways implement /messages but not SSE.
        # Fall back to a non-streaming request so the caller still works.
        content = ""

    non_stream_payload = dict(request_payload)
    non_stream_payload["stream"] = False
    with httpx.Client(**httpx_kwargs) as client:
        response = client.post(url, json=non_stream_payload)
        _raise_for_error(response)
        payload = response.json()
    content = _extract_content_text(payload.get("content"))
    result_messages[-1]["content"] = content
    result_messages.usage = payload.get("usage") or {}
    yield result_messages
    return result_messages


if __name__ == "__main__":
    pass
