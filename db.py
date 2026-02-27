import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

SCHEMA_VERSION = 2


class SchemaMismatchError(RuntimeError):
    """数据库结构不符合当前版本要求。"""


class NicknameAmbiguousError(RuntimeError):
    """按昵称查询命中了多个用户。"""

    def __init__(self, nickname: str, user_ids: list[str]):
        self.nickname = nickname
        self.user_ids = user_ids
        super().__init__(f"昵称「{nickname}」在当前会话中对应多个用户: {', '.join(user_ids)}")


@dataclass
class User:
    session_type: str
    session_id: str
    user_id: str
    level: int
    current_nickname: Optional[str] = None
    historical_nicknames: list[str] = field(default_factory=list)


class FavorabilityDB:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        try:
            self._init_tables()
        except Exception:
            self.conn.close()
            raise

    def _init_tables(self):
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

        if version != SCHEMA_VERSION:
            raise SchemaMismatchError(
                f"数据库 schema_version={version}，当前插件要求 {SCHEMA_VERSION}。请删除旧数据库后重建。"
            )

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
            """
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def _validate_schema(self):
        required_tables = {"meta", "users", "nicknames"}
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
        required_users_columns = {"session_type", "session_id", "user_id", "level"}
        if users_columns != required_users_columns:
            raise SchemaMismatchError("users 表结构不符合要求。请删除旧数据库后重建。")

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
        if nick_columns != required_nick_columns:
            raise SchemaMismatchError("nicknames 表结构不符合要求。请删除旧数据库后重建。")

        if not self._has_unique_index(
            "nicknames",
            ["session_type", "session_id", "user_id", "nickname"],
        ):
            raise SchemaMismatchError("nicknames 唯一约束不符合要求。请删除旧数据库后重建。")

        if not self._has_index(
            "nicknames",
            "idx_nick_lookup",
            ["session_type", "session_id", "nickname", "is_current"],
        ):
            raise SchemaMismatchError("nicknames 索引不符合要求。请删除旧数据库后重建。")

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
        self, table_name: str, index_name: str, expected_columns: list[str]
    ) -> bool:
        index_rows = self.conn.execute(f"PRAGMA index_list('{table_name}')").fetchall()
        names = {row[1] for row in index_rows}
        if index_name not in names:
            return False
        index_columns = [
            info[2]
            for info in self.conn.execute(f"PRAGMA index_info('{index_name}')").fetchall()
        ]
        return index_columns == expected_columns

    def add_user(
        self,
        session_type: str,
        session_id: str,
        user_id: str,
        level: int,
    ) -> bool:
        """添加新用户，若已存在返回 False。"""
        try:
            self.conn.execute(
                """
                INSERT INTO users (session_type, session_id, user_id, level)
                VALUES (?, ?, ?, ?)
                """,
                (session_type, session_id, user_id, level),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_user(self, session_type: str, session_id: str, user_id: str) -> bool:
        """删除用户及其所有昵称（CASCADE）。"""
        cur = self.conn.execute(
            """
            DELETE FROM users
            WHERE session_type = ? AND session_id = ? AND user_id = ?
            """,
            (session_type, session_id, user_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_user(self, session_type: str, session_id: str, user_id: str) -> Optional[User]:
        """通过会话和用户 ID 查询用户。"""
        row = self.conn.execute(
            """
            SELECT session_type, session_id, user_id, level
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
            current_nickname=self.get_current_nickname(session_type, session_id, user_id),
            historical_nicknames=self.get_historical_nicknames(
                session_type, session_id, user_id
            ),
        )

    def find_user_by_current_nickname(
        self, session_type: str, session_id: str, nickname: str
    ) -> Optional[User]:
        """通过当前昵称查找用户（仅当前会话）。"""
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
        self, session_type: str, session_id: str, user_id: str, level: int
    ) -> bool:
        """更新好感度等级。"""
        cur = self.conn.execute(
            """
            UPDATE users
            SET level = ?
            WHERE session_type = ? AND session_id = ? AND user_id = ?
            """,
            (level, session_type, session_id, user_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def upsert_current_nickname(
        self, session_type: str, session_id: str, user_id: str, nickname: str
    ) -> bool:
        """设置当前昵称，旧当前昵称会转为曾用名。"""
        nickname = nickname.strip()
        if not nickname:
            return False

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

            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return False

    def remove_current_nickname(
        self, session_type: str, session_id: str, user_id: str, nickname: str
    ) -> bool:
        """删除当前昵称（不会删除其他曾用名）。"""
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
        self.conn.commit()
        return cur.rowcount > 0

    def get_current_nickname(
        self, session_type: str, session_id: str, user_id: str
    ) -> Optional[str]:
        """获取用户当前昵称。"""
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

    def close(self):
        self.conn.close()
