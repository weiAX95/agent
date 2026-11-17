"""Run Controller 领域状态机的离线回归测试，无外部数据库或模型依赖。"""

import unittest
import uuid

from src.agent.run_policy import (
    RunController,
    RunLimitError,
    RunLimits,
    RunStateError,
    RunStatus,
)


class RunControllerTests(unittest.TestCase):
    def setUp(self):
        self.now = [10.0]
        self.run = RunController(
            run_id=uuid.uuid4(),
            request_id=uuid.uuid4(),
            owner_id=uuid.uuid4(),
            limits=RunLimits(max_steps=2, timeout_seconds=5),
            clock=lambda: self.now[0],
        )

    def test_run_starts_once_and_records_steps(self):
        self.assertEqual(self.run.status, RunStatus.CREATED)
        self.run.start()
        self.assertEqual(self.run.before_step(), 1)
        self.assertEqual(self.run.before_step(), 2)
        self.assertEqual(self.run.steps, 2)
        with self.assertRaises(RunStateError):
            self.run.start()

    def test_step_limit_rejects_extra_work_and_is_terminal(self):
        self.run.start()
        self.run.before_step()
        self.run.before_step()
        with self.assertRaises(RunLimitError) as context:
            self.run.before_step()
        self.assertEqual(context.exception.code, "RUN_STEP_LIMIT_EXCEEDED")
        self.assertEqual(self.run.status, RunStatus.FAILED)
        self.assertEqual(self.run.steps, 2)
        with self.assertRaises(RunStateError):
            self.run.before_step()

    def test_deadline_is_checked_at_boundary(self):
        self.run.start()
        self.now[0] = 14.999
        self.run.check_active()
        self.now[0] = 15.0
        with self.assertRaises(RunLimitError) as context:
            self.run.check_active()
        self.assertEqual(context.exception.code, "RUN_DEADLINE_EXCEEDED")
        self.assertEqual(self.run.status, RunStatus.TIMED_OUT)

    def test_late_success_cannot_override_timeout(self):
        self.run.start()
        self.now[0] = 20
        with self.assertRaises(RunLimitError):
            self.run.finish(RunStatus.SUCCEEDED)
        self.assertEqual(self.run.status, RunStatus.TIMED_OUT)
        with self.assertRaises(RunStateError):
            self.run.finish(RunStatus.FAILED)

    def test_one_terminal_transition_only(self):
        self.run.start()
        self.run.finish(RunStatus.CANCELLED)
        with self.assertRaises(RunStateError):
            self.run.finish(RunStatus.SUCCEEDED)
        with self.assertRaises(RunStateError):
            self.run.before_step()

    def test_invalid_transition_does_not_mutate_status(self):
        with self.assertRaises(RunStateError):
            self.run.finish(RunStatus.FAILED)
        self.assertEqual(self.run.status, RunStatus.CREATED)
        self.run.start()
        with self.assertRaises(ValueError):
            self.run.finish(RunStatus.RUNNING)
        self.assertEqual(self.run.status, RunStatus.RUNNING)

    def test_failure_is_explicit_terminal(self):
        self.run.start()
        self.run.finish(RunStatus.FAILED)
        self.assertEqual(self.run.status, RunStatus.FAILED)

    def test_limits_reject_invalid_values(self):
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                RunLimits(max_steps=limit)
        for timeout in (0, -1, 3601, True, "10"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                RunLimits(timeout_seconds=timeout)


if __name__ == "__main__":
    unittest.main()
