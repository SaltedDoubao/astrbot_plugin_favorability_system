import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import FavorabilityDB


class FavorabilityDBV3Tests(unittest.TestCase):
    def test_db_path_without_directory_is_supported(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = os.getcwd()
            try:
                os.chdir(td)
                db = FavorabilityDB("favorability.db")
                self.assertTrue(os.path.exists(os.path.join(td, "favorability.db")))
                db.close()
            finally:
                os.chdir(cwd)

    def test_migrate_v2_to_v3_keeps_user_data(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "favorability.db")
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE users (
                    session_type TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    level INTEGER NOT NULL,
                    PRIMARY KEY (session_type, session_id, user_id)
                );
                CREATE TABLE nicknames (
                    session_type TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
                    created_at INTEGER NOT NULL,
                    UNIQUE(session_type, session_id, user_id, nickname)
                );
                CREATE INDEX idx_nick_lookup
                ON nicknames(session_type, session_id, nickname, is_current);
                INSERT INTO meta (key, value) VALUES ('schema_version', '2');
                INSERT INTO users (session_type, session_id, user_id, level)
                VALUES ('group', '100', 'u1', 12);
                """
            )
            conn.commit()
            conn.close()

            db = FavorabilityDB(db_path)
            user = db.get_user("group", "100", "u1")
            self.assertIsNotNone(user)
            self.assertEqual(user.level, 12)
            self.assertIsNotNone(user.daily_bucket)
            db.remove_user("group", "100", "u1")
            left_nicknames = db.conn.execute("SELECT COUNT(*) FROM nicknames").fetchone()[0]
            self.assertEqual(left_nicknames, 0)
            db.close()

    def test_score_events_aggregation(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "favorability.db")
            db = FavorabilityDB(db_path)
            db.add_user("group", "100", "u1", 0)

            db.log_score_event(
                "group",
                "100",
                "u1",
                "thanks",
                2,
                5,
                5,
                1.0,
                1730000000,
                "感谢反馈",
            )
            db.log_score_event(
                "group",
                "100",
                "u1",
                "thanks",
                2,
                3,
                0,
                0.3,
                1730000010,
                "重复消息",
            )
            db.log_score_event(
                "group",
                "100",
                "u1",
                "rude",
                2,
                -6,
                -6,
                1.0,
                1730000020,
                "无礼表达",
            )

            count = db.count_positive_events_by_type_since(
                "group", "100", "u1", "thanks", 1729999999
            )
            negative_count = db.count_negative_events_by_type_since(
                "group", "100", "u1", "rude", 1729999999
            )
            total = db.sum_positive_delta_since("group", "100", "u1", 1729999999)
            self.assertEqual(count, 1)
            self.assertEqual(negative_count, 1)
            self.assertEqual(total, 5)
            db.close()

    def test_score_events_schema_still_valid_after_ddl_dedup(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "favorability.db")
            db = FavorabilityDB(db_path)
            db._validate_schema()
            version_row = db.conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            self.assertIsNotNone(version_row)
            assert version_row is not None
            self.assertEqual(int(version_row[0]), 3)
            db.close()

    def test_remove_user_clears_score_events(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "favorability.db")
            db = FavorabilityDB(db_path)
            db.add_user("group", "100", "u1", 0)
            db.log_score_event(
                "group",
                "100",
                "u1",
                "thanks",
                2,
                5,
                5,
                1.0,
                1730000000,
                "感谢反馈",
            )
            self.assertTrue(db.remove_user("group", "100", "u1"))
            left_events = db.conn.execute("SELECT COUNT(*) FROM score_events").fetchone()[0]
            self.assertEqual(left_events, 0)
            db.close()

    def test_count_negative_events_by_type_since(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "favorability.db")
            db = FavorabilityDB(db_path)
            db.add_user("group", "100", "u1", 0)
            db.log_score_event(
                "group",
                "100",
                "u1",
                "rude",
                2,
                -6,
                -6,
                1.0,
                1730000020,
                "无礼表达",
            )
            count = db.count_negative_events_by_type_since(
                "group", "100", "u1", "rude", 1730000010
            )
            self.assertEqual(count, 1)
            db.close()

    def test_only_one_current_nickname_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "favorability.db")
            db = FavorabilityDB(db_path)
            db.add_user("group", "100", "u1", 0)
            db.conn.execute(
                """
                INSERT INTO nicknames
                (session_type, session_id, user_id, nickname, is_current, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("group", "100", "u1", "a", 1, 1),
            )
            db.conn.commit()

            with self.assertRaises(sqlite3.IntegrityError):
                db.conn.execute(
                    """
                    INSERT INTO nicknames
                    (session_type, session_id, user_id, nickname, is_current, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("group", "100", "u1", "b", 1, 2),
                )
            db.conn.rollback()
            db.close()

    def test_immediate_transaction_rolls_back_on_error(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "favorability.db")
            db = FavorabilityDB(db_path)
            db.add_user("group", "100", "u1", 0)

            with self.assertRaises(sqlite3.IntegrityError):
                with db.immediate_transaction():
                    db.update_level("group", "100", "u1", 10, commit=False)
                    db.log_score_event(
                        "group",
                        "100",
                        "u1",
                        "thanks",
                        2,
                        5,
                        5,
                        1.0,
                        1730000000,
                        None,  # type: ignore[arg-type]
                        commit=False,
                    )

            user = db.get_user("group", "100", "u1")
            assert user is not None
            self.assertEqual(user.level, 0)
            db.close()

    def test_reset_user_and_reset_session_users(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "favorability.db")
            db = FavorabilityDB(db_path)
            today = "2026-03-02"
            db.add_user(
                "group",
                "100",
                "u1",
                66,
                daily_pos_gain=8,
                daily_neg_gain=2,
                daily_bucket=today,
            )
            db.add_user("group", "100", "u2", 50, daily_pos_gain=1, daily_neg_gain=0)
            db.add_user("group", "200", "u3", 70)

            self.assertTrue(
                db.reset_user(
                    "group",
                    "100",
                    "u1",
                    5,
                    last_interaction_at=123456,
                    daily_bucket=today,
                )
            )
            user = db.get_user("group", "100", "u1")
            assert user is not None
            self.assertEqual(user.level, 5)
            self.assertEqual(user.daily_pos_gain, 0)
            self.assertEqual(user.daily_neg_gain, 0)
            self.assertEqual(user.last_interaction_at, 123456)

            reset_count = db.reset_session_users("group", "100", 9, daily_bucket=today)
            self.assertEqual(reset_count, 2)
            u1 = db.get_user("group", "100", "u1")
            u2 = db.get_user("group", "100", "u2")
            u3 = db.get_user("group", "200", "u3")
            assert u1 is not None and u2 is not None and u3 is not None
            self.assertEqual(u1.level, 9)
            self.assertEqual(u2.level, 9)
            self.assertEqual(u3.level, 70)
            db.close()

    def test_fetch_export_rows_scope_filters(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "favorability.db")
            db = FavorabilityDB(db_path)
            db.add_user("group", "100", "u1", 10)
            db.add_user("group", "200", "u2", 20)
            db.upsert_current_nickname("group", "100", "u1", "昵称1")
            db.upsert_current_nickname("group", "200", "u2", "昵称2")
            db.log_score_event(
                "group", "100", "u1", "thanks", 1, 4, 4, 1.0, 1730000000, "KW_THANKS"
            )
            db.log_score_event(
                "group", "200", "u2", "thanks", 1, 4, 4, 1.0, 1730000010, "KW_THANKS"
            )

            session_rows = db.fetch_export_rows(
                "session", session_type="group", session_id="100"
            )
            global_rows = db.fetch_export_rows("global")
            self.assertEqual(len(session_rows["users"]), 1)
            self.assertEqual(len(session_rows["nicknames"]), 1)
            self.assertEqual(len(session_rows["score_events"]), 1)
            self.assertEqual(len(global_rows["users"]), 2)
            self.assertEqual(len(global_rows["nicknames"]), 2)
            self.assertEqual(len(global_rows["score_events"]), 2)
            db.close()

    def test_get_stats_scope_filters(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "favorability.db")
            db = FavorabilityDB(db_path)
            bucket = time.strftime("%Y-%m-%d", time.localtime())
            db.add_user(
                "group",
                "100",
                "u1",
                10,
                daily_pos_gain=7,
                daily_neg_gain=1,
                daily_bucket=bucket,
            )
            db.add_user(
                "group",
                "200",
                "u2",
                30,
                daily_pos_gain=5,
                daily_neg_gain=2,
                daily_bucket=bucket,
            )
            db.log_score_event(
                "group", "100", "u1", "thanks", 1, 4, 4, 1.0, 1730000000, "KW_THANKS"
            )
            db.log_score_event(
                "group", "200", "u2", "thanks", 1, 4, 4, 1.0, 1730000010, "KW_THANKS"
            )

            session_stats = db.get_stats("session", session_type="group", session_id="100")
            global_stats = db.get_stats("global")
            self.assertEqual(session_stats["user_count"], 1)
            self.assertEqual(global_stats["user_count"], 2)
            self.assertEqual(session_stats["score_event_count"], 1)
            self.assertEqual(global_stats["score_event_count"], 2)
            self.assertEqual(session_stats["daily_pos_total"], 7)
            self.assertEqual(global_stats["daily_pos_total"], 12)
            db.close()


if __name__ == "__main__":
    unittest.main()
