"""长期记忆存储（阶段 5）：SQLite，单文件、零额外依赖。

设计约束：
  - 明确区分"用户陈述"（source=user）与"模型推断"（source=inferred）；
    模型只能通过需确认的 save_memory 工具写入，且必须标记来源；
  - 记忆可查看、修改、删除、导出；
  - 删除即不再参与任何决策（物理删除，保留操作审计日志）；
  - 线程安全：工具在执行器线程池中运行，主线程也会读写。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_KINDS = ("preference", "fact")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class MemoryStore:
    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK (kind IN ('preference', 'fact')),
                    content TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'user' CHECK (source IN ('user', 'inferred')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    user_text TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    steps_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proactive_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('sensor_threshold', 'device_offline')),
                    params_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    cooldown_seconds INTEGER NOT NULL DEFAULT 300,
                    state_json TEXT NOT NULL DEFAULT '{}',
                    last_fired_at TEXT
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    rule_id INTEGER,
                    rule_name TEXT,
                    level TEXT NOT NULL DEFAULT 'info',
                    message TEXT NOT NULL,
                    read INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------- 记忆 CRUD

    def add(self, kind: str, content: str, source: str = "user") -> int:
        if kind not in _KINDS:
            raise ValueError(f"记忆类型只能是 {'/'.join(_KINDS)}")
        content = (content or "").strip()
        if not content:
            raise ValueError("记忆内容不能为空")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO memories (kind, content, source, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (kind, content, source, _now(), _now()),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def list(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "SELECT id, kind, content, source, created_at, updated_at FROM memories"
        params: tuple = ()
        if kind:
            if kind not in _KINDS:
                raise ValueError(f"记忆类型只能是 {'/'.join(_KINDS)}")
            query += " WHERE kind = ?"
            params = (kind,)
        query += " ORDER BY updated_at DESC, id DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def update(self, memory_id: int, content: str) -> bool:
        content = (content or "").strip()
        if not content:
            raise ValueError("记忆内容不能为空")
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
                (content, _now(), int(memory_id)),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def delete(self, memory_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM memories WHERE id = ?", (int(memory_id),)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def export(self) -> Dict[str, Any]:
        return {
            "exported_at": _now(),
            "memories": self.list(),
            "task_logs": self.list_task_logs(),
        }

    # ------------------------------------------------------------ 提示词注入

    def prompt_block(self, max_items: int = 20) -> str:
        """生成注入系统提示词的记忆摘要块。"""
        memories = self.list()[: max_items]
        if not memories:
            return "（暂无已保存的长期记忆）"
        lines = []
        for m in memories:
            tag = {"preference": "偏好", "fact": "事实"}[m["kind"]]
            lines.append(f"  - [{tag}] #{m['id']} {m['content']}")
        return "\n".join(lines)

    # -------------------------------------------------------------- 任务日志

    def add_task_log(self, user_text: str, outcome: str, steps: List[Dict[str, Any]]) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO task_logs (created_at, user_text, outcome, steps_json)"
                " VALUES (?, ?, ?, ?)",
                (_now(), user_text, outcome, json.dumps(steps, ensure_ascii=False)),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def list_task_logs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at, user_text, outcome, steps_json FROM task_logs"
                " ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["steps"] = json.loads(item.pop("steps_json"))
            except ValueError:
                item["steps"] = []
            result.append(item)
        return result

    # ---------------------------------------------------------- 主动服务规则

    def add_rule(self, name: str, kind: str, params: Dict[str, Any], cooldown_seconds: int = 300) -> int:
        if kind not in ("sensor_threshold", "device_offline"):
            raise ValueError("规则类型只能是 sensor_threshold / device_offline")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO proactive_rules (name, kind, params_json, enabled, cooldown_seconds)"
                " VALUES (?, ?, ?, 1, ?)",
                (name, kind, json.dumps(params, ensure_ascii=False), int(cooldown_seconds)),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def list_rules(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, kind, params_json, enabled, cooldown_seconds, last_fired_at"
                " FROM proactive_rules ORDER BY id"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["params"] = json.loads(item.pop("params_json"))
            except ValueError:
                item["params"] = {}
            item["enabled"] = bool(item["enabled"])
            result.append(item)
        return result

    def set_rule_enabled(self, rule_id: int, enabled: bool) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE proactive_rules SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, int(rule_id)),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def update_rule_state(self, rule_id: int, state: Dict[str, Any], last_fired: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE proactive_rules SET state_json = ?, last_fired_at = ? WHERE id = ?",
                (
                    json.dumps(state, ensure_ascii=False),
                    _now() if last_fired else None,
                    int(rule_id),
                ),
            )
            self._conn.commit()

    # ------------------------------------------------------------------ 通知

    def add_notification(self, rule_id: Optional[int], rule_name: str, message: str, level: str = "info") -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO notifications (created_at, rule_id, rule_name, level, message)"
                " VALUES (?, ?, ?, ?, ?)",
                (_now(), rule_id, rule_name, level, message),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def list_notifications(self, unread_only: bool = False, limit: int = 50) -> List[Dict[str, Any]]:
        query = (
            "SELECT id, created_at, rule_id, rule_name, level, message, read"
            " FROM notifications"
        )
        if unread_only:
            query += " WHERE read = 0"
        query += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, (int(limit),)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["read"] = bool(item["read"])
            result.append(item)
        return result

    def mark_notifications_read(self) -> int:
        with self._lock:
            cursor = self._conn.execute("UPDATE notifications SET read = 1 WHERE read = 0")
            self._conn.commit()
            return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
