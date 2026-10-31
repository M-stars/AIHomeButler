"""家庭生活服务存储（阶段 14.7）：SQLite 单文件、零额外依赖、线程安全。

范围：食材（pantry）、衣物（wardrobe）、穿着记录与穿搭反馈。
与 memory.db 分库：这些是"家庭物品台账"，增长快、可整体重建，
与长期记忆（低频、高价值、需导出审阅）生命周期不同。

设计约束：
  - 工具在执行器线程池中运行，主线程/调度线程并发读写，因此沿用
    MemoryStore 的 RLock + WAL 模式；
  - 写入方传日期字符串（YYYY-MM-DD），本层不做时钟假设，便于测试注入；
  - 名称归一化（name_key）用于去重/重入库判定：去首尾空白、压缩内部空白、小写；
  - 状态流转不在此层判断（那是 pantry/wardrobe 业务模块的事）。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional


def normalize_name(name: str) -> str:
    """名称归一化：压缩空白 + 小写。中文不受影响，仅消除书写差异。"""
    return " ".join(str(name or "").split()).lower()


def _now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


class HouseholdStore:
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
                CREATE TABLE IF NOT EXISTS food_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    name_key TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT '其他',
                    quantity TEXT NOT NULL DEFAULT '',
                    expires_in_days INTEGER,
                    added_date TEXT NOT NULL,
                    last_remind_date TEXT,
                    notes TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'in_stock'
                        CHECK (status IN ('in_stock', 'used_up', 'discarded')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS food_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS garments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT NOT NULL,
                    name TEXT NOT NULL,
                    name_key TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT '其他',
                    warmth TEXT NOT NULL DEFAULT '适中',
                    wash_interval_days INTEGER,
                    status TEXT NOT NULL DEFAULT 'clean'
                        CHECK (status IN ('clean', 'in_laundry', 'retired')),
                    times_worn_since_wash INTEGER NOT NULL DEFAULT 0,
                    last_worn_date TEXT,
                    last_wash_date TEXT,
                    last_remind_date TEXT,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wear_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    garment_id INTEGER NOT NULL,
                    owner TEXT NOT NULL,
                    worn_on TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outfit_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT NOT NULL,
                    garment_id INTEGER,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    # ---------------------------------------------------------------- 食材

    def add_food(
        self,
        name: str,
        category: str,
        quantity: str,
        expires_in_days: Optional[int],
        notes: str,
        added_date: str,
    ) -> int:
        name = (name or "").strip()
        if not name:
            raise ValueError("食材名称不能为空")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO food_items (name, name_key, category, quantity, expires_in_days,"
                " added_date, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    normalize_name(name),
                    category or "其他",
                    (quantity or "").strip(),
                    expires_in_days,
                    added_date,
                    (notes or "").strip(),
                    _now(),
                    _now(),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def get_food(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM food_items WHERE id = ?", (int(item_id),)
            ).fetchone()
        return dict(row) if row else None

    def find_food_in_stock(self, name_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM food_items WHERE name_key = ? AND status = 'in_stock'"
                " ORDER BY id DESC LIMIT 1",
                (name_key,),
            ).fetchone()
        return dict(row) if row else None

    _FOOD_FIELDS = ("category", "quantity", "expires_in_days", "added_date",
                    "last_remind_date", "notes", "status")

    def update_food(self, item_id: int, **fields) -> bool:
        """按白名单更新食材字段；空字段集为编程错误，直接抛出。"""
        unknown = [k for k in fields if k not in self._FOOD_FIELDS]
        if unknown:
            raise ValueError(f"不允许更新食材字段：{unknown}")
        if not fields:
            raise ValueError("update_food 需要至少一个字段")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE food_items SET {sets}, updated_at = ? WHERE id = ?",
                (*fields.values(), _now(), int(item_id)),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def list_food(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "SELECT * FROM food_items"
        params: List[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def log_food_event(self, item_id: int, action: str, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO food_events (item_id, action, detail, created_at)"
                " VALUES (?, ?, ?, ?)",
                (int(item_id), action, (detail or "")[:300], _now()),
            )
            self._conn.commit()

    def list_food_events(self, item_id: Optional[int] = None,
                         limit: int = 50) -> List[Dict[str, Any]]:
        query = "SELECT id, item_id, action, detail, created_at FROM food_events"
        params: List[Any] = []
        if item_id is not None:
            query += " WHERE item_id = ?"
            params.append(int(item_id))
        query += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # ---------------------------------------------------------------- 衣物

    def add_garment(
        self,
        owner: str,
        name: str,
        category: str,
        warmth: str,
        wash_interval_days: Optional[int],
        notes: str,
    ) -> int:
        owner = (owner or "").strip()
        name = (name or "").strip()
        if not owner:
            raise ValueError("衣物归属成员不能为空")
        if not name:
            raise ValueError("衣物名称不能为空")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO garments (owner, name, name_key, category, warmth,"
                " wash_interval_days, notes, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    owner,
                    name,
                    normalize_name(name),
                    category or "其他",
                    warmth or "适中",
                    wash_interval_days,
                    (notes or "").strip(),
                    _now(),
                    _now(),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def get_garment(self, garment_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM garments WHERE id = ?", (int(garment_id),)
            ).fetchone()
        return dict(row) if row else None

    def find_garment(self, owner: str, name_key: str) -> Optional[Dict[str, Any]]:
        """按（成员，名称）找未退役衣物；同名校验取最近创建的一件。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM garments WHERE owner = ? AND name_key = ?"
                " AND status != 'retired' ORDER BY id DESC LIMIT 1",
                (owner, name_key),
            ).fetchone()
        return dict(row) if row else None

    _GARMENT_FIELDS = ("category", "warmth", "wash_interval_days", "status",
                       "times_worn_since_wash", "last_worn_date", "last_wash_date",
                       "last_remind_date", "notes")

    def update_garment(self, garment_id: int, **fields) -> bool:
        unknown = [k for k in fields if k not in self._GARMENT_FIELDS]
        if unknown:
            raise ValueError(f"不允许更新衣物字段：{unknown}")
        if not fields:
            raise ValueError("update_garment 需要至少一个字段")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE garments SET {sets}, updated_at = ? WHERE id = ?",
                (*fields.values(), _now(), int(garment_id)),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def list_garments(self, owner: Optional[str] = None,
                      status: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "SELECT * FROM garments"
        clauses: List[str] = []
        params: List[Any] = []
        if owner:
            clauses.append("owner = ?")
            params.append(owner)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY owner, category, id"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # ---------------------------------------------------------- 穿着与反馈

    def has_wear_event(self, garment_id: int, worn_on: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM wear_events WHERE garment_id = ? AND worn_on = ? LIMIT 1",
                (int(garment_id), worn_on),
            ).fetchone()
        return row is not None

    def add_wear_event(self, garment_id: int, owner: str, worn_on: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO wear_events (garment_id, owner, worn_on, created_at)"
                " VALUES (?, ?, ?, ?)",
                (int(garment_id), owner, worn_on, _now()),
            )
            self._conn.commit()

    def list_wear_events(self, garment_id: Optional[int] = None,
                         limit: int = 100) -> List[Dict[str, Any]]:
        query = "SELECT id, garment_id, owner, worn_on, created_at FROM wear_events"
        params: List[Any] = []
        if garment_id is not None:
            query += " WHERE garment_id = ?"
            params.append(int(garment_id))
        query += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def add_outfit_feedback(self, owner: str, content: str,
                            garment_id: Optional[int] = None) -> int:
        content = (content or "").strip()
        if not content:
            raise ValueError("反馈内容不能为空")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO outfit_feedback (owner, garment_id, content, created_at)"
                " VALUES (?, ?, ?, ?)",
                (owner, garment_id, content, _now()),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def list_outfit_feedback(self, owner: Optional[str] = None,
                             limit: int = 10) -> List[Dict[str, Any]]:
        query = "SELECT id, owner, garment_id, content, created_at FROM outfit_feedback"
        params: List[Any] = []
        if owner:
            query += " WHERE owner = ?"
            params.append(owner)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
