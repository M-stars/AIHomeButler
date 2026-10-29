"""阶段 12 Web 音色库/语音合成 API 测试（FastAPI TestClient + Fake 语音栈）。

不连接真实 CosyVoice 服务：client 为 Fake，档案/缓存用 tmp 目录。
"""
import io
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from butler.voice.cosyvoice_client import CosyVoiceClientError, pcm_to_wav
from butler.voice.profiles import CONSENT_PENDING, VoiceProfileManager
from butler.voice.tts import AudioCache, CosyVoiceSpeaker, SilentSpeaker
from tests.test_voice_profiles import make_wav_bytes

FAKE_WAV = pcm_to_wav(b"\x01\x02" * 4000, 24000)


class FakeClient:
    """CosyVoiceClient 替身：可编程成功/失败。"""

    def __init__(self):
        self.calls = []
        self.fail = None
        self.healthy = True

    def synthesize(self, text, profile=None):
        self.calls.append((text, profile))
        if self.fail is not None:
            raise self.fail
        return FAKE_WAV

    def health_check(self, timeout_seconds: float = 5.0) -> bool:
        return self.healthy


def build_stack(tmp_path, monkeypatch, backend="cosyvoice"):
    from butler.voice.assembly import build_tts_stack
    from butler.config import load_config

    monkeypatch.setenv("TTS_BACKEND", backend)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LLM_API_KEY", "test-key-offline")
    config = load_config()
    stack = build_tts_stack(config)
    fake = FakeClient()
    # 注入 Fake 客户端（不触网），CosyVoiceSpeaker 复用同一档案管理器与缓存
    stack.client = fake
    stack.cosyvoice_speaker = CosyVoiceSpeaker(
        fake, stack.profiles,
        voice_id=None,
        cache=stack.cache,
    )
    stack.speaker = stack.cosyvoice_speaker if backend == "cosyvoice" else stack.speaker
    return stack


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    from butler.webapp import create_app

    stack = build_stack(tmp_path, monkeypatch)
    app = create_app(
        config=SimpleNamespace(
            model="fake-model",
            device_backend="simulated",
            confirmation_mode="always",
            tts_backend=stack.backend,
            data_dir=tmp_path / "data",
        ),
        registry=None,
        memory_store=SimpleNamespace(
            list=lambda: [],
            add=lambda *a, **k: "1",
            delete=lambda mid: True,
            list_notifications=lambda: [],
            list_task_logs=lambda: [],
        ),
        agent=SimpleNamespace(process=lambda text: SimpleNamespace(
            reply="好的", tool_events=[], stopped_by_limit=False)),
        rule_engine=SimpleNamespace(run_checks=lambda: []),
        confirmer=SimpleNamespace(pending=lambda: [], respond=lambda cid, ok: True),
        planner=None,
        voice_stack=stack,
    )
    return TestClient(app), stack


def test_voice_status_endpoint(app_client):
    client, stack = app_client
    resp = client.get("/api/voice/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["backend"] == "cosyvoice"
    assert body["cosyvoice_healthy"] is True
    assert "AI 合成" in body["ai_disclaimer"]


def test_voice_library_crud_flow(app_client):
    client, stack = app_client
    # 创建（multipart 上传）
    resp = client.post("/api/voices", data={
        "display_name": "我的音色",
        "reference_text": "这是参考录音的原文",
        "consent_granted": "true",
    }, files={"prompt_wav": ("ref.wav", io.BytesIO(make_wav_bytes()), "audio/wav")})
    assert resp.status_code == 200
    voice = resp.json()["voice"]
    vid = voice["voice_id"]
    assert voice["consent_status"] == "granted"
    assert "audio/" in voice["reference_audio_path"]
    assert "_audio_file" not in voice  # 内部路径字段不外泄

    # 列表
    listing = client.get("/api/voices").json()
    assert any(v["voice_id"] == vid for v in listing["voices"])

    # 未授权档案出现在列表但不可试听
    pending = stack.profiles.create("未授权音色", "原文", make_wav_bytes(), ".wav",
                                    consent_granted=False)
    resp = client.post(f"/api/voices/{pending['voice_id']}/test",
                       json={"text": "你好"})
    assert resp.status_code == 400
    assert "授权" in resp.json()["detail"]

    # 试听（已授权）→ 返回 WAV
    resp = client.post(f"/api/voices/{vid}/test", json={"text": "你好，卧室"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.content[:4] == b"RIFF"

    # 设为默认 → 影响合成层当前音色
    resp = client.post(f"/api/voices/{vid}/default")
    assert resp.status_code == 200
    assert stack.cosyvoice_speaker.voice_id == vid

    # 删除 → 档案消失、录音删除、生成缓存清除、合成层回退预设
    resp = client.delete(f"/api/voices/{vid}")
    assert resp.status_code == 200
    assert all(v["voice_id"] != vid for v in client.get("/api/voices").json()["voices"])
    assert stack.cosyvoice_speaker.voice_id is None


def test_upload_rejects_bad_format_and_missing_fields(app_client):
    client, _stack = app_client
    # 非音频内容伪装 .wav
    resp = client.post("/api/voices", data={
        "display_name": "坏文件", "reference_text": "文本", "consent_granted": "true",
    }, files={"prompt_wav": ("fake.wav", io.BytesIO(b"not audio"), "audio/wav")})
    assert resp.status_code == 400
    # 缺参考文本（FastAPI 把空字符串 Form 字段按缺失处理→422，业务校验→400，均为拒绝）
    resp = client.post("/api/voices", data={
        "display_name": "缺文本", "reference_text": "", "consent_granted": "true",
    }, files={"prompt_wav": ("ref.wav", io.BytesIO(make_wav_bytes()), "audio/wav")})
    assert resp.status_code in (400, 422)


def test_test_voice_endpoint_error_mapping(app_client):
    client, stack = app_client
    profile = stack.profiles.create("测试音色", "原文", make_wav_bytes(), ".wav",
                                    consent_granted=True)
    stack.client.fail = CosyVoiceClientError("服务挂了", "unavailable")
    resp = client.post(f"/api/voices/{profile['voice_id']}/test", json={"text": "hi"})
    assert resp.status_code == 503
    stack.client.fail = CosyVoiceClientError("太慢", "timeout")
    resp = client.post(f"/api/voices/{profile['voice_id']}/test", json={"text": "hi"})
    assert resp.status_code == 504


def test_tts_endpoint_follows_backend(app_client, tmp_path, monkeypatch):
    client, stack = app_client
    # cosyvoice 后端：走合成器
    resp = client.post("/api/tts", json={"text": "朗读这段话"})
    assert resp.status_code == 200
    assert resp.content[:4] == b"RIFF"
    assert stack.client.calls, "应调用注入的 Fake 客户端"
    # 空文本拒绝
    assert client.post("/api/tts", json={"text": "  "}).status_code == 400


def test_tts_endpoint_sapi_backend(tmp_path, monkeypatch):
    from butler.webapp import create_app

    stack = build_stack(tmp_path, monkeypatch, backend="sapi")
    app = create_app(
        config=SimpleNamespace(
            model="m", device_backend="simulated", confirmation_mode="always",
            tts_backend="sapi", data_dir=tmp_path / "data",
        ),
        registry=None,
        memory_store=SimpleNamespace(list=lambda: [], add=lambda *a, **k: "1",
                                     delete=lambda mid: True,
                                     list_notifications=lambda: [],
                                     list_task_logs=lambda: []),
        agent=SimpleNamespace(process=lambda t: SimpleNamespace(reply="r", tool_events=[],
                                                               stopped_by_limit=False)),
        rule_engine=SimpleNamespace(run_checks=lambda: []),
        confirmer=SimpleNamespace(pending=lambda: [], respond=lambda cid, ok: True),
        planner=None,
        voice_stack=stack,
    )
    client = TestClient(app)
    # SAPI 不可用的环境下 speaker 是 SilentSpeaker → speak_to_file 抛 VoiceError → 503
    resp = client.post("/api/tts", json={"text": "朗读"})
    if isinstance(stack.speaker, SilentSpeaker):
        assert resp.status_code == 503
    else:
        assert resp.status_code == 200
        assert resp.content[:4] == b"RIFF"


def test_voice_endpoints_503_without_stack(tmp_path, monkeypatch):
    from butler.webapp import create_app

    app = create_app(
        config=SimpleNamespace(
            model="m", device_backend="simulated", confirmation_mode="always",
            tts_backend="sapi", data_dir=tmp_path / "data",
        ),
        registry=None,
        memory_store=SimpleNamespace(list=lambda: [], add=lambda *a, **k: "1",
                                     delete=lambda mid: True,
                                     list_notifications=lambda: [],
                                     list_task_logs=lambda: []),
        agent=SimpleNamespace(process=lambda t: SimpleNamespace(reply="r", tool_events=[],
                                                               stopped_by_limit=False)),
        rule_engine=SimpleNamespace(run_checks=lambda: []),
        confirmer=SimpleNamespace(pending=lambda: [], respond=lambda cid, ok: True),
        planner=None,
        voice_stack=None,
    )
    client = TestClient(app)
    assert client.get("/api/voice/status").status_code == 503
    assert client.get("/api/voices").status_code == 503
    assert client.post("/api/tts", json={"text": "x"}).status_code == 503


def test_preset_voice_endpoint(app_client, tmp_path, monkeypatch):
    client, stack = app_client
    # 造一个假的官方示例资产目录
    asset_dir = tmp_path / "asset"
    asset_dir.mkdir()
    (asset_dir / "zero_shot_prompt.wav").write_bytes(make_wav_bytes())
    monkeypatch.setenv("COSYVOICE_ASSET_DIR", str(asset_dir))
    resp = client.post("/api/voices/preset")
    assert resp.status_code == 200
    voice = resp.json()["voice"]
    assert voice["consent_status"] == "granted"
    assert voice["display_name"] == "官方示例音色"
    # 资产缺失时报 400 而非 500
    monkeypatch.setenv("COSYVOICE_ASSET_DIR", str(tmp_path / "nope"))
    assert client.post("/api/voices/preset").status_code == 400


def test_cache_clear_endpoint(app_client):
    client, stack = app_client
    stack.cache.put_bytes("cosyvoice", "v1", "文本", b"a")
    resp = client.post("/api/voice/cache/clear")
    assert resp.status_code == 200
    assert resp.json()["cleared"] == 1
