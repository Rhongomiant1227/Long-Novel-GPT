import unittest

from config import _default_token_limits


class FastProviderConfigTest(unittest.TestCase):
    def test_fast_grok_models_receive_long_context_budget(self):
        limits = _default_token_limits("fast", ["grok-4.6"])

        self.assertGreaterEqual(limits["max_input_tokens"], 60_000)
        self.assertEqual(limits["max_output_tokens"], 16_000)


if __name__ == "__main__":
    unittest.main()
