"""运维命令必须默认只读，执行时明确提供审计身份与证据。"""

import unittest
import uuid

from tools.reconcile_model_calls import parse_args


class ReconcileCommandTests(unittest.TestCase):
    def test_preview_is_default(self):
        args = parse_args([])
        self.assertFalse(args.apply)
        self.assertEqual(args.grace_seconds, 300)
        self.assertEqual(args.limit, 100)

    def test_apply_requires_reviewer_and_evidence(self):
        with self.assertRaises(SystemExit):
            parse_args(["--apply"])
        reviewer = uuid.uuid4()
        args = parse_args(["--apply", "--reviewer-id", str(reviewer),
                           "--evidence-ref", "provider-ticket-123"])
        self.assertEqual(args.reviewer_id, reviewer)
        self.assertEqual(args.evidence_ref, "provider-ticket-123")


if __name__ == "__main__":
    unittest.main()
