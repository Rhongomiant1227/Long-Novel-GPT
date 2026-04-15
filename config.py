import json
import os
from pathlib import Path
from dotenv import dotenv_values, load_dotenv

print("Loading .env file...")
env_path = os.path.join(os.path.dirname(__file__), '.env')
DEFAULT_OPENAI_COMPAT_BASE_URL = os.getenv('DEFAULT_OPENAI_COMPAT_BASE_URL', 'https://www.ananapi.com/')
DEFAULT_SUB2API_CHAIN_FILE = Path.home() / '.codex' / 'sub2api_priority.json'


def _load_openai_fallback_env():
    if not os.getenv('GPT_API_KEY', '').strip():
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

    if not os.getenv('GPT_API_KEY', '').strip():
        openai_api_key = os.getenv('OPENAI_API_KEY', '').strip()
        if openai_api_key:
            os.environ['GPT_API_KEY'] = openai_api_key

    if not os.getenv('GPT_BASE_URL', '').strip():
        openai_base_url = os.getenv('OPENAI_BASE_URL', '').strip()
        if openai_base_url:
            os.environ['GPT_BASE_URL'] = openai_base_url

    # Keep direct CLI/script invocations aligned with the project start scripts.
    # Without this fallback, ad-hoc python runs can silently hit the official
    # OpenAI endpoint and fail on region restrictions instead of using the proxy
    # endpoint already standardized for this repository.
    if not os.getenv('GPT_BASE_URL', '').strip():
        os.environ['GPT_BASE_URL'] = DEFAULT_OPENAI_COMPAT_BASE_URL


def _first_env(*names):
    for name in names:
        value = os.getenv(name, '').strip()
        if value:
            return value
    return ''


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


def _read_json_file(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except Exception:
        return None


def _read_openai_api_key_from_auth_file(path_text):
    if not path_text:
        return ''
    try:
        path = Path(path_text).expanduser().resolve()
    except Exception:
        path = Path(path_text).expanduser()
    if not path.exists():
        return ''
    payload = _read_json_file(path)
    if not isinstance(payload, dict):
        return ''
    return str(
        payload.get('OPENAI_API_KEY')
        or payload.get('api_key')
        or payload.get('key')
        or ''
    ).strip()


def _normalize_openai_chain_entry(prefix, entry, index, default_proxies=''):
    if not isinstance(entry, dict):
        return None

    base_url = str(
        entry.get('base_url')
        or entry.get('endpoint')
        or entry.get('url')
        or ''
    ).strip()
    if not base_url:
        return None

    api_key = str(entry.get('api_key') or '').strip()
    if not api_key:
        env_name = str(entry.get('api_key_env') or entry.get('env') or '').strip()
        if env_name:
            api_key = os.getenv(env_name, '').strip()
    if not api_key:
        api_key = _read_openai_api_key_from_auth_file(
            str(entry.get('auth_file') or entry.get('auth_path') or '').strip()
        )
    if not api_key:
        return None

    name = str(
        entry.get('name')
        or entry.get('label')
        or entry.get('provider')
        or f'{prefix.lower()}_{index + 1}'
    ).strip()

    return {
        'name': name,
        'base_url': base_url,
        'api_key': api_key,
        'proxies': str(entry.get('proxies') or default_proxies or '').strip(),
    }


def _load_openai_compat_api_chain(prefix, default_file: Path | None = None):
    payload = None

    chain_json = _first_env(f'{prefix}_API_CHAIN_JSON', f'{prefix}_API_CHAIN')
    if chain_json:
        try:
            payload = json.loads(chain_json)
        except Exception:
            payload = None

    if payload is None:
        chain_file_value = os.getenv(f'{prefix}_API_CHAIN_FILE', '').strip()
        if not chain_file_value and default_file is not None:
            chain_file_value = str(default_file)
        if chain_file_value:
            try:
                chain_file = Path(chain_file_value).expanduser().resolve()
            except Exception:
                chain_file = Path(chain_file_value).expanduser()
            if chain_file.exists():
                payload = _read_json_file(chain_file)

    if isinstance(payload, dict):
        entries = payload.get('providers') or payload.get('endpoints') or payload.get('chain') or []
        reset_time = str(
            payload.get('daily_reset_time')
            or payload.get('rollover_time')
            or payload.get('reset_time')
            or ''
        ).strip()
        rate_limit_retries = payload.get('rate_limit_retries')
    elif isinstance(payload, list):
        entries = payload
        reset_time = ''
        rate_limit_retries = None
    else:
        entries = []
        reset_time = ''
        rate_limit_retries = None

    default_proxies = _first_env(f'{prefix}_PROXIES', 'GPT_PROXIES')
    normalized_chain = []
    for index, entry in enumerate(entries):
        normalized_entry = _normalize_openai_chain_entry(prefix, entry, index, default_proxies=default_proxies)
        if normalized_entry:
            normalized_chain.append(normalized_entry)

    if not normalized_chain:
        return {}

    primary = normalized_chain[0]
    result = {
        'api_chain': normalized_chain,
        'base_url': primary['base_url'],
        'api_key': primary['api_key'],
    }
    if primary.get('proxies'):
        result['proxies'] = primary['proxies']
    if reset_time:
        result['api_chain_reset_time'] = reset_time
    if rate_limit_retries not in (None, ''):
        result['api_chain_rate_limit_retries'] = int(rate_limit_retries)
    return result


def _default_token_limits(provider, available_models=None):
    prefix = provider.upper()
    legacy_max_tokens = _read_int_env(f'{prefix}_MAX_TOKENS')
    max_input_tokens = _read_int_env(f'{prefix}_MAX_INPUT_TOKENS')
    max_output_tokens = _read_int_env(f'{prefix}_MAX_OUTPUT_TOKENS')

    models = [m.strip() for m in (available_models or []) if m and m.strip()]
    default_input_tokens = 4096
    default_output_tokens = 4096

    if provider in {'gpt', 'sub2api'} and any(model.startswith('gpt-5') for model in models):
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
GPT_API_CHAIN_SETTINGS = _load_openai_compat_api_chain('GPT')
SUB2API_API_CHAIN_SETTINGS = _load_openai_compat_api_chain('SUB2API', default_file=DEFAULT_SUB2API_CHAIN_FILE)


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
        'base_url': GPT_API_CHAIN_SETTINGS.get('base_url', os.getenv('GPT_BASE_URL', '')),
        'api_key': GPT_API_CHAIN_SETTINGS.get('api_key', os.getenv('GPT_API_KEY', '')),
        'proxies': GPT_API_CHAIN_SETTINGS.get('proxies', os.getenv('GPT_PROXIES', '')),
        'reasoning_effort': os.getenv('GPT_REASONING_EFFORT', ''),
        'temperature': os.getenv('GPT_TEMPERATURE', ''),
        'top_p': os.getenv('GPT_TOP_P', ''),
        'timeout': float(os.getenv('GPT_TIMEOUT_SECONDS', 3600)),
        'max_retries': int(os.getenv('GPT_MAX_RETRIES', 3)),
        'available_models': os.getenv('GPT_AVAILABLE_MODELS', '').split(','),
        **GPT_API_CHAIN_SETTINGS,
        **_default_token_limits('gpt', os.getenv('GPT_AVAILABLE_MODELS', '').split(',')),
    },
    'sub2api': {
        'base_url': SUB2API_API_CHAIN_SETTINGS.get('base_url', _first_env('SUB2API_BASE_URL', 'GPT_BASE_URL', 'OPENAI_BASE_URL')),
        'api_key': SUB2API_API_CHAIN_SETTINGS.get('api_key', _first_env('SUB2API_API_KEY', 'GPT_API_KEY', 'OPENAI_API_KEY')),
        'proxies': SUB2API_API_CHAIN_SETTINGS.get('proxies', _first_env('SUB2API_PROXIES', 'GPT_PROXIES')),
        'reasoning_effort': _first_env('SUB2API_REASONING_EFFORT', 'GPT_REASONING_EFFORT'),
        'temperature': _first_env('SUB2API_TEMPERATURE', 'GPT_TEMPERATURE'),
        'top_p': _first_env('SUB2API_TOP_P', 'GPT_TOP_P'),
        'timeout': float(_first_env('SUB2API_TIMEOUT_SECONDS', 'GPT_TIMEOUT_SECONDS') or 3600),
        'max_retries': int(_first_env('SUB2API_MAX_RETRIES', 'GPT_MAX_RETRIES') or 3),
        'available_models': _first_env('SUB2API_AVAILABLE_MODELS', 'GPT_AVAILABLE_MODELS').split(','),
        **SUB2API_API_CHAIN_SETTINGS,
        **_default_token_limits('sub2api', _first_env('SUB2API_AVAILABLE_MODELS', 'GPT_AVAILABLE_MODELS').split(',')),
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

DEFAULT_MAIN_MODEL = os.getenv('DEFAULT_MAIN_MODEL', 'sub2api/gpt-5.4')
DEFAULT_SUB_MODEL = os.getenv('DEFAULT_SUB_MODEL', 'sub2api/gpt-5.4')

ENABLE_ONLINE_DEMO = os.getenv('ENABLE_ONLINE_DEMO', 'false').lower() == 'true'
