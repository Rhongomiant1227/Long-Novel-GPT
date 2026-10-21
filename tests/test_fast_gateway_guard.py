import unittest

from llm_api.openai_api import (
    _classify_api_chain_error,
    _looks_like_prompt_corruption_response,
)


class FastGatewayGuardTest(unittest.TestCase):
    def test_detects_garbled_prompt_refusal(self):
        text = (
            "It looks like your message got garbled with a bunch of question "
            "marks. Could you clarify what you were trying to ask?"
        )
        self.assertTrue(_looks_like_prompt_corruption_response(text))

    def test_classifies_corrupted_prompt_as_cooldown_error(self):
        error = RuntimeError("provider_prompt_corrupted: gateway returned a prompt-garbled refusal")
        self.assertEqual(_classify_api_chain_error(error), "provider_prompt_corrupted")

    def test_does_not_flag_normal_chinese_dialogue(self):
        self.assertFalse(
            _looks_like_prompt_corruption_response(
                "许沉捏紧拆线钳，低声说：“别把信号灯当成出口。”"
            )
        )


if __name__ == "__main__":
    unittest.main()
