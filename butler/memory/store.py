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
                    member_id TEXT,
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
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT 'system',
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS token_usage (
                    day TEXT PRIMARY KEY,
                    used_tokens INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            self._conn.commit()
            # 阶段 13 迁移：老库补 member_id 列（可空=共享记忆），重复执行忽略
            try:
                with self._lock:
                    self._conn.execute("ALTER TABLE memories ADD COLUMN member_id TEXT")
                    self._conn.commit()
            except sqlite3.OperationalError:
                pass

    # ------------------------------------------------------------- 记忆 CRUD

    def add(self, kind: str, content: str, source: str = "user",
            member_id: Optional[str] = None) -> int:
        """新增记忆。member_id 非空表示该成员的个人记忆；None=家庭共享。"""
        if kind not in _KINDS:
            raise ValueError(f"记忆类型只能是 {'/'.join(_KINDS)}")
        content = (content or "").strip()
        if not content:
            raise ValueError("记忆内容不能为空")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO memories (kind, content, source, member_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (kind, content, source, member_id, _now(), _now()),
            )
            self._conn.commit()
            memory_id = int(cursor.lastrowid)
        self.add_audit("memory.add", f"#{memory_id} [{kind}] {content[:200]}", "ok")
        return memory_id

    def list(self, kind: Optional[str] = None,
             for_member_id: Optional[str] = None,
             shared_only: bool = False) -> List[Dict[str, Any]]:
        """列出记忆。三种作用域：
        - for_member_id 给定：该成员的个人记忆 + 家庭共享记忆；
        - shared_only=True：仅共享记忆（未识别说话人时 Agent 的视图，
          陌生人不能翻看任何成员的个人记忆）；
        - 都不给：全部（管理视图，Web 记忆管理/CLI /memory 用）。"""
        query = ("SELECT id, kind, content, source, member_id, created_at, updated_at"
                 " FROM memories")
        clauses: List[str] = []
        params: List[Any] = []
        if kind:
            if kind not in _KINDS:
                raise ValueError(f"记忆类型只能是 {'/'.join(_KINDS)}")
            clauses.append("kind = ?")
            params.append(kind)
        if for_member_id:
            clauses.append("(member_id IS NULL OR member_id = ?)")
            params.append(for_member_id)
        elif shared_only:
            clauses.append("member_id IS NULL")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
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
            changed = cursor.rowcount > 0
        if changed:
            self.add_audit("memory.update", f"#{int(memory_id)} → {content[:200]}", "ok")
        return changed

    def delete(self, memory_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM memories WHERE id = ?", (int(memory_id),)
            )
            self._conn.commit()
            deleted = cursor.rowcount > 0
        if deleted:
            self.add_audit("memory.delete", f"#{int(memory_id)}（物理删除，不再参与决策）", "ok")
        return deleted

    def export(self) -> Dict[str, Any]:
        return {
            "exported_at": _now(),
            "memories": self.list(),
            "task_logs": self.list_task_logs(),
        }

    # ------------------------------------------------------------ 提示词注入

    def prompt_block(self, max_items: int = 20,
                     for_member_id: Optional[str] = None,
                     member_name: Optional[str] = None,
                     shared_only: bool = False) -> str:
        """生成注入系统提示词的记忆摘要块。

        for_member_id 给定时只注入"该成员的个人记忆 + 共享记忆"，
        个人记忆单独打标（成员名）；shared_only=True 仅注入共享记忆。
        """
        memories = self.list(for_member_id=for_member_id,
                             shared_only=shared_only)[: max_items]
        if not memories:
            return "（暂无已保存的长期记忆）"
        lines = []
        for m in memories:
            tag = {"preference": "偏好", "fact": "事实"}[m["kind"]]
            owner = ""
            if for_member_id and m.get("member_id"):
                owner = f"【{member_name or '个人'}】"
            elif for_member_id:
                owner = "【共享】"
            lines.append(f"  - [{tag}]{owner} #{m['id']} {m['content']}")
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

    # ------------------------------------------------------------------ 审计日志

    _AUDIT_RETENTION = 2000

    def add_audit(self, action: str, detail: str = "", status: str = "",
                  actor: str = "system") -> int:
        """追加一条审计记录。actor: system=存储层自动记录 / agent=模型工具调用 /
        user=Web 界面操作。超量历史在写入时按保留上限裁剪。"""
        action = (action or "").strip()
        if not action:
            raise ValueError("审计 action 不能为空")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO audit_log (created_at, actor, action, detail, status)"
                " VALUES (?, ?, ?, ?, ?)",
                (_now(), actor, action, (detail or "")[:500], (status or "")[:50]),
            )
            self._conn.execute(
                "DELETE FROM audit_log WHERE id <= ("
                " SELECT id FROM audit_log ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (self._AUDIT_RETENTION,),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def list_audit(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at, actor, action, detail, status FROM audit_log"
                " ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------- token 用量

    def add_tokens_used(self, total_tokens: int) -> int:
        """累计当日 token 用量（按本地日期分桶），返回累计后的当日总量。"""
        total_tokens = max(0, int(total_tokens))
        day = _now()[:10]
        with self._lock:
            self._conn.execute(
                "INSERT INTO token_usage (day, used_tokens) VALUES (?, ?)"
                " ON CONFLICT(day) DO UPDATE SET used_tokens = used_tokens + ?",
                (day, total_tokens, total_tokens),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT used_tokens FROM token_usage WHERE day = ?", (day,)
            ).fetchone()
        return int(row["used_tokens"]) if row else 0

    def tokens_used_today(self) -> int:
        day = _now()[:10]
        with self._lock:
            row = self._conn.execute(
                "SELECT used_tokens FROM token_usage WHERE day = ?", (day,)
            ).fetchone()
        return int(row["used_tokens"]) if row else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
