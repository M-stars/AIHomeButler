"""阶段 13 家庭成员声纹测试：注册/识别/阈值/陌生人、成员上下文、按人记忆隔离。

全部离线：向量用构造数据，声纹提取端点用 httpx.MockTransport，不触真实服务。
"""
import sqlite3
from pathlib import Path

import httpx
import pytest

from butler.memory.store import MemoryStore
from butler.tools.memory_tools import build_memory_tools
from butler.voice.cosyvoice_client import CosyVoiceClient, CosyVoiceClientError
from butler.voice.speakers import (
    MemberContext,
    SpeakerError,
    SpeakerRegistry,
)

FAKE_EMBED_A = [1.0] + [0.0] * 191          # 192 维
FAKE_EMBED_B = [0.0, 1.0] + [0.0] * 190
FAKE_EMBED_NEAR_A = [0.98, 0.02] + [0.0] * 190  # 与 A 余弦≈0.98


@pytest.fixture()
def registry(tmp_path):
    return SpeakerRegistry(tmp_path / "speakers")


# ---- 注册 ----

def test_enroll_requires_name_and_consent(registry):
    with pytest.raises(SpeakerError):
        registry.enroll("", FAKE_EMBED_A, consent_granted=True)
    with pytest.raises(SpeakerError) as excinfo:
        registry.enroll("爸爸", FAKE_EMBED_A, consent_granted=False)
    assert "授权" in str(excinfo.value)
    assert registry.list() == []


def test_enroll_rejects_duplicate_name_and_dim_mismatch(registry):
    registry.enroll("爸爸", FAKE_EMBED_A, consent_granted=True)
    with pytest.raises(SpeakerError):
        registry.enroll("爸爸", FAKE_EMBED_B, consent_granted=True)
    with pytest.raises(SpeakerError):
        registry.enroll("妈妈", [1.0, 2.0], consent_granted=True)  # 维度不一致
    with pytest.raises(SpeakerError):
        registry.enroll("妈妈", [], consent_granted=True)


def test_list_and_get_strip_embedding(registry):
    profile = registry.enroll("爸爸", FAKE_EMBED_A, consent_granted=True)
    assert all("embedding" not in m for m in registry.list())
    assert "embedding" not in registry.get(profile["member_id"])
    assert len(registry.get(profile["member_id"], with_embedding=True)["embedding"]) == 192


# ---- 识别 ----

def test_identify_matches_above_threshold_and_records(registry):
    profile = registry.enroll("爸爸", FAKE_EMBED_A, consent_granted=True)
    member, sim = registry.identify(list(FAKE_EMBED_A), threshold=0.30)
    assert member["member_id"] == profile["member_id"]
    assert sim == pytest.approx(1.0, abs=1e-6)
    member2, _ = registry.identify(FAKE_EMBED_NEAR_A, threshold=0.30, record=True)
    assert member2["display_name"] == "爸爸"
    hits = registry.get(profile["member_id"])["hits"]
    assert hits["count"] == 1 and hits["last_similarity"] is not None
    assert len(hits["recent"]) == 1


def test_identify_below_threshold_is_stranger(registry):
    registry.enroll("爸爸", FAKE_EMBED_A, consent_granted=True)
    member, sim = registry.identify(list(FAKE_EMBED_B), threshold=0.30)
    assert member is None        # 正交向量相似度 0 → 陌生人
    assert sim == 0.0
    # 高于 0 但低于阈值（与 [1,0,...] 夹角使相似度恰为 0.6）
    half = [0.6, 0.8] + [0.0] * 190
    member2, sim2 = registry.identify(half, threshold=0.80)
    assert member2 is None
    assert sim2 == pytest.approx(0.6, abs=1e-6)


def test_identify_skips_members_without_consent(registry, tmp_path):
    registry.enroll("爸爸", FAKE_EMBED_A, consent_granted=True)
    # 直接构造未授权档案（模拟历史数据）：识别必须跳过它
    index = tmp_path / "speakers" / "speakers.json"
    payload = {
        "version": 1, "embed_dim": 192,
        "members": [{
            "member_id": "pending001", "display_name": "未授权",
            "embedding": list(FAKE_EMBED_A), "consent_status": "pending",
            "voice_id": None, "created_at": "x", "updated_at": "x",
            "hits": {"count": 0, "last_at": None, "last_similarity": None},
        }],
    }
    index.write_text(__import__("json").dumps(payload), encoding="utf-8")
    r2 = SpeakerRegistry(tmp_path / "speakers")
    member, _ = r2.identify(list(FAKE_EMBED_A), threshold=0.30)
    assert member is None  # 未授权成员绝不参与识别


def test_identify_with_no_members(registry):
    member, sim = registry.identify(list(FAKE_EMBED_A), threshold=0.30)
    assert member is None and sim == 0.0


def test_identify_dim_mismatch_rejected(registry):
    registry.enroll("爸爸", FAKE_EMBED_A, consent_granted=True)
    with pytest.raises(SpeakerError):
        registry.identify([1.0, 2.0], threshold=0.30)


# ---- 变更 ----

def test_update_voice_and_name_and_delete(registry):
    profile = registry.enroll("爸爸", FAKE_EMBED_A, consent_granted=True)
    updated = registry.update_voice(profile["member_id"], "voice123")
    assert updated["voice_id"] == "voice123"
    cleared = registry.update_voice(profile["member_id"], "")
    assert cleared["voice_id"] is None
    renamed = registry.update_name(profile["member_id"], "老爸")
    assert renamed["display_name"] == "老爸"
    with pytest.raises(SpeakerError):
        registry.enroll("老爸", FAKE_EMBED_B, consent_granted=True)
    removed = registry.delete(profile["member_id"])
    assert removed["display_name"] == "老爸"
    assert registry.list() == []


def test_delete_resets_embed_dim(registry):
    profile = registry.enroll("爸爸", FAKE_EMBED_A, consent_granted=True)
    registry.delete(profile["member_id"])
    assert registry.embed_dim is None
    # 删除后可注册不同维度的新档案（换模型场景）
    registry.enroll("新成员", [1.0] * 256, consent_granted=True)
    assert registry.embed_dim == 256


def test_persistence_roundtrip(tmp_path):
    r1 = SpeakerRegistry(tmp_path / "spk")
    profile = r1.enroll("妈妈", FAKE_EMBED_A, consent_granted=True, voice_id="v1")
    r1.record_hit(profile["member_id"], 0.87)
    r2 = SpeakerRegistry(tmp_path / "spk")
    member = r2.get(profile["member_id"])
    assert member["display_name"] == "妈妈" and member["voice_id"] == "v1"
    assert member["hits"]["count"] == 1
    member_hit, sim = r2.identify(list(FAKE_EMBED_A), threshold=0.30)
    assert member_hit["display_name"] == "妈妈"


def test_corrupted_index_actionable_error(tmp_path):
    (tmp_path / "spk").mkdir()
    (tmp_path / "spk" / "speakers.json").write_text("{bad json", encoding="utf-8")
    with pytest.raises(SpeakerError):
        SpeakerRegistry(tmp_path / "spk")


def test_get_by_name(registry):
    registry.enroll("爸爸", FAKE_EMBED_A, consent_granted=True)
    assert registry.get_by_name("爸爸")["display_name"] == "爸爸"
    assert registry.get_by_name("路人") is None
    assert registry.get_by_name("  爸爸  ")["display_name"] == "爸爸"  # 去空白
    with pytest.raises(SpeakerError):
        registry.get("../etc")  # get 对未知 ID 抛错；get_by_name 返回 None 不抛


# ---- MemberContext ----

def test_member_context_set_get_clear():
    ctx = MemberContext()
    assert ctx.get() is None
    ctx.set({"member_id": "m1", "display_name": "爸爸"})
    got = ctx.get()
    assert got["display_name"] == "爸爸"
    got["display_name"] = "被改了"  # 拷贝，不影响内部状态
    assert ctx.get()["display_name"] == "爸爸"
    ctx.clear()
    assert ctx.get() is None
    ctx.set(None)
    assert ctx.get() is None


# ---- 客户端 speaker_embedding ----

def _client_with(handler, **kw):
    return CosyVoiceClient(
        "http://127.0.0.1:9880", transport=httpx.MockTransport(handler), **kw
    )


def test_client_speaker_embedding_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/speaker_embed")
        assert b"clip.wav" in request.content
        return httpx.Response(200, json={"embedding": [0.1, 0.2], "dim": 2})

    client = _client_with(handler)
    embedding = client.speaker_embedding(b"RIFF....", filename="clip.wav")
    assert embedding == [0.1, 0.2]


def test_client_speaker_embedding_error_mapping():
    client = _client_with(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(CosyVoiceClientError) as excinfo:
        client.speaker_embedding(b"x")
    assert excinfo.value.kind == "server_error"

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client2 = _client_with(down)
    with pytest.raises(CosyVoiceClientError) as excinfo:
        client2.speaker_embedding(b"x")
    assert excinfo.value.kind == "unavailable"
    with pytest.raises(CosyVoiceClientError):
        client2.speaker_embedding(b"")


def test_client_speaker_embedding_bad_json():
    client = _client_with(lambda request: httpx.Response(200, content=b"not json"))
    with pytest.raises(CosyVoiceClientError) as excinfo:
        client.speaker_embedding(b"x")
    assert excinfo.value.kind == "server_error"


# ---- 按成员记忆隔离（V2）----

def test_memory_member_scoping(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    own = store.add("preference", "爸爸喜欢24度", member_id="dad")
    shared = store.add("fact", "周五全家大扫除")
    other = store.add("preference", "妈妈怕冷", member_id="mom")

    dad_view = store.list(for_member_id="dad")
    contents = {m["content"] for m in dad_view}
    assert "爸爸喜欢24度" in contents and "周五全家大扫除" in contents
    assert "妈妈怕冷" not in contents
    all_view = store.list()
    assert len(all_view) == 3
    assert {m["member_id"] for m in all_view} == {"dad", "mom", None}


def test_memory_prompt_block_labels(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    store.add("preference", "爸爸喜欢24度", member_id="dad")
    store.add("fact", "周五全家大扫除")
    block = store.prompt_block(for_member_id="dad", member_name="爸爸")
    assert "【爸爸】" in block and "爸爸喜欢24度" in block
    assert "【共享】" in block and "周五全家大扫除" in block
    # 非成员视角（管理视图）：无个人/共享标注
    assert "【" not in store.prompt_block()


def test_memory_migration_from_old_schema(tmp_path):
    """老库（无 member_id 列）打开时自动迁移，数据保留。"""
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT,"
        " content TEXT, source TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO memories (kind, content, source, created_at, updated_at)"
        " VALUES ('fact', '旧库数据', 'user', 't', 't')"
    )
    conn.commit()
    conn.close()
    store = MemoryStore(db)
    assert "旧库数据" in [m["content"] for m in store.list()]
    store.add("preference", "新数据", member_id="dad")  # 迁移后可写入成员记忆
    assert len(store.list(for_member_id="dad")) == 2  # 旧数据视为共享


def test_memory_shared_only_scope_blocks_stranger(tmp_path):
    """未识别说话人时只能看共享记忆——陌生人不能翻看任何成员的个人记忆。"""
    store = MemoryStore(tmp_path / "memory.db")
    store.add("preference", "爸爸喜欢24度", member_id="dad")
    store.add("fact", "周五全家大扫除")
    shared = store.list(shared_only=True)
    assert [m["content"] for m in shared] == ["周五全家大扫除"]
    block = store.prompt_block(shared_only=True)
    assert "爸爸喜欢24度" not in block and "周五全家大扫除" in block
    # 管理视图仍能看到全部（Web 记忆管理/CLI /memory）
    assert len(store.list()) == 2


def test_memory_tools_tag_current_member(tmp_path):
    from butler.voice.speakers import MemberContext as Ctx  # 局部导入避免遮蔽

    store = MemoryStore(tmp_path / "memory.db")
    ctx = Ctx()
    tools = {spec.name: spec for spec in build_memory_tools(store, member_context=ctx)}

    ctx.set({"member_id": "dad", "display_name": "爸爸"})
    result = tools["save_memory"].handler({"kind": "preference", "content": "喜欢24度"})
    assert result["owner"] == "爸爸"
    assert store.list()[0]["member_id"] == "dad"

    listed = tools["list_memories"].handler({})
    assert listed["count"] == 1

    ctx.clear()
    result2 = tools["save_memory"].handler({"kind": "fact", "content": "周五打扫"})
    assert result2["owner"] == "家庭共享"
    # 无成员（未识别说话人）：只看共享记忆，不能翻爸爸的个人记忆
    assert tools["list_memories"].handler({})["count"] == 1
    assert tools["list_memories"].handler({})["memories"][0]["content"] == "周五打扫"


# ---- 识别辅助函数（assembly.identify_from_wav）----

class FakeEmbedClient:
    def __init__(self, embedding=None, fail=None):
        self.embedding = embedding or list(FAKE_EMBED_A)
        self.fail = fail
        self.calls = []

    def speaker_embedding(self, wav_bytes, filename="clip.wav"):
        self.calls.append(filename)
        if self.fail is not None:
            raise self.fail
        return list(self.embedding)


def _identity(tmp_path, enabled=True, threshold=0.30):
    from butler.voice.assembly import build_speaker_identity
    from butler.voice.cosyvoice_client import CosyVoiceClient as _C  # noqa: F401

    identity = build_speaker_identity(
        type("Cfg", (), {"data_dir": tmp_path / "data",
                         "speaker_recognition": enabled,
                         "speaker_threshold": threshold})(),
        client=None,
    )
    return identity


def test_identify_from_wav_hit(tmp_path):
    from butler.voice.assembly import identify_from_wav

    identity = _identity(tmp_path)
    profile = identity.registry.enroll("爸爸", FAKE_EMBED_A, consent_granted=True)
    fake = FakeEmbedClient()
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF....")
    member, sim = identify_from_wav(identity, fake, wav)
    assert member["member_id"] == profile["member_id"]
    assert sim == pytest.approx(1.0, abs=1e-6)
    assert identity.registry.get(profile["member_id"])["hits"]["count"] == 1


def test_identify_from_wav_disabled_or_error(tmp_path):
    from butler.voice.assembly import identify_from_wav

    identity = _identity(tmp_path, enabled=False)
    member, sim = identify_from_wav(identity, FakeEmbedClient(), tmp_path / "x.wav")
    assert member is None and sim is None  # 未启用时相似度也返回 None

    identity2 = _identity(tmp_path)
    identity2.registry.enroll("爸爸", FAKE_EMBED_A, consent_granted=True)
    failing = FakeEmbedClient(fail=CosyVoiceClientError("服务挂了", "unavailable"))
    member2, sim2 = identify_from_wav(identity2, failing, tmp_path / "x.wav")
    assert member2 is None and sim2 is None  # 失败不打断对话


def test_speaker_identity_status_shape(tmp_path):
    identity = _identity(tmp_path)
    identity.registry.enroll("爸爸", FAKE_EMBED_A, consent_granted=True)
    status = identity.status()
    assert status["speaker_recognition"] is True
    assert status["member_count"] == 1
    assert status["threshold"] == 0.30
    assert "不作为身份验证" in status["ai_disclaimer"]
    assert all("embedding" not in m for m in status["members"])
