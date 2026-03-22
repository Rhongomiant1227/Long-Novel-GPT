import json
import os
from pathlib import Path
from dotenv import dotenv_values, load_dotenv

print("Loading .env file...")
env_path = os.path.join(os.path.dirname(__file__), '.env')


def _load_openai_fallback_env():
    if not os.getenv('GPT_API_KEY', '').strip():
        openai_api_key = os.getenv('OPENAI_API_KEY', '').strip()
        if openai_api_key:
            os.environ['GPT_API_KEY'] = openai_api_key

    if not os.getenv('GPT_BASE_URL', '').strip():
        openai_base_url = os.getenv('OPENAI_BASE_URL', '').strip()
        if openai_base_url:
            os.environ['GPT_BASE_URL'] = openai_base_url

    if os.getenv('GPT_API_KEY', '').strip():
        return

    auth_candidates = [
        Path.home() / '.codex' / 'auth.json',
        Path(os.getenv('USERPROFILE', '')).expanduser() / '.codex' / 'auth.json',
    ]
    seen = set()
    for candidate in auth_candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        if str(resolved) in seen or not resolved.exists():
            continue
        seen.add(str(resolved))
        try:
            payload = json.loads(resolved.read_text(encoding='utf-8'))
        except Exception:
            continue
        api_key = str(payload.get('OPENAI_API_KEY', '')).strip()
        if api_key:
            os.environ['GPT_API_KEY'] = api_key
            break

    # Keep direct CLI/script invocations aligned with the project start scripts.
    # Without this fallback, ad-hoc python runs can silently hit the official
    # OpenAI endpoint and fail on region restrictions instead of using the proxy
    # endpoint already standardized for this repository.
    if not os.getenv('GPT_BASE_URL', '').strip():
        os.environ['GPT_BASE_URL'] = 'https://fast.vpsairobot.com/v1'


def _read_int_env(name):
    value = os.getenv(name, '').strip()
    if not value:
        return None
    return int(value)


def _read_float_env(name):
    value = os.getenv(name, '').strip()
    if not value:
        return None
    return float(value)


def _default_token_limits(provider, available_models=None):
    prefix = provider.upper()
    legacy_max_tokens = _read_int_env(f'{prefix}_MAX_TOKENS')
    max_input_tokens = _read_int_env(f'{prefix}_MAX_INPUT_TOKENS')
    max_output_tokens = _read_int_env(f'{prefix}_MAX_OUTPUT_TOKENS')

    models = [m.strip() for m in (available_models or []) if m and m.strip()]
    default_input_tokens = 4096
    default_output_tokens = 4096

    if provider == 'gpt' and any(model.startswith('gpt-5') for model in models):
        # ChatMessages uses a rough heuristic tokenizer and underestimates Chinese prompts.
        # Keep a wide safety margin by default, while still using much more context than 200k.
        context_window = _read_int_env('GPT_CONTEXT_WINDOW') or 1_000_000
        input_budget_ratio = _read_float_env('GPT_INPUT_BUDGET_RATIO') or 0.35
        default_input_tokens = max(200000, min(context_window, int(context_window * input_budget_ratio)))
        default_output_tokens = 65536

    if legacy_max_tokens is not None:
        default_input_tokens = legacy_max_tokens
        default_output_tokens = legacy_max_tokens

    max_input_tokens = max_input_tokens or default_input_tokens
    max_output_tokens = max_output_tokens or default_output_tokens
    return {
        'max_tokens': max_output_tokens,
        'max_input_tokens': max_input_tokens,
        'max_output_tokens': max_output_tokens,
    }


def _mask_secret(key, value):
    if value is None:
        return value
    upper_key = key.upper()
    if any(token in upper_key for token in ['API_KEY', 'AK', 'SK']):
        if len(value) <= 8:
            return '*' * len(value)
        return f"{value[:4]}...{value[-4:]}"
    return value


if os.path.exists(env_path):
    env_dict = dotenv_values(env_path)
    
    print("Environment variables to be loaded:")
    for key, value in env_dict.items():
        print(f"{key}={_mask_secret(key, value)}")
    print("-" * 50)
    
    os.environ.update(env_dict)
    print(f"Loaded environment variables from: {env_path}")
else:
    print("Warning: .env file not found")

_load_openai_fallback_env()


# Thread Configuration
MAX_THREAD_NUM = int(os.getenv('MAX_THREAD_NUM', 5))


MAX_NOVEL_SUMMARY_LENGTH = int(os.getenv('MAX_NOVEL_SUMMARY_LENGTH', 20000))

# MongoDB Configuration
ENABLE_MONOGODB = os.getenv('ENABLE_MONGODB', 'false').lower() == 'true'
MONGODB_URI = os.getenv('MONGODB_URI', 'mongodb://127.0.0.1:27017/')
MONOGODB_DB_NAME = os.getenv('MONGODB_DB_NAME', 'llm_api')
ENABLE_MONOGODB_CACHE = os.getenv('ENABLE_MONGODB_CACHE', 'true').lower() == 'true'
CACHE_REPLAY_SPEED = float(os.getenv('CACHE_REPLAY_SPEED', 2))
CACHE_REPLAY_MAX_DELAY = float(os.getenv('CACHE_REPLAY_MAX_DELAY', 5))

# API Cost Limits
API_COST_LIMITS = {
    'HOURLY_LIMIT_RMB': float(os.getenv('API_HOURLY_LIMIT_RMB', 100)),
    'DAILY_LIMIT_RMB': float(os.getenv('API_DAILY_LIMIT_RMB', 500)),
    'USD_TO_RMB_RATE': float(os.getenv('API_USD_TO_RMB_RATE', 7))
}

# API Settings
API_SETTINGS = {
    'wenxin': {
        'ak': os.getenv('WENXIN_AK', ''),
        'sk': os.getenv('WENXIN_SK', ''),
        'available_models': os.getenv('WENXIN_AVAILABLE_MODELS', '').split(','),
        **_default_token_limits('wenxin', os.getenv('WENXIN_AVAILABLE_MODELS', '').split(',')),
    },
    'doubao': {
        'api_key': os.getenv('DOUBAO_API_KEY', ''),
        'endpoint_ids': os.getenv('DOUBAO_ENDPOINT_IDS', '').split(','),
        'available_models': os.getenv('DOUBAO_AVAILABLE_MODELS', '').split(','),
        **_default_token_limits('doubao', os.getenv('DOUBAO_AVAILABLE_MODELS', '').split(',')),
    },
    'gpt': {
        'base_url': os.getenv('GPT_BASE_URL', ''),
        'api_key': os.getenv('GPT_API_KEY', ''),
        'proxies': os.getenv('GPT_PROXIES', ''),
        'reasoning_effort': os.getenv('GPT_REASONING_EFFORT', ''),
        'temperature': os.getenv('GPT_TEMPERATURE', ''),
        'top_p': os.getenv('GPT_TOP_P', ''),
        'timeout': float(os.getenv('GPT_TIMEOUT_SECONDS', 3600)),
        'max_retries': int(os.getenv('GPT_MAX_RETRIES', 3)),
        'available_models': os.getenv('GPT_AVAILABLE_MODELS', '').split(','),
        **_default_token_limits('gpt', os.getenv('GPT_AVAILABLE_MODELS', '').split(',')),
    },
    'zhipuai': {
        'api_key': os.getenv('ZHIPUAI_API_KEY', ''),
        'available_models': os.getenv('ZHIPUAI_AVAILABLE_MODELS', '').split(','),
        **_default_token_limits('zhipuai', os.getenv('ZHIPUAI_AVAILABLE_MODELS', '').split(',')),
    },
    'local': {
        'base_url': os.getenv('LOCAL_BASE_URL', ''),
        'api_key': os.getenv('LOCAL_API_KEY', ''),
        'available_models': os.getenv('LOCAL_AVAILABLE_MODELS', '').split(','),
        **_default_token_limits('local', os.getenv('LOCAL_AVAILABLE_MODELS', '').split(',')),
    }
}

for model in API_SETTINGS.values():
    model['available_models'] = [e.strip() for e in model['available_models']]

DEFAULT_MAIN_MODEL = os.getenv('DEFAULT_MAIN_MODEL', 'wenxin/ERNIE-Novel-8K')
DEFAULT_SUB_MODEL = os.getenv('DEFAULT_SUB_MODEL', 'wenxin/ERNIE-3.5-8K')

ENABLE_ONLINE_DEMO = os.getenv('ENABLE_ONLINE_DEMO', 'false').lower() == 'true'
