"""供应商普通响应与流式回执的逐次用量解析。"""

import json
import unittest

from src.agent.provider_usage import ProviderUsageParser, usage_from_payload
from src.agent.usage_pricing import TokenUsage


class ProviderUsageTests(unittest.TestCase):
    def test_chat_json_preserves_missing_cache_usage(self):
        self.assertEqual(usage_from_payload({"usage": {
            "prompt_tokens": 10, "completion_tokens": 3,
        }}), TokenUsage(10, 3, None))
        self.assertEqual(usage_from_payload({"usage": {
            "prompt_tokens": 10, "completion_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 0},
        }}), TokenUsage(10, 3, 0))

    def test_responses_usage_and_invalid_counts(self):
        self.assertEqual(usage_from_payload({"response": {"usage": {
            "input_tokens": 9, "output_tokens": 4,
            "input_tokens_details": {"cached_tokens": 2},
        }}}), TokenUsage(9, 4, 2))
        self.assertIsNone(usage_from_payload({"usage": {
            "prompt_tokens": -1, "completion_tokens": 1,
        }}))

    def test_sse_chunks_capture_last_provider_receipt(self):
        parser = ProviderUsageParser()
        events = (
            'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            'data: {"usage":{"prompt_tokens":8,"completion_tokens":2,'
            '"prompt_tokens_details":{"cached_tokens":1}}}\n\n'
            'data: [DONE]\n\n'
        ).encode()
        for offset in range(0, len(events), 7):
            parser.feed(events[offset:offset + 7])
        self.assertEqual(parser.finish(), TokenUsage(8, 2, 1))

    def test_json_body_chunks_and_unknown(self):
        parser = ProviderUsageParser()
        body = json.dumps({"usage": {"input_tokens": 5, "output_tokens": 1,
                                     "input_tokens_details": {"cached_tokens": 0}}}).encode()
        parser.feed(body[:10])
        parser.feed(body[10:])
        self.assertEqual(parser.finish(), TokenUsage(5, 1, 0))
        self.assertIsNone(ProviderUsageParser().finish())


if __name__ == "__main__":
    unittest.main()
