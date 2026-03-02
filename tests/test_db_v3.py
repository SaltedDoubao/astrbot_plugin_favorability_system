import os
import sqlite3
import tempfile
import unittest

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
            total = db.sum_positive_delta_since("group", "100", "u1", 1729999999)
            self.assertEqual(count, 1)
            self.assertEqual(total, 5)
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


if __name__ == "__main__":
    unittest.main()
