import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional

SCHEMA_VERSION = 3


class SchemaMismatchError(RuntimeError):
    """数据库结构不符合当前版本要求。"""


class NicknameAmbiguousError(RuntimeError):
    """按昵称查询命中了多个用户。"""

    def __init__(self, nickname: str, user_ids: list[str]):
        self.nickname = nickname
        self.user_ids = user_ids
        super().__init__(
            f"昵称「{nickname}」在当前会话中对应多个用户: {', '.join(user_ids)}"
        )


@dataclass
class User:
    session_type: str
    session_id: str
    user_id: str
    level: int
    current_nickname: Optional[str] = None
    historical_nicknames: list[str] = field(default_factory=list)
    last_interaction_at: Optional[int] = None
    daily_pos_gain: int = 0
    daily_neg_gain: int = 0
    daily_bucket: Optional[str] = None


class FavorabilityDB:
    def __init__(self, db_path: str):
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.RLock()
        self.conn.execute("PRAGMA foreign_keys = ON")
        try:
            self._init_tables()
        except Exception:
            self.conn.close()
            raise

    @contextmanager
    def immediate_transaction(self) -> Iterator[None]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self.conn.rollback()
                raise
            self.conn.commit()

    def _init_tables(self):
        with self._lock:
            existing_tables = {
                row[0]
                for row in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            core_tables = {"meta", "users", "nicknames"}

            if not existing_tables.intersection(core_tables):
                self._create_schema()
                return

            if "meta" not in existing_tables:
                raise SchemaMismatchError(
                    "检测到旧版数据库结构（缺少 meta 表）。本版本不支持自动迁移，请删除旧数据库后重建。"
                )

            version_row = self.conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if not version_row:
                raise SchemaMismatchError(
                    "数据库缺少 schema_version。请删除旧数据库后重建。"
                )

            try:
                version = int(version_row[0])
            except (TypeError, ValueError) as exc:
                raise SchemaMismatchError("schema_version 非法，无法继续启动。") from exc

            if version == 2:
                self._migrate_v2_to_v3()
                version = 3

            if version != SCHEMA_VERSION:
                raise SchemaMismatchError(
                    f"数据库 schema_version={version}，当前插件要求 {SCHEMA_VERSION}。请删除旧数据库后重建。"
                )

            self._apply_v3_compat_fixes()
            self._validate_schema()

    def _create_schema(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                session_type TEXT NOT NULL,
                session_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                level INTEGER NOT NULL,
                last_interaction_at INTEGER,
                daily_pos_gain INTEGER NOT NULL DEFAULT 0,
                daily_neg_gain INTEGER NOT NULL DEFAULT 0,
                daily_bucket TEXT,
                PRIMARY KEY (session_type, session_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS nicknames (
                session_type TEXT NOT NULL,
                session_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                nickname TEXT NOT NULL,
                is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
                created_at INTEGER NOT NULL,
                FOREIGN KEY (session_type, session_id, user_id)
                    REFERENCES users(session_type, session_id, user_id) ON DELETE CASCADE,
                UNIQUE(session_type, session_id, user_id, nickname)
            );

            CREATE INDEX IF NOT EXISTS idx_nick_lookup
            ON nicknames(session_type, session_id, nickname, is_current);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_nick_current_unique
            ON nicknames(session_type, session_id, user_id)
            WHERE is_current = 1;

            CREATE TABLE IF NOT EXISTS score_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_type TEXT NOT NULL,
                session_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                interaction_type TEXT NOT NULL,
                intensity INTEGER NOT NULL,
                raw_delta INTEGER NOT NULL,
                final_delta INTEGER NOT NULL,
                anti_spam_mul REAL NOT NULL,
                created_at INTEGER NOT NULL,
                evidence TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (session_type, session_id, user_id)
                    REFERENCES users(session_type, session_id, user_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_score_events_user_time
            ON score_events(session_type, session_id, user_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_score_events_type_time
            ON score_events(session_type, session_id, user_id, interaction_type, created_at);
            """
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def _migrate_v2_to_v3(self):
        existing_tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        required_v2_tables = {"users", "nicknames"}
        missing = required_v2_tables - existing_tables
        if missing:
            raise SchemaMismatchError(
                f"v2 数据库缺少必要表: {', '.join(sorted(missing))}。无法迁移。"
            )

        try:
            user_columns = self._get_columns("users")
            if "last_interaction_at" not in user_columns:
                self.conn.execute("ALTER TABLE users ADD COLUMN last_interaction_at INTEGER")
            if "daily_pos_gain" not in user_columns:
                self.conn.execute(
                    "ALTER TABLE users ADD COLUMN daily_pos_gain INTEGER NOT NULL DEFAULT 0"
                )
            if "daily_neg_gain" not in user_columns:
                self.conn.execute(
                    "ALTER TABLE users ADD COLUMN daily_neg_gain INTEGER NOT NULL DEFAULT 0"
                )
            if "daily_bucket" not in user_columns:
                self.conn.execute("ALTER TABLE users ADD COLUMN daily_bucket TEXT")

            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS score_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_type TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    interaction_type TEXT NOT NULL,
                    intensity INTEGER NOT NULL,
                    raw_delta INTEGER NOT NULL,
                    final_delta INTEGER NOT NULL,
                    anti_spam_mul REAL NOT NULL,
                    created_at INTEGER NOT NULL,
                    evidence TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (session_type, session_id, user_id)
                        REFERENCES users(session_type, session_id, user_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_score_events_user_time
                ON score_events(session_type, session_id, user_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_score_events_type_time
                ON score_events(session_type, session_id, user_id, interaction_type, created_at);
                """
            )

            today_bucket = time.strftime("%Y-%m-%d", time.localtime())
            self.conn.execute(
                """
                UPDATE users
                SET
                    daily_pos_gain = COALESCE(daily_pos_gain, 0),
                    daily_neg_gain = COALESCE(daily_neg_gain, 0),
                    daily_bucket = COALESCE(daily_bucket, ?)
                """,
                (today_bucket,),
            )

            self.conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '3')"
            )
            self._apply_v3_compat_fixes()
            self.conn.commit()
        except Exception as exc:
            self.conn.rollback()
            raise SchemaMismatchError(f"v2 -> v3 迁移失败: {exc}") from exc

    def _apply_v3_compat_fixes(self):
        self._normalize_current_nicknames()
        self.conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_nick_current_unique
            ON nicknames(session_type, session_id, user_id)
            WHERE is_current = 1
            """
        )
        if not self._has_users_foreign_key("nicknames"):
            self._rebuild_nicknames_with_fk()
        if not self._has_users_foreign_key("score_events"):
            self._rebuild_score_events_with_fk()

    def _normalize_current_nicknames(self):
        duplicate_groups = self.conn.execute(
            """
            SELECT session_type, session_id, user_id
            FROM nicknames
            WHERE is_current = 1
            GROUP BY session_type, session_id, user_id
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        for session_type, session_id, user_id in duplicate_groups:
            rowids = self.conn.execute(
                """
                SELECT rowid
                FROM nicknames
                WHERE session_type = ?
                  AND session_id = ?
                  AND user_id = ?
                  AND is_current = 1
                ORDER BY created_at DESC, rowid DESC
                """,
                (session_type, session_id, user_id),
            ).fetchall()
            for row in rowids[1:]:
                self.conn.execute(
                    "UPDATE nicknames SET is_current = 0 WHERE rowid = ?", (row[0],)
                )

    def _rebuild_nicknames_with_fk(self):
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self.conn.execute(
                """
                DELETE FROM nicknames
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM users
                    WHERE users.session_type = nicknames.session_type
                      AND users.session_id = nicknames.session_id
                      AND users.user_id = nicknames.user_id
                )
                """
            )
            self.conn.execute("ALTER TABLE nicknames RENAME TO nicknames_old")
            self.conn.execute(
                """
                CREATE TABLE nicknames (
                    session_type TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY (session_type, session_id, user_id)
                        REFERENCES users(session_type, session_id, user_id) ON DELETE CASCADE,
                    UNIQUE(session_type, session_id, user_id, nickname)
                )
                """
            )
            self.conn.execute(
                """
                INSERT OR IGNORE INTO nicknames
                (session_type, session_id, user_id, nickname, is_current, created_at)
                SELECT session_type, session_id, user_id, nickname, is_current, created_at
                FROM nicknames_old
                ORDER BY created_at ASC, rowid ASC
                """
            )
            self.conn.execute("DROP TABLE nicknames_old")
            self.conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_nick_lookup
                ON nicknames(session_type, session_id, nickname, is_current);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_nick_current_unique
                ON nicknames(session_type, session_id, user_id)
                WHERE is_current = 1;
                """
            )
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

    def _rebuild_score_events_with_fk(self):
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self.conn.execute(
                """
                DELETE FROM score_events
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM users
                    WHERE users.session_type = score_events.session_type
                      AND users.session_id = score_events.session_id
                      AND users.user_id = score_events.user_id
                )
                """
            )
            self.conn.execute("ALTER TABLE score_events RENAME TO score_events_old")
            self.conn.execute(
                """
                CREATE TABLE score_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_type TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    interaction_type TEXT NOT NULL,
                    intensity INTEGER NOT NULL,
                    raw_delta INTEGER NOT NULL,
                    final_delta INTEGER NOT NULL,
                    anti_spam_mul REAL NOT NULL,
                    created_at INTEGER NOT NULL,
                    evidence TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (session_type, session_id, user_id)
                        REFERENCES users(session_type, session_id, user_id) ON DELETE CASCADE
                )
                """
            )
            self.conn.execute(
                """
                INSERT INTO score_events (
                    id,
                    session_type,
                    session_id,
                    user_id,
                    interaction_type,
                    intensity,
                    raw_delta,
                    final_delta,
                    anti_spam_mul,
                    created_at,
                    evidence
                )
                SELECT
                    id,
                    session_type,
                    session_id,
                    user_id,
                    interaction_type,
                    intensity,
                    raw_delta,
                    final_delta,
                    anti_spam_mul,
                    created_at,
                    evidence
                FROM score_events_old
                ORDER BY id ASC
                """
            )
            self.conn.execute("DROP TABLE score_events_old")
            self.conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_score_events_user_time
                ON score_events(session_type, session_id, user_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_score_events_type_time
                ON score_events(session_type, session_id, user_id, interaction_type, created_at);
                """
            )
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

    def _validate_schema(self):
        required_tables = {"meta", "users", "nicknames", "score_events"}
        existing_tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing_tables = required_tables - existing_tables
        if missing_tables:
            raise SchemaMismatchError(
                f"数据库缺少必要表: {', '.join(sorted(missing_tables))}。请删除旧数据库后重建。"
            )

        users_columns = self._get_columns("users")
        required_users_columns = {
            "session_type",
            "session_id",
            "user_id",
            "level",
            "last_interaction_at",
            "daily_pos_gain",
            "daily_neg_gain",
            "daily_bucket",
        }
        if not required_users_columns.issubset(users_columns):
            raise SchemaMismatchError("users 表结构不符合 v3 要求。请删除旧数据库后重建。")

        users_pk_columns = self._get_pk_columns("users")
        if users_pk_columns != ["session_type", "session_id", "user_id"]:
            raise SchemaMismatchError("users 主键不符合要求。请删除旧数据库后重建。")

        nick_columns = self._get_columns("nicknames")
        required_nick_columns = {
            "session_type",
            "session_id",
            "user_id",
            "nickname",
            "is_current",
            "created_at",
        }
        if not required_nick_columns.issubset(nick_columns):
            raise SchemaMismatchError(
                "nicknames 表结构不符合要求。请删除旧数据库后重建。"
            )

        if not self._has_unique_index(
            "nicknames",
            ["session_type", "session_id", "user_id", "nickname"],
        ):
            raise SchemaMismatchError(
                "nicknames 唯一约束不符合要求。请删除旧数据库后重建。"
            )

        if not self._has_index(
            "nicknames",
            "idx_nick_lookup",
            ["session_type", "session_id", "nickname", "is_current"],
        ):
            raise SchemaMismatchError(
                "nicknames 索引不符合要求。请删除旧数据库后重建。"
            )

        if not self._has_index(
            "nicknames",
            "idx_nick_current_unique",
            ["session_type", "session_id", "user_id"],
            require_unique=True,
        ):
            raise SchemaMismatchError(
                "nicknames 索引 idx_nick_current_unique 缺失或不匹配。"
            )

        if not self._has_users_foreign_key("nicknames"):
            raise SchemaMismatchError("nicknames 外键约束不符合要求。")

        score_columns = self._get_columns("score_events")
        required_score_columns = {
            "id",
            "session_type",
            "session_id",
            "user_id",
            "interaction_type",
            "intensity",
            "raw_delta",
            "final_delta",
            "anti_spam_mul",
            "created_at",
            "evidence",
        }
        if not required_score_columns.issubset(score_columns):
            raise SchemaMismatchError(
                "score_events 表结构不符合要求。请删除旧数据库后重建。"
            )

        if not self._has_index(
            "score_events",
            "idx_score_events_user_time",
            ["session_type", "session_id", "user_id", "created_at"],
        ):
            raise SchemaMismatchError(
                "score_events 索引 idx_score_events_user_time 缺失或不匹配。"
            )

        if not self._has_index(
            "score_events",
            "idx_score_events_type_time",
            [
                "session_type",
                "session_id",
                "user_id",
                "interaction_type",
                "created_at",
            ],
        ):
            raise SchemaMismatchError(
                "score_events 索引 idx_score_events_type_time 缺失或不匹配。"
            )

        if not self._has_users_foreign_key("score_events"):
            raise SchemaMismatchError("score_events 外键约束不符合要求。")

    def _get_columns(self, table_name: str) -> set[str]:
        rows = self.conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
        return {row[1] for row in rows}

    def _get_pk_columns(self, table_name: str) -> list[str]:
        rows = self.conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
        ordered = sorted((row[5], row[1]) for row in rows if row[5] > 0)
        return [name for _, name in ordered]

    def _has_unique_index(self, table_name: str, expected_columns: list[str]) -> bool:
        index_rows = self.conn.execute(f"PRAGMA index_list('{table_name}')").fetchall()
        for row in index_rows:
            index_name = row[1]
            is_unique = bool(row[2])
            if not is_unique:
                continue
            index_columns = [
                info[2]
                for info in self.conn.execute(
                    f"PRAGMA index_info('{index_name}')"
                ).fetchall()
            ]
            if index_columns == expected_columns:
                return True
        return False

    def _has_index(
        self,
        table_name: str,
        index_name: str,
        expected_columns: list[str],
        *,
        require_unique: bool = False,
    ) -> bool:
        index_rows = self.conn.execute(f"PRAGMA index_list('{table_name}')").fetchall()
        target_row = None
        for row in index_rows:
            if row[1] == index_name:
                target_row = row
                break
        if target_row is None:
            return False
        if require_unique and not bool(target_row[2]):
            return False
        index_columns = [
            info[2]
            for info in self.conn.execute(
                f"PRAGMA index_info('{index_name}')"
            ).fetchall()
        ]
        return index_columns == expected_columns

    def _has_users_foreign_key(self, table_name: str) -> bool:
        rows = self.conn.execute(f"PRAGMA foreign_key_list('{table_name}')").fetchall()
        if not rows:
            return False
        groups: dict[int, list[tuple]] = {}
        for row in rows:
            groups.setdefault(int(row[0]), []).append(row)
        for group_rows in groups.values():
            ordered = sorted(group_rows, key=lambda r: int(r[1]))
            ref_table = ordered[0][2]
            from_cols = [r[3] for r in ordered]
            to_cols = [r[4] for r in ordered]
            on_delete = ordered[0][6]
            if (
                ref_table == "users"
                and from_cols == ["session_type", "session_id", "user_id"]
                and to_cols == ["session_type", "session_id", "user_id"]
                and str(on_delete).upper() == "CASCADE"
            ):
                return True
        return False

    def add_user(
        self,
        session_type: str,
        session_id: str,
        user_id: str,
        level: int,
        last_interaction_at: Optional[int] = None,
        daily_pos_gain: int = 0,
        daily_neg_gain: int = 0,
        daily_bucket: Optional[str] = None,
        *,
        commit: bool = True,
    ) -> bool:
        """添加新用户，若已存在返回 False。"""
        if daily_bucket is None:
            daily_bucket = time.strftime("%Y-%m-%d", time.localtime())
        with self._lock:
            try:
                self.conn.execute(
                    """
                    INSERT INTO users (
                        session_type,
                        session_id,
                        user_id,
                        level,
                        last_interaction_at,
                        daily_pos_gain,
                        daily_neg_gain,
                        daily_bucket
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_type,
                        session_id,
                        user_id,
                        level,
                        last_interaction_at,
                        daily_pos_gain,
                        daily_neg_gain,
                        daily_bucket,
                    ),
                )
                if commit:
                    self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                if commit:
                    self.conn.rollback()
                return False

    def remove_user(self, session_type: str, session_id: str, user_id: str) -> bool:
        """删除用户及其所有昵称（CASCADE）。"""
        with self._lock:
            try:
                self.conn.execute(
                    """
                    DELETE FROM score_events
                    WHERE session_type = ? AND session_id = ? AND user_id = ?
                    """,
                    (session_type, session_id, user_id),
                )
                cur = self.conn.execute(
                    """
                    DELETE FROM users
                    WHERE session_type = ? AND session_id = ? AND user_id = ?
                    """,
                    (session_type, session_id, user_id),
                )
                self.conn.commit()
                return cur.rowcount > 0
            except Exception:
                self.conn.rollback()
                raise

    def get_user(
        self, session_type: str, session_id: str, user_id: str
    ) -> Optional[User]:
        """通过会话和用户 ID 查询用户。"""
        with self._lock:
            row = self.conn.execute(
                """
                SELECT
                    session_type,
                    session_id,
                    user_id,
                    level,
                    last_interaction_at,
                    daily_pos_gain,
                    daily_neg_gain,
                    daily_bucket
                FROM users
                WHERE session_type = ? AND session_id = ? AND user_id = ?
                """,
                (session_type, session_id, user_id),
            ).fetchone()
            if not row:
                return None
            return User(
                session_type=row[0],
                session_id=row[1],
                user_id=row[2],
                level=row[3],
                current_nickname=self.get_current_nickname(
                    session_type, session_id, user_id
                ),
                historical_nicknames=self.get_historical_nicknames(
                    session_type, session_id, user_id
                ),
                last_interaction_at=row[4],
                daily_pos_gain=row[5] or 0,
                daily_neg_gain=row[6] or 0,
                daily_bucket=row[7],
            )

    def get_ranking(
        self,
        session_type: str,
        session_id: str,
        limit: int,
        offset: int,
    ) -> tuple[list[User], int]:
        """按好感度降序返回分页用户列表和总数。"""
        with self._lock:
            total = self.conn.execute(
                "SELECT COUNT(*) FROM users WHERE session_type = ? AND session_id = ?",
                (session_type, session_id),
            ).fetchone()[0]

            rows = self.conn.execute(
                """
                SELECT u.user_id, u.level, n.nickname
                FROM users u
                LEFT JOIN nicknames n
                  ON u.session_type = n.session_type
                 AND u.session_id = n.session_id
                 AND u.user_id = n.user_id
                 AND n.is_current = 1
                WHERE u.session_type = ? AND u.session_id = ?
                ORDER BY u.level DESC, u.user_id ASC
                LIMIT ? OFFSET ?
                """,
                (session_type, session_id, limit, offset),
            ).fetchall()

            users = [
                User(
                    session_type=session_type,
                    session_id=session_id,
                    user_id=row[0],
                    level=row[1],
                    current_nickname=row[2],
                )
                for row in rows
            ]
            return users, total

    def find_user_by_current_nickname(
        self, session_type: str, session_id: str, nickname: str
    ) -> Optional[User]:
        """通过当前昵称查找用户（仅当前会话）。"""
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT u.user_id
                FROM users u
                JOIN nicknames n
                  ON u.session_type = n.session_type
                 AND u.session_id = n.session_id
                 AND u.user_id = n.user_id
                WHERE u.session_type = ?
                  AND u.session_id = ?
                  AND n.nickname = ?
                  AND n.is_current = 1
                """,
                (session_type, session_id, nickname),
            ).fetchall()

            if not rows:
                return None

            if len(rows) > 1:
                user_ids = sorted({row[0] for row in rows})
                raise NicknameAmbiguousError(nickname=nickname, user_ids=user_ids)

            return self.get_user(session_type, session_id, rows[0][0])

    def update_level(
        self,
        session_type: str,
        session_id: str,
        user_id: str,
        level: int,
        *,
        last_interaction_at: Optional[int] = None,
        daily_pos_gain: Optional[int] = None,
        daily_neg_gain: Optional[int] = None,
        daily_bucket: Optional[str] = None,
        commit: bool = True,
    ) -> bool:
        """更新好感度等级，可选更新行为统计字段。"""
        sets = ["level = ?"]
        params: list[object] = [level]

        if last_interaction_at is not None:
            sets.append("last_interaction_at = ?")
            params.append(last_interaction_at)
        if daily_pos_gain is not None:
            sets.append("daily_pos_gain = ?")
            params.append(daily_pos_gain)
        if daily_neg_gain is not None:
            sets.append("daily_neg_gain = ?")
            params.append(daily_neg_gain)
        if daily_bucket is not None:
            sets.append("daily_bucket = ?")
            params.append(daily_bucket)

        params.extend([session_type, session_id, user_id])
        with self._lock:
            cur = self.conn.execute(
                f"""
                UPDATE users
                SET {', '.join(sets)}
                WHERE session_type = ? AND session_id = ? AND user_id = ?
                """,
                tuple(params),
            )
            if commit:
                self.conn.commit()
            return cur.rowcount > 0

    def upsert_current_nickname(
        self,
        session_type: str,
        session_id: str,
        user_id: str,
        nickname: str,
        *,
        commit: bool = True,
    ) -> bool:
        """设置当前昵称，旧当前昵称会转为曾用名。"""
        nickname = nickname.strip()
        if not nickname:
            return False

        with self._lock:
            try:
                now = int(time.time())
                self.conn.execute(
                    """
                    UPDATE nicknames
                    SET is_current = 0
                    WHERE session_type = ? AND session_id = ? AND user_id = ? AND is_current = 1
                    """,
                    (session_type, session_id, user_id),
                )

                existed = self.conn.execute(
                    """
                    SELECT 1
                    FROM nicknames
                    WHERE session_type = ? AND session_id = ? AND user_id = ? AND nickname = ?
                    """,
                    (session_type, session_id, user_id, nickname),
                ).fetchone()

                if existed:
                    self.conn.execute(
                        """
                        UPDATE nicknames
                        SET is_current = 1
                        WHERE session_type = ? AND session_id = ? AND user_id = ? AND nickname = ?
                        """,
                        (session_type, session_id, user_id, nickname),
                    )
                else:
                    self.conn.execute(
                        """
                        INSERT INTO nicknames
                        (session_type, session_id, user_id, nickname, is_current, created_at)
                        VALUES (?, ?, ?, ?, 1, ?)
                        """,
                        (session_type, session_id, user_id, nickname, now),
                    )

                if commit:
                    self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                if commit:
                    self.conn.rollback()
                return False

    def remove_current_nickname(
        self,
        session_type: str,
        session_id: str,
        user_id: str,
        nickname: str,
        *,
        commit: bool = True,
    ) -> bool:
        """删除当前昵称（不会删除其他曾用名）。"""
        with self._lock:
            cur = self.conn.execute(
                """
                DELETE FROM nicknames
                WHERE session_type = ?
                  AND session_id = ?
                  AND user_id = ?
                  AND nickname = ?
                  AND is_current = 1
                """,
                (session_type, session_id, user_id, nickname),
            )
            if commit:
                self.conn.commit()
            return cur.rowcount > 0

    def get_current_nickname(
        self, session_type: str, session_id: str, user_id: str
    ) -> Optional[str]:
        """获取用户当前昵称。"""
        with self._lock:
            row = self.conn.execute(
                """
                SELECT nickname
                FROM nicknames
                WHERE session_type = ?
                  AND session_id = ?
                  AND user_id = ?
                  AND is_current = 1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (session_type, session_id, user_id),
            ).fetchone()
            if not row:
                return None
            return row[0]

    def get_historical_nicknames(
        self, session_type: str, session_id: str, user_id: str
    ) -> list[str]:
        """获取用户曾用名（不含当前昵称）。"""
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT nickname
                FROM nicknames
                WHERE session_type = ?
                  AND session_id = ?
                  AND user_id = ?
                  AND is_current = 0
                ORDER BY created_at DESC
                """,
                (session_type, session_id, user_id),
            ).fetchall()
            return [r[0] for r in rows]

    def ensure_current_nickname(
        self, session_type: str, session_id: str, user_id: str, fallback_nickname: str
    ) -> bool:
        """如果当前昵称不存在，则尝试使用回退昵称补齐。"""
        current = self.get_current_nickname(session_type, session_id, user_id)
        if current:
            return True
        return self.upsert_current_nickname(
            session_type, session_id, user_id, fallback_nickname
        )

    def log_score_event(
        self,
        session_type: str,
        session_id: str,
        user_id: str,
        interaction_type: str,
        intensity: int,
        raw_delta: int,
        final_delta: int,
        anti_spam_mul: float,
        created_at: int,
        evidence: str,
        *,
        commit: bool = True,
    ):
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO score_events (
                    session_type,
                    session_id,
                    user_id,
                    interaction_type,
                    intensity,
                    raw_delta,
                    final_delta,
                    anti_spam_mul,
                    created_at,
                    evidence
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_type,
                    session_id,
                    user_id,
                    interaction_type,
                    intensity,
                    raw_delta,
                    final_delta,
                    anti_spam_mul,
                    created_at,
                    evidence,
                ),
            )
            if commit:
                self.conn.commit()

    def count_positive_events_by_type_since(
        self,
        session_type: str,
        session_id: str,
        user_id: str,
        interaction_type: str,
        since_ts: int,
    ) -> int:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT COUNT(*)
                FROM score_events
                WHERE session_type = ?
                  AND session_id = ?
                  AND user_id = ?
                  AND interaction_type = ?
                  AND final_delta > 0
                  AND created_at >= ?
                """,
                (session_type, session_id, user_id, interaction_type, since_ts),
            ).fetchone()
            return int(row[0] if row and row[0] is not None else 0)

    def sum_positive_delta_since(
        self,
        session_type: str,
        session_id: str,
        user_id: str,
        since_ts: int,
    ) -> int:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT COALESCE(SUM(final_delta), 0)
                FROM score_events
                WHERE session_type = ?
                  AND session_id = ?
                  AND user_id = ?
                  AND final_delta > 0
                  AND created_at >= ?
                """,
                (session_type, session_id, user_id, since_ts),
            ).fetchone()
            return int(row[0] if row and row[0] is not None else 0)

    def close(self):
        with self._lock:
            self.conn.close()
