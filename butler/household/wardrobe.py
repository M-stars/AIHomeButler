"""衣柜管家（阶段 14.7）：家庭成员衣物台账 + 穿着记录 + 洗涤跟踪 + 穿搭推荐。

设计约束：
  - 衣物按成员归属（owner）。语音模式默认取声纹识别出的说话人；文本模式
    由模型传 owner（用户点名"给小星推荐"），都没有时落到「我」并如实说明；
  - 穿着登记是低摩擦记账（LOW 风险免确认）：同一天同一件重复登记幂等跳过；
  - 洗涤提醒是确定性计算：按类目缺省策略（距上次洗涤天数 / 自上次洗涤后
    穿着次数），单件可用 wash_interval_days 覆盖天数；配饰不提醒；
  - 穿搭推荐拆成"确定性数据 + 模型推理"：工具返回天气实况、在库干净衣物、
    近期反馈，由模型组织建议；天气失败如实降级，绝不编造。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional

from ..devices.base import DeviceError
from ..tools.base import RISK_LOW, RISK_MEDIUM, ToolSpec
from .store import HouseholdStore, normalize_name
from .weather import WeatherProvider, WeatherUnavailable

GARMENT_CATEGORIES = ("上衣", "裤子", "裙子", "外套", "内衣", "袜子", "鞋子", "配饰", "其他")
WARMTH_LEVELS = ("薄", "适中", "厚")

# 类目缺省洗涤策略：interval=距上次洗涤多少天该洗；max_wears=自上次洗涤最多穿几次。
# 取保守值；单件衣物可用 wash_interval_days 覆盖天数维度。
CATEGORY_WASH_POLICY = {
    "上衣": {"interval": 3, "max_wears": 3},
    "裤子": {"interval": 5, "max_wears": 5},
    "裙子": {"interval": 4, "max_wears": 4},
    "外套": {"interval": 20, "max_wears": 15},
    "内衣": {"interval": 1, "max_wears": 1},
    "袜子": {"interval": 1, "max_wears": 1},
    "鞋子": {"interval": 60, "max_wears": 30},
    "其他": {"interval": 7, "max_wears": 5},
    # 配饰（围巾/腰带等）不做洗涤跟踪
    "配饰": None,
}

DEFAULT_OWNER = "我"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_BATCH = 20


def resolve_owner(explicit: Optional[str], member_context=None) -> str:
    """确定衣物归属：显式指定 > 声纹识别出的成员 > 默认「我」。"""
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    member = member_context.get() if member_context is not None else None
    if member and member.get("display_name"):
        return str(member["display_name"]).strip()
    return DEFAULT_OWNER


def wash_policy(garment: Dict[str, Any]) -> Optional[Dict[str, int]]:
    """单件衣物的洗涤策略；None=不跟踪（配饰）。"""
    category = garment.get("category") or "其他"
    policy = CATEGORY_WASH_POLICY.get(category, CATEGORY_WASH_POLICY["其他"])
    if policy is None:
        return None
    interval = garment.get("wash_interval_days")
    if isinstance(interval, int) and not isinstance(interval, bool) and interval > 0:
        return {"interval": interval, "max_wears": policy["max_wears"]}
    return dict(policy)


def evaluate_wash(garment: Dict[str, Any], today: date) -> Optional[Dict[str, Any]]:
    """单件衣物（须为 clean 状态）的该洗信号；None=无需提醒。"""
    policy = wash_policy(garment)
    if policy is None:
        return None
    reasons: List[str] = []
    wears = int(garment.get("times_worn_since_wash") or 0)
    if wears >= policy["max_wears"]:
        reasons.append(f"已连穿 {wears} 次")
    last_wash = garment.get("last_wash_date")
    if last_wash:
        try:
            elapsed = (today - date.fromisoformat(str(last_wash)[:10])).days
            if elapsed >= policy["interval"]:
                reasons.append(f"距上次洗涤 {elapsed} 天")
        except ValueError:
            pass
    elif not reasons:
        # 从未洗涤过也从未记录穿着：没有可靠起算点，不打扰
        return None
    if not reasons:
        return None
    name = garment.get("name", f"#{garment.get('id')}")
    return {
        "level": "info",
        "garment_id": garment["id"],
        "name": name,
        "message": f"衣物「{name}」（{garment.get('owner')}）{'、'.join(reasons)}，该洗了",
    }


def _parse_date_or_none(raw: Any, field: str) -> Optional[str]:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    value = str(raw).strip()
    if not _DATE_RE.match(value):
        raise DeviceError(f"{field} 需要是 YYYY-MM-DD 格式日期，收到 {value!r}")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise DeviceError(f"{field} 不是有效日期：{value!r}") from None
    return value


def build_wardrobe_tools(store: HouseholdStore, weather: WeatherProvider,
                         member_context=None) -> List[ToolSpec]:
    def _add_garments(args: Dict) -> Dict:
        raw_items = args["items"]
        if not isinstance(raw_items, list) or not raw_items:
            raise DeviceError("items 需要至少一件衣物")
        if len(raw_items) > _MAX_BATCH:
            raise DeviceError(f"一次最多登记 {_MAX_BATCH} 件衣物")
        owner = resolve_owner(args.get("owner"), member_context)
        results = []
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                raise DeviceError(f"第 {index + 1} 件衣物必须是对象")
            name = str(raw.get("name") or "").strip()
            if not name:
                raise DeviceError(f"第 {index + 1} 件衣物缺少名称")
            category = str(raw.get("category") or "").strip()
            if category not in GARMENT_CATEGORIES:
                category = "其他"
            warmth = str(raw.get("warmth") or "").strip()
            if warmth not in WARMTH_LEVELS:
                warmth = "适中"
            interval = raw.get("wash_interval_days")
            if interval is not None and (
                isinstance(interval, bool) or not isinstance(interval, int)
                or not 1 <= interval <= 365
            ):
                raise DeviceError(f"「{name}」的 wash_interval_days 需要是 1-365 的整数")
            existing = store.find_garment(owner, normalize_name(name))
            if existing is not None:
                fields: Dict[str, Any] = {"category": category, "warmth": warmth}
                if interval is not None:
                    fields["wash_interval_days"] = interval
                if raw.get("notes"):
                    fields["notes"] = str(raw["notes"]).strip()
                store.update_garment(existing["id"], **fields)
                action, gid = "updated", existing["id"]
            else:
                gid = store.add_garment(
                    owner=owner, name=name, category=category, warmth=warmth,
                    wash_interval_days=interval,
                    notes=str(raw.get("notes") or "").strip(),
                )
                action = "added"
            results.append({"id": gid, "name": name, "owner": owner, "action": action,
                            "category": category, "warmth": warmth})
        return {"owner": owner, "items": results, "count": len(results)}

    def _list_garments(args: Dict) -> Dict:
        owner = resolve_owner(args.get("owner"), member_context)
        status = args.get("status", "clean")
        if status == "all":
            garments = store.list_garments(owner=owner)
        elif status in ("clean", "in_laundry"):
            garments = store.list_garments(owner=owner, status=status)
        else:
            raise DeviceError(f"status 只支持 clean/in_laundry/all，收到 {status!r}")
        today = date.today()
        views = []
        for g in garments:
            days_since_wash = None
            if g.get("last_wash_date"):
                try:
                    days_since_wash = (today - date.fromisoformat(g["last_wash_date"][:10])).days
                except ValueError:
                    pass
            views.append({
                "id": g["id"], "name": g["name"], "category": g["category"],
                "warmth": g["warmth"], "status": g["status"],
                "times_worn_since_wash": g["times_worn_since_wash"],
                "last_wash_date": g["last_wash_date"],
                "days_since_wash": days_since_wash,
                "notes": g["notes"],
            })
        return {"owner": owner, "garments": views, "count": len(views)}

    def _log_wear(args: Dict) -> Dict:
        names = args["names"]
        if not isinstance(names, list) or not names:
            raise DeviceError("names 需要至少一件衣物名称")
        if len(names) > _MAX_BATCH:
            raise DeviceError(f"一次最多登记 {_MAX_BATCH} 件")
        owner = resolve_owner(args.get("owner"), member_context)
        worn_on = _parse_date_or_none(args.get("worn_on"), "worn_on") or date.today().isoformat()
        logged, skipped, unmatched = [], [], []
        for raw_name in names:
            name = str(raw_name or "").strip()
            if not name:
                continue
            garment = store.find_garment(owner, normalize_name(name))
            if garment is None:
                unmatched.append(name)
                continue
            if store.has_wear_event(garment["id"], worn_on):
                skipped.append({"name": name, "reason": "该日期已登记过，幂等跳过"})
                continue
            store.add_wear_event(garment["id"], owner, worn_on)
            store.update_garment(
                garment["id"],
                times_worn_since_wash=int(garment["times_worn_since_wash"]) + 1,
                last_worn_date=worn_on,
            )
            logged.append({"id": garment["id"], "name": name})
        return {"owner": owner, "worn_on": worn_on, "logged": logged,
                "skipped": skipped, "unmatched": unmatched}

    def _mark_washed(args: Dict) -> Dict:
        names = args["names"]
        if not isinstance(names, list) or not names:
            raise DeviceError("names 需要至少一件衣物名称")
        if len(names) > _MAX_BATCH:
            raise DeviceError(f"一次最多登记 {_MAX_BATCH} 件")
        owner = resolve_owner(args.get("owner"), member_context)
        washed_on = _parse_date_or_none(args.get("washed_on"), "washed_on") or date.today().isoformat()
        washed, unmatched = [], []
        for raw_name in names:
            name = str(raw_name or "").strip()
            if not name:
                continue
            garment = store.find_garment(owner, normalize_name(name))
            if garment is None:
                unmatched.append(name)
                continue
            store.update_garment(
                garment["id"], status="clean", times_worn_since_wash=0,
                last_wash_date=washed_on,
            )
            washed.append({"id": garment["id"], "name": name})
        return {"owner": owner, "washed_on": washed_on, "washed": washed,
                "unmatched": unmatched}

    def _set_outfit_feedback(args: Dict) -> Dict:
        content = str(args["content"] or "").strip()
        if not content:
            raise DeviceError("反馈内容不能为空")
        owner = resolve_owner(args.get("owner"), member_context)
        garment_name = str(args.get("garment_name") or "").strip()
        garment_id = None
        if garment_name:
            garment = store.find_garment(owner, normalize_name(garment_name))
            if garment is None:
                raise DeviceError(f"没有找到「{garment_name}」（{owner}），"
                                  "请先用 add_garments 登记或核对名称")
            garment_id = garment["id"]
        feedback_id = store.add_outfit_feedback(owner, content, garment_id)
        return {"feedback_id": feedback_id, "owner": owner,
                "garment_name": garment_name or None, "saved": True}

    def _recommend_outfit(args: Dict) -> Dict:
        owner = resolve_owner(args.get("owner"), member_context)
        today = date.today()
        clean = store.list_garments(owner=owner, status="clean")
        garments = []
        for g in clean:
            days_since_wash = None
            if g.get("last_wash_date"):
                try:
                    days_since_wash = (today - date.fromisoformat(g["last_wash_date"][:10])).days
                except ValueError:
                    pass
            garments.append({
                "id": g["id"], "name": g["name"], "category": g["category"],
                "warmth": g["warmth"], "times_worn_since_wash": g["times_worn_since_wash"],
                "days_since_wash": days_since_wash, "notes": g["notes"],
            })
        feedback = store.list_outfit_feedback(owner=owner, limit=8)
        weather_data = None
        weather_note = ""
        try:
            weather_data = weather.current_summary()
        except WeatherUnavailable as exc:
            weather_note = f"天气信息不可用（{exc}），本次建议未考虑天气。"
        return {
            "owner": owner,
            "today": today.isoformat(),
            "weekday": "一二三四五六日"[today.weekday()],
            "occasion": str(args.get("occasion") or "").strip() or None,
            "weather": weather_data,
            "weather_note": weather_note,
            "garments": garments,
            "recent_feedback": [
                {"content": f["content"], "created_at": f["created_at"]} for f in feedback
            ],
        }

    return [
        ToolSpec(
            name="add_garments",
            description=(
                "把衣物登记进家庭成员衣柜（支持批量）。用户说「我有/买了…」时使用；"
                "owner 省略时记到当前说话人名下。同名衣物重复登记 = 更新信息。"
                "登记前会请求用户确认。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "衣物名，如 蓝色卫衣"},
                                "category": {"type": "string", "enum": list(GARMENT_CATEGORIES)},
                                "warmth": {"type": "string", "enum": list(WARMTH_LEVELS),
                                           "description": "薄/适中/厚，默认 适中"},
                                "wash_interval_days": {"type": "integer", "minimum": 1, "maximum": 365,
                                                       "description": "自定义洗涤间隔天数，可省略"},
                                "notes": {"type": "string"},
                            },
                            "required": ["name"],
                        },
                        "description": "衣物列表，最多 20 件",
                    },
                    "owner": {"type": "string", "description": "归属成员称呼；省略=当前说话人"},
                },
                "required": ["items"],
                "additionalProperties": False,
            },
            risk=RISK_MEDIUM,
            handler=_add_garments,
            timeout_seconds=10.0,
        ),
        ToolSpec(
            name="list_garments",
            description="查看某成员的衣物台账（真实库存）。owner 省略=当前说话人。",
            parameters={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "status": {"type": "string", "enum": ["clean", "in_laundry", "all"],
                               "description": "默认 clean（可穿）"},
                },
                "required": [],
                "additionalProperties": False,
            },
            risk=RISK_LOW,
            handler=_list_garments,
            timeout_seconds=5.0,
            retryable=True,
        ),
        ToolSpec(
            name="log_wear",
            description=(
                "登记今天（或指定日期）实际穿的衣物，用于穿着次数统计与洗涤提醒。"
                "用户说「今天穿的…」时使用；未登记的衣物会出现在 unmatched 里，"
                "此时应建议用户先登记。同日重复登记自动幂等跳过。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "names": {"type": "array", "items": {"type": "string"},
                              "description": "衣物名称列表"},
                    "owner": {"type": "string"},
                    "worn_on": {"type": "string",
                                "description": "YYYY-MM-DD，默认今天"},
                },
                "required": ["names"],
                "additionalProperties": False,
            },
            risk=RISK_LOW,
            handler=_log_wear,
            timeout_seconds=5.0,
        ),
        ToolSpec(
            name="mark_washed",
            description=(
                "衣物洗好了：标记为可穿并重置穿着计数（洗涤提醒的起算点）。"
                "用户说「洗好了/从洗衣机拿出来的是…」时使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "names": {"type": "array", "items": {"type": "string"}},
                    "owner": {"type": "string"},
                    "washed_on": {"type": "string", "description": "YYYY-MM-DD，默认今天"},
                },
                "required": ["names"],
                "additionalProperties": False,
            },
            risk=RISK_LOW,
            handler=_mark_washed,
            timeout_seconds=5.0,
        ),
        ToolSpec(
            name="set_outfit_feedback",
            description=(
                "记录用户对某件衣物/某套穿搭的反馈（如「这条裤子显胖」「这外套很暖」），"
                "之后的穿搭推荐会参考。用户表达喜欢/不喜欢时主动使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "反馈原意，一句话"},
                    "owner": {"type": "string"},
                    "garment_name": {"type": "string",
                                     "description": "针对单件衣物时给名称；整套反馈省略"},
                },
                "required": ["content"],
                "additionalProperties": False,
            },
            risk=RISK_LOW,
            handler=_set_outfit_feedback,
            timeout_seconds=5.0,
        ),
        ToolSpec(
            name="recommend_outfit",
            description=(
                "获取穿搭推荐所需数据：今日天气（未配置/失败时返回 weather_note）、"
                "该成员全部干净衣物、近期穿搭反馈。拿到数据后由你组织成具体穿搭建议；"
                "weather 为 null 时如实说明未考虑天气，禁止编造气温。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "occasion": {"type": "string",
                                 "description": "场合，如 上学/上班/运动；可省略"},
                },
                "required": [],
                "additionalProperties": False,
            },
            risk=RISK_LOW,
            handler=_recommend_outfit,
            timeout_seconds=15.0,
        ),
    ]
