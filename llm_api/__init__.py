from typing import Dict, Generator

from .mongodb_cache import llm_api_cache
from .baidu_api import stream_chat_with_wenxin, wenxin_model_config
from .doubao_api import stream_chat_with_doubao, doubao_model_config
from .chat_messages import ChatMessages
from .openai_api import stream_chat_with_gpt, gpt_model_config
from .zhipuai_api import stream_chat_with_zhipuai, zhipuai_model_config


class ModelConfig(dict):
    def __init__(self, model: str, **options):
        super().__init__(**options)
        self["model"] = model
        self.validate()

    def validate(self):
        def normalize_positive_int(key, fallback_key=None):
            raw_value = self.get(key)
            if raw_value in (None, '') and fallback_key is not None:
                raw_value = self.get(fallback_key)
            if raw_value in (None, ''):
                raise ValueError(f"ModelConfig missing key: {key}")
            value = int(raw_value)
            if value <= 0:
                raise ValueError(f"{key} must be greater than 0")
            self[key] = value
            return value

        def check_key(provider, keys):
            for key in keys:
                if key not in self:
                    raise ValueError(f"{provider} missing config key: {key}")
                if not str(self[key]).strip():
                    raise ValueError(f"{provider} empty config key: {key}")

        model_name = self["model"]

        if model_name in wenxin_model_config:
            check_key("Wenxin", ["ak", "sk"])
        elif model_name in doubao_model_config:
            check_key("Doubao", ["api_key", "endpoint_id"])
        elif model_name in zhipuai_model_config:
            check_key("ZhipuAI", ["api_key"])
        else:
            # Unknown model names fall back to OpenAI-compatible calling.
            check_key("OpenAI", ["api_key"])

        max_output_tokens = normalize_positive_int("max_output_tokens", fallback_key="max_tokens")
        self["max_tokens"] = max_output_tokens
        normalize_positive_int("max_input_tokens", fallback_key="max_tokens")

    def get_api_keys(self) -> Dict[str, str]:
        return {k: v for k, v in self.items() if k != "model"}


@llm_api_cache()
def stream_chat(model_config: ModelConfig, messages: list, response_json=False) -> Generator:
    if isinstance(model_config, dict):
        model_config = ModelConfig(**model_config)

    model_config.validate()

    messages = ChatMessages(messages, model=model_config["model"])
    max_input_tokens = int(model_config["max_input_tokens"])
    max_output_tokens = int(model_config["max_output_tokens"])

    if messages.count_message_tokens() > max_input_tokens:
        raise Exception(
            f"Request text is too long, exceeds max_input_tokens:{max_input_tokens}."
        )

    yield messages

    if model_config["model"] in wenxin_model_config:
        result = yield from stream_chat_with_wenxin(
            messages,
            model=model_config["model"],
            ak=model_config["ak"],
            sk=model_config["sk"],
            max_tokens=max_output_tokens,
            response_json=response_json,
        )
    elif model_config["model"] in doubao_model_config:
        result = yield from stream_chat_with_doubao(
            messages,
            model=model_config["model"],
            endpoint_id=model_config["endpoint_id"],
            api_key=model_config["api_key"],
            max_tokens=max_output_tokens,
            response_json=response_json,
        )
    elif model_config["model"] in zhipuai_model_config:
        result = yield from stream_chat_with_zhipuai(
            messages,
            model=model_config["model"],
            api_key=model_config["api_key"],
            max_tokens=max_output_tokens,
            response_json=response_json,
        )
    else:
        result = yield from stream_chat_with_gpt(
            messages,
            model=model_config["model"],
            api_key=model_config["api_key"],
            base_url=model_config.get("base_url"),
            api_chain=model_config.get("api_chain"),
            api_chain_reset_time=model_config.get("api_chain_reset_time"),
            api_chain_rate_limit_retries=model_config.get("api_chain_rate_limit_retries"),
            proxies=model_config.get("proxies"),
            reasoning_effort=model_config.get("reasoning_effort"),
            temperature=model_config.get("temperature"),
            top_p=model_config.get("top_p"),
            timeout=model_config.get("timeout"),
            max_retries=model_config.get("max_retries"),
            max_tokens=max_output_tokens,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            response_json=response_json,
        )

    result.finished = True
    yield result

    return result


def test_stream_chat(model_config: ModelConfig):
    messages = [{"role": "user", "content": "1+1=? Return only the answer."}]
    for response in stream_chat(model_config, messages, use_cache=False):
        yield response.response

    return response


__all__ = [
    "ChatMessages",
    "stream_chat",
    "wenxin_model_config",
    "doubao_model_config",
    "gpt_model_config",
    "zhipuai_model_config",
    "ModelConfig",
]
