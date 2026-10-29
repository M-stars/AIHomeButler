"""阶段 13 Web 家庭成员声纹 API 测试（TestClient + Fake 客户端，不触真实服务）。"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from butler.voice.cosyvoice_client import CosyVoiceClientError, pcm_to_wav

FAKE_EMBED = [1.0] + [0.0] * 191


class FakeEmbedClient:
    """声纹提取替身：返回固定向量，可编程失败。"""

    def __init__(self):
        self.embedding = list(FAKE_EMBED)
        self.fail = None
        self.calls = []

    def speaker_embedding(self, wav_bytes, filename="clip.wav"):
        self.calls.append((len(wav_bytes), filename))
        if self.fail is not None:
            raise self.fail
        return list(self.embedding)

    def health_check(self, timeout_seconds: float = 5.0) -> bool:
        return True


def build_app(tmp_path, monkeypatch, fake=None, enabled=True):
    from butler.webapp import create_app
    from butler.voice.assembly import build_speaker_identity
    from butler.voice.profiles import VoiceProfileManager

    fake = fake or FakeEmbedClient()
    identity = build_speaker_identity(
        SimpleNamespace(data_dir=tmp_path / "data", speaker_recognition=enabled,
                        speaker_threshold=0.30),
        client=fake,
    )
    app = create_app(
        config=SimpleNamespace(
            model="m", device_backend="simulated", confirmation_mode="always",
            tts_backend="cosyvoice", data_dir=tmp_path / "data",
        ),
        registry=None,
        memory_store=SimpleNamespace(
            list=lambda *a, **k: [], add=lambda *a, **k: "1", delete=lambda mid: True,
            list_notifications=lambda: [], list_task_logs=lambda: [],
        ),
        agent=SimpleNamespace(process=lambda t: SimpleNamespace(
            reply="r", tool_events=[], stopped_by_limit=False)),
        rule_engine=SimpleNamespace(run_checks=lambda: []),
        confirmer=SimpleNamespace(pending=lambda: [], respond=lambda cid, ok: True),
        planner=None,
        voice_stack=SimpleNamespace(client=fake),
        speaker_identity=identity,
    )
    return TestClient(app), identity, fake


WAV_BYTES = pcm_to_wav(b"\x01\x02" * 4000, 24000)


def test_speakers_status_shape(tmp_path, monkeypatch):
    client, identity, _fake = build_app(tmp_path, monkeypatch)
    resp = client.get("/api/speakers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["speaker_recognition"] is True
    assert body["member_count"] == 0
    assert body["service_healthy"] is True
    assert "不作为身份验证" in body["ai_disclaimer"]


def test_enroll_list_delete_flow(tmp_path, monkeypatch):
    client, identity, fake = build_app(tmp_path, monkeypatch)
    resp = client.post("/api/speakers", data={
        "display_name": "爸爸", "consent_granted": "true", "voice_id": "",
    }, files={"wav": ("clip.wav", WAV_BYTES, "audio/wav")})
    assert resp.status_code == 200
    speaker = resp.json()["speaker"]
    assert speaker["display_name"] == "爸爸"
    assert "embedding" not in speaker  # 向量永不出 API
    assert fake.calls and fake.calls[0][0] == len(WAV_BYTES)  # 音频进了提取端点

    listing = client.get("/api/speakers").json()
    assert listing["member_count"] == 1
    assert all("embedding" not in m for m in listing["members"])

    # 删除
    resp = client.delete(f"/api/speakers/{speaker['member_id']}")
    assert resp.status_code == 200
    assert client.get("/api/speakers").json()["member_count"] == 0


def test_enroll_requires_consent(tmp_path, monkeypatch):
    client, _identity, _fake = build_app(tmp_path, monkeypatch)
    resp = client.post("/api/speakers", data={
        "display_name": "爸爸", "consent_granted": "false",
    }, files={"wav": ("clip.wav", WAV_BYTES, "audio/wav")})
    assert resp.status_code == 400
    assert "授权" in resp.json()["detail"]


def test_enroll_service_error_mapping(tmp_path, monkeypatch):
    client, _identity, fake = build_app(tmp_path, monkeypatch)
    fake.fail = CosyVoiceClientError("服务离线", "unavailable")
    resp = client.post("/api/speakers", data={
        "display_name": "爸爸", "consent_granted": "true",
    }, files={"wav": ("clip.wav", WAV_BYTES, "audio/wav")})
    assert resp.status_code == 503
    fake.fail = CosyVoiceClientError("太慢", "timeout")
    resp = client.post("/api/speakers", data={
        "display_name": "爸爸", "consent_granted": "true",
    }, files={"wav": ("clip.wav", WAV_BYTES, "audio/wav")})
    assert resp.status_code == 504


def test_duplicate_name_rejected(tmp_path, monkeypatch):
    client, _identity, _fake = build_app(tmp_path, monkeypatch)
    for _ in range(2):
        resp = client.post("/api/speakers", data={
            "display_name": "爸爸", "consent_granted": "true",
        }, files={"wav": ("clip.wav", WAV_BYTES, "audio/wav")})
    assert resp.status_code == 400
    assert "已存在" in resp.json()["detail"]


def test_bind_voice_and_rename(tmp_path, monkeypatch):
    client, identity, _fake = build_app(tmp_path, monkeypatch)
    resp = client.post("/api/speakers", data={
        "display_name": "妈妈", "consent_granted": "true",
    }, files={"wav": ("clip.wav", WAV_BYTES, "audio/wav")})
    mid = resp.json()["speaker"]["member_id"]

    form = {"voice_id": "voice123"}
    resp = client.post(f"/api/speakers/{mid}/voice", data=form)
    assert resp.status_code == 200
    assert resp.json()["speaker"]["voice_id"] == "voice123"
    # 恢复默认
    resp = client.post(f"/api/speakers/{mid}/voice", data={"voice_id": ""})
    assert resp.json()["speaker"]["voice_id"] is None

    resp = client.post(f"/api/speakers/{mid}/name", data={"display_name": "老妈"})
    assert resp.status_code == 200
    assert resp.json()["speaker"]["display_name"] == "老妈"
    # 重名拒绝
    client.post("/api/speakers", data={
        "display_name": "爸爸", "consent_granted": "true",
    }, files={"wav": ("clip.wav", WAV_BYTES, "audio/wav")})
    resp = client.post(f"/api/speakers/{mid}/name", data={"display_name": "爸爸"})
    assert resp.status_code == 400


def test_speakers_503_without_identity(tmp_path, monkeypatch):
    from butler.webapp import create_app

    app = create_app(
        config=SimpleNamespace(
            model="m", device_backend="simulated", confirmation_mode="always",
            tts_backend="sapi", data_dir=tmp_path / "data",
        ),
        registry=None,
        memory_store=SimpleNamespace(
            list=lambda *a, **k: [], add=lambda *a, **k: "1", delete=lambda mid: True,
            list_notifications=lambda: [], list_task_logs=lambda: [],
        ),
        agent=SimpleNamespace(process=lambda t: SimpleNamespace(
            reply="r", tool_events=[], stopped_by_limit=False)),
        rule_engine=SimpleNamespace(run_checks=lambda: []),
        confirmer=SimpleNamespace(pending=lambda: [], respond=lambda cid, ok: True),
        planner=None,
        voice_stack=None,
        speaker_identity=None,
    )
    client = TestClient(app)
    assert client.get("/api/speakers").status_code == 503
    # 缺必填字段时 FastAPI 参数校验(422)先于装配检查(503)触发，均为拒绝
    assert client.post("/api/speakers", data={"display_name": "x"}).status_code in (422, 503)
