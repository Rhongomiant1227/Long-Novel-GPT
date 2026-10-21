"""Smoke-test the project's direct fast provider without console encoding loss."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backend_utils import get_model_config_from_provider_model
from llm_api import stream_chat


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    model_name = os.getenv("FAST_PROBE_MODEL", "grok-4.5").strip()
    model = get_model_config_from_provider_model(f"fast/{model_name}")
    messages = [
        {
            "role": "system",
            "content": "你是中文商业小说作者。严格遵守用户事实，只输出可发布正文。",
        },
        {
            "role": "user",
            "content": (
                "写一段约一百五十字的中文都市悬疑正文。凌晨两点十七分，"
                "封闭三年的旧地铁站只有一盏信号灯亮起。主角许沉是来拆除"
                "设备的通信工程师；他没有门禁卡，只带拆线钳和没电的旧对讲机。"
                "让他通过现场物件得到一个可验证的新线索，结尾必须是完整动作"
                "或完整对白。只输出正文，不要标题、解释、总结或英文。"
            ),
        },
    ]

    started = time.perf_counter()
    final = None
    for current in stream_chat(model, messages):
        final = current

    response = str(getattr(final, "response", "") or "").strip()
    print({
        "model": model_name,
        "base_url": model.get("base_url"),
        "seconds": round(time.perf_counter() - started, 2),
        "chars": len(response),
        "text": response,
    })
    return 0 if response else 1


if __name__ == "__main__":
    raise SystemExit(main())
