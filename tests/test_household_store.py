"""HouseholdStore 单元测试：CRUD、补货语义、穿着幂等、白名单更新。全部离线。"""
from datetime import date

import pytest

from butler.household.store import HouseholdStore, normalize_name


@pytest.fixture
def store(tmp_path):
    return HouseholdStore(tmp_path / "household.db")


TODAY = "2026-11-01"


# ---------------------------------------------------------------- 食材

def test_add_food_and_get(store):
    item_id = store.add_food("西红柿", "蔬菜", "3个", 7, "", TODAY)
    item = store.get_food(item_id)
    assert item["name"] == "西红柿"
    assert item["name_key"] == "西红柿"
    assert item["status"] == "in_stock"
    assert item["added_date"] == TODAY


def test_find_food_in_stock_normalizes_name(store):
    store.add_food("西红柿", "蔬菜", "3个", 7, "", TODAY)
    assert store.find_food_in_stock(normalize_name("  西红柿 ")) is not None
    assert store.find_food_in_stock(normalize_name("土豆")) is None


def test_find_food_ignores_used_up(store):
    item_id = store.add_food("鸡蛋", "肉禽蛋", "1盒", 14, "", TODAY)
    store.update_food(item_id, status="used_up")
    assert store.find_food_in_stock(normalize_name("鸡蛋")) is None


def test_update_food_whitelist(store):
    item_id = store.add_food("牛奶", "乳品", "1箱", 14, "", TODAY)
    assert store.update_food(item_id, quantity="2箱", last_remind_date=TODAY)
    item = store.get_food(item_id)
    assert item["quantity"] == "2箱"
    assert item["last_remind_date"] == TODAY
    with pytest.raises(ValueError):
        store.update_food(item_id, name_key="hack")
    with pytest.raises(ValueError):
        store.update_food(item_id)


def test_list_food_filters_by_status(store):
    used = store.add_food("面包", "主食", "1袋", 5, "", TODAY)
    store.update_food(used, status="used_up")
    store.add_food("白菜", "蔬菜", "1棵", 7, "", TODAY)
    in_stock = [i["name"] for i in store.list_food(status="in_stock")]
    assert in_stock == ["白菜"]
    assert len(store.list_food()) == 2


def test_food_events_logged(store):
    item_id = store.add_food("排骨", "肉禽蛋", "500g", 3, "", TODAY)
    store.log_food_event(item_id, "add", "入库")
    events = store.list_food_events(item_id)
    assert len(events) == 1
    assert events[0]["action"] == "add"


# ---------------------------------------------------------------- 衣物

def test_add_garment_requires_owner_and_name(store):
    with pytest.raises(ValueError):
        store.add_garment("", "卫衣", "上衣", "适中", None, "")
    with pytest.raises(ValueError):
        store.add_garment("小星", "", "上衣", "适中", None, "")


def test_find_garment_scope_by_owner(store):
    store.add_garment("小星", "蓝色卫衣", "上衣", "适中", None, "")
    store.add_garment("爷爷", "蓝色卫衣", "上衣", "厚", None, "")
    assert store.find_garment("小星", normalize_name("蓝色卫衣"))["owner"] == "小星"
    assert store.find_garment("爷爷", normalize_name("蓝色卫衣"))["warmth"] == "厚"
    assert store.find_garment("其他人", normalize_name("蓝色卫衣")) is None


def test_find_garment_skips_retired(store):
    gid = store.add_garment("小星", "旧T恤", "上衣", "薄", None, "")
    store.update_garment(gid, status="retired")
    assert store.find_garment("小星", normalize_name("旧T恤")) is None


def test_update_garment_whitelist(store):
    gid = store.add_garment("小星", "牛仔裤", "裤子", "适中", None, "")
    assert store.update_garment(
        gid, times_worn_since_wash=2, last_wash_date=TODAY, status="in_laundry"
    )
    g = store.get_garment(gid)
    assert g["times_worn_since_wash"] == 2
    assert g["status"] == "in_laundry"
    with pytest.raises(ValueError):
        store.update_garment(gid, owner="别人")
    with pytest.raises(ValueError):
        store.update_garment(gid)


def test_list_garments_filters(store):
    store.add_garment("小星", "卫衣", "上衣", "适中", None, "")
    dirty = store.add_garment("小星", "袜子", "袜子", "薄", None, "")
    store.update_garment(dirty, status="in_laundry")
    assert [g["name"] for g in store.list_garments(owner="小星", status="clean")] == ["卫衣"]
    assert len(store.list_garments(owner="小星", status="in_laundry")) == 1
    assert len(store.list_garments()) == 2


# ------------------------------------------------------------ 穿着与反馈

def test_wear_event_idempotent_query(store):
    gid = store.add_garment("小星", "T恤", "上衣", "薄", None, "")
    store.add_wear_event(gid, "小星", "2026-11-01")
    assert store.has_wear_event(gid, "2026-11-01")
    assert not store.has_wear_event(gid, "2026-11-02")
    assert not store.has_wear_event(gid + 100, "2026-11-01")


def test_outfit_feedback(store):
    gid = store.add_garment("小星", "牛仔裤", "裤子", "适中", None, "")
    store.add_outfit_feedback("小星", "这条裤子很舒服", gid)
    store.add_outfit_feedback("小星", "今天这套有点冷")
    store.add_outfit_feedback("爷爷", "外套很暖")
    mine = store.list_outfit_feedback(owner="小星")
    assert len(mine) == 2
    # 默认按 id 倒序：最新在前
    assert mine[0]["content"] == "今天这套有点冷"
    assert mine[1]["garment_id"] == gid
    with pytest.raises(ValueError):
        store.add_outfit_feedback("小星", "  ")


def test_normalize_name():
    assert normalize_name("  蓝色 卫衣 ") == "蓝色 卫衣"
    assert normalize_name("T-Shirt") == "t-shirt"
    assert normalize_name("") == ""
