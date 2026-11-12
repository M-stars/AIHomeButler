"""食材管家（阶段 14.7）：家庭食材台账 + 保质期跟踪 + 用掉/丢弃登记。

设计约束：
  - 入库批量进行（一句话报一堆菜），MEDIUM 风险需确认——模型转写可能出错，
    用户在确认界面看到完整清单后批准；重复入库 = 补货（重置入库日期）；
  - 保质期跟踪是确定性日期计算（入库日期 + 保质期天数），无 LLM 参与；
    保质期默认值按类目兜底，模型应尽量显式给出；
  - "吃什么"的推荐本身由模型基于 list_food_items 的真实台账推理，
    本模块不生成菜谱，也不假装知道冰箱里有什么。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from ..devices.base import DeviceError
from ..tools.base import RISK_LOW, RISK_MEDIUM, ToolSpec
from .store import HouseholdStore, normalize_name

FOOD_CATEGORIES = ("蔬菜", "水果", "肉禽蛋", "海鲜", "乳品", "主食",
                   "冷冻", "调料", "零食", "其他")

# 类目缺省保质期（天）：仅当模型/用户没有显式给出 expires_in_days 时兜底。
# 保守取短——宁可提前提醒也不让东西放到坏。
CATEGORY_EXPIRY_DAYS = {
    "蔬菜": 7, "水果": 10, "肉禽蛋": 5, "海鲜": 3, "乳品": 14,
    "主食": 180, "冷冻": 90, "调料": 365, "零食": 180, "其他": 14,
}

_MAX_BATCH = 20


def default_expiry_days(category: str) -> int:
    return CATEGORY_EXPIRY_DAYS.get(category or "其他", CATEGORY_EXPIRY_DAYS["其他"])


def days_left(item: Dict[str, Any], today: date) -> Optional[int]:
    """距过期天数：负数=已过期，None=未设保质期（不跟踪）。"""
    expires_in_days = item.get("expires_in_days")
    if expires_in_days is None:
        return None
    try:
        added = date.fromisoformat(str(item["added_date"])[:10])
        shelf = int(expires_in_days)
    except (ValueError, TypeError):
        return None
    return shelf - (today - added).days


def evaluate_expiry(item: Dict[str, Any], today: date,
                    warn_days: int = 2) -> Optional[Dict[str, Any]]:
    """单个食材的到期信号。返回 None=无需提醒；否则带 level/message。"""
    left = days_left(item, today)
    if left is None:
        return None
    name = item.get("name", f"#{item.get('id')}")
    quantity = (item.get("quantity") or "").strip()
    suffix = f"（{quantity}）" if quantity else ""
    if left < 0:
        return {
            "level": "warning",
            "item_id": item["id"],
            "name": name,
            "message": f"食材「{name}」{suffix}已过保质期 {-left} 天"
                       f"（入库 {item.get('added_date')}），建议检查处理",
        }
    if left <= warn_days:
        when = "今天到期" if left == 0 else f"还剩 {left} 天"
        return {
            "level": "info",
            "item_id": item["id"],
            "name": name,
            "message": f"食材「{name}」{suffix}{when}，建议优先吃掉",
        }
    return None


def _validate_item_payload(raw: Any, index: int) -> Dict[str, Any]:
    """批量入库的单项校验：schema 只保证顶层数组类型，内容在这里管。"""
    if not isinstance(raw, dict):
        raise DeviceError(f"第 {index + 1} 项食材必须是对象")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise DeviceError(f"第 {index + 1} 项食材缺少名称")
    category = str(raw.get("category") or "").strip()
    if category not in FOOD_CATEGORIES:
        # 用户口语类目（"干货""半成品"）不硬拒：归入"其他"并保留在备注里
        category = "其他"
    expires = raw.get("expires_in_days")
    if expires is not None:
        if isinstance(expires, bool) or not isinstance(expires, int) or not 0 <= expires <= 3650:
            raise DeviceError(
                f"「{name}」的 expires_in_days 需要是 0-3650 的整数（天）"
            )
    else:
        expires = default_expiry_days(category)
    return {
        "name": name,
        "category": category,
        "quantity": str(raw.get("quantity") or "").strip(),
        "expires_in_days": expires,
        "notes": str(raw.get("notes") or "").strip(),
    }


def build_pantry_tools(store: HouseholdStore) -> List[ToolSpec]:
    def _add_food_items(args: Dict) -> Dict:
        raw_items = args["items"]
        if not isinstance(raw_items, list) or not raw_items:
            raise DeviceError("items 需要至少一个食材")
        if len(raw_items) > _MAX_BATCH:
            raise DeviceError(f"一次最多入库 {_MAX_BATCH} 项食材")
        today = date.today().isoformat()
        results = []
        for index, raw in enumerate(raw_items):
            item = _validate_item_payload(raw, index)
            name_key = normalize_name(item["name"])
            existing = store.find_food_in_stock(name_key)
            if existing is not None:
                fields: Dict[str, Any] = {"added_date": today}
                if item["quantity"]:
                    fields["quantity"] = item["quantity"]
                fields["expires_in_days"] = item["expires_in_days"]
                if item["notes"]:
                    fields["notes"] = item["notes"]
                store.update_food(existing["id"], **fields)
                store.log_food_event(existing["id"], "restock",
                                     f"补货：数量={item['quantity'] or '不变'}")
                action = "restocked"
                item_id = existing["id"]
            else:
                item_id = store.add_food(
                    name=item["name"], category=item["category"],
                    quantity=item["quantity"], expires_in_days=item["expires_in_days"],
                    notes=item["notes"], added_date=today,
                )
                store.log_food_event(item_id, "add", f"入库：{item['quantity'] or '数量未记'}")
                action = "added"
            expires_on = (date.today()
                          + timedelta(days=item["expires_in_days"])).isoformat()
            results.append({
                "id": item_id, "name": item["name"], "action": action,
                "category": item["category"], "quantity": item["quantity"],
                "expires_in_days": item["expires_in_days"], "expires_on": expires_on,
            })
        return {"items": results, "count": len(results)}

    def _list_food_items(args: Dict) -> Dict:
        want = args.get("filter", "in_stock")
        if want == "all":
            items = store.list_food()
        elif want == "in_stock":
            items = store.list_food(status="in_stock")
        elif want == "expiring":
            items = [
                dict(x) for x in store.list_food(status="in_stock")
                if (lambda left: left is not None and left <= 2)(days_left(x, date.today()))
            ]
        else:
            raise DeviceError(f"filter 只支持 in_stock/all/expiring，收到 {want!r}")
        today = date.today()
        views = []
        for item in items:
            left = days_left(item, today)
            views.append({
                "id": item["id"], "name": item["name"], "category": item["category"],
                "quantity": item["quantity"], "status": item["status"],
                "added_date": item["added_date"],
                "expires_in_days": item["expires_in_days"],
                "days_left": left,
                "expired": left is not None and left < 0,
            })
        return {"items": views, "count": len(views)}

    def _remove_food_item(args: Dict) -> Dict:
        item = store.get_food(args["item_id"])
        if item is None:
            raise DeviceError(f"食材 #{args['item_id']} 不存在")
        if item["status"] != "in_stock":
            raise DeviceError(
                f"食材「{item['name']}」当前状态为 {item['status']}，无需再次移除"
            )
        reason = args["reason"]
        store.update_food(args["item_id"], status=reason)
        store.log_food_event(args["item_id"], reason, args.get("note") or "")
        label = "已用完" if reason == "used_up" else "已丢弃"
        return {"item_id": args["item_id"], "name": item["name"], "result": label}

    item_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "食材名，如 西红柿"},
            "category": {"type": "string", "enum": list(FOOD_CATEGORIES),
                         "description": "类目；不认识就选 其他"},
            "quantity": {"type": "string", "description": "数量，如 3个 / 500g；可省略"},
            "expires_in_days": {"type": "integer", "minimum": 0, "maximum": 3650,
                                "description": "保质期天数（自入库起）。请尽量给出；"
                                               "缺省按类目默认值"},
            "notes": {"type": "string", "description": "备注，可省略"},
        },
        "required": ["name"],
    }

    return [
        ToolSpec(
            name="add_food_items",
            description=(
                "把食材登记进家庭食材台账（支持批量）。用户说「买了…」「家里有…」时使用。"
                "台账里已有同名在库食材时自动按补货处理（重置入库日期，保质期重新起算）。"
                "登记前会请求用户确认清单。"
            ),
            parameters={
                "type": "object",
                "properties": {"items": {"type": "array", "items": item_schema,
                                         "description": "食材列表，最多 20 项"}},
                "required": ["items"],
                "additionalProperties": False,
            },
            risk=RISK_MEDIUM,
            handler=_add_food_items,
            timeout_seconds=10.0,
        ),
        ToolSpec(
            name="list_food_items",
            description=(
                "查看家庭食材台账（真实库存，不是猜测）。用户问「家里有什么菜/能做什么吃」"
                "时必须先调用本工具；filter=expiring 只看临期食材。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "filter": {"type": "string",
                               "enum": ["in_stock", "all", "expiring"],
                               "description": "默认 in_stock（在库）"},
                },
                "required": [],
                "additionalProperties": False,
            },
            risk=RISK_LOW,
            handler=_list_food_items,
            timeout_seconds=5.0,
            retryable=True,
        ),
        ToolSpec(
            name="remove_food_item",
            description=(
                "把某项食材移出台账：reason=used_up（做菜用完/吃掉）或 discarded（坏掉丢弃）。"
                "用户说「西红柿用完了」「鸡蛋坏了两盒」时使用。需用户确认。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer", "description": "食材 ID（来自 list_food_items）"},
                    "reason": {"type": "string", "enum": ["used_up", "discarded"]},
                    "note": {"type": "string", "description": "备注，可省略"},
                },
                "required": ["item_id", "reason"],
                "additionalProperties": False,
            },
            risk=RISK_MEDIUM,
            handler=_remove_food_item,
            timeout_seconds=5.0,
        ),
    ]
