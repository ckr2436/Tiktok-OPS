from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.tasks import ttb_gmvmax_tasks


class SmartGuardTaskLockTests(unittest.TestCase):
    @patch.object(ttb_gmvmax_tasks, "_db_session")
    @patch.object(ttb_gmvmax_tasks, "RedisDistributedLock")
    def test_inflight_cycle_is_skipped_before_opening_database_session(
        self,
        lock_cls: MagicMock,
        db_session: MagicMock,
    ) -> None:
        lock = lock_cls.return_value
        lock.acquire.return_value = False

        result = ttb_gmvmax_tasks.smart_guard_cycle_task.run()

        self.assertEqual("skipped", result["status"])
        self.assertEqual("inflight", result["reason"])
        db_session.assert_not_called()
        lock.release.assert_not_called()

    @patch.object(
        ttb_gmvmax_tasks,
        "recover_incomplete_gmvmax_create_intents",
        new_callable=AsyncMock,
    )
    @patch.object(ttb_gmvmax_tasks, "run_smart_guard_cycle", new_callable=AsyncMock)
    @patch.object(ttb_gmvmax_tasks, "_close_session")
    @patch.object(ttb_gmvmax_tasks, "_db_session")
    @patch.object(ttb_gmvmax_tasks, "RedisDistributedLock")
    def test_acquired_cycle_always_releases_lock(
        self,
        lock_cls: MagicMock,
        db_session: MagicMock,
        close_session: MagicMock,
        run_cycle: AsyncMock,
        recover_intents: AsyncMock,
    ) -> None:
        lock = lock_cls.return_value
        lock.acquire.return_value = True
        db = db_session.return_value
        run_cycle.return_value = {"checked": 2, "errors": 0}
        recover_intents.return_value = {
            "checked": 1,
            "quarantined": 1,
            "errors": 0,
        }

        with (
            patch.object(
                ttb_gmvmax_tasks,
                "acquire_guard_action_lease",
                return_value=7,
            ) as acquire_lease,
            patch.object(
                ttb_gmvmax_tasks,
                "release_guard_action_lease",
                return_value=True,
            ) as release_lease,
        ):
            result = ttb_gmvmax_tasks.smart_guard_cycle_task.run()

        self.assertEqual(2, result["checked"])
        self.assertEqual(7, result["fencing_token"])
        self.assertEqual(
            1,
            result["create_intent_recovery"]["quarantined"],
        )
        self.assertGreaterEqual(db.commit.call_count, 3)
        recover_intents.assert_awaited_once_with(db)
        run_cycle.assert_awaited_once_with(db)
        acquire_lease.assert_called_once()
        release_lease.assert_called_once()
        close_session.assert_called_once_with(db)
        lock.release.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
