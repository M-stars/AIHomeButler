"""阶段 12 CosyVoice 客户端 / Speaker / 降级 / 缓存测试。

全部使用 httpx.MockTransport 与 Fake 对象，不连接真实 CosyVoice 服务。
"""
import struct
from pathlib import Path

import httpx
import pytest

from butler.voice.cosyvoice_client import (
    CosyVoiceClient,
    CosyVoiceClientError,
    DEFAULT_PROMPT_PREFIX,
    pcm_to_wav,
)
from butler.voice.profiles import CONSENT_GRANTED, CONSENT_PENDING, VoiceProfileError
from butler.voice.tts import (
    AudioCache,
    CosyVoiceSpeaker,
    FallbackSpeaker,
    SilentSpeaker,
    SapiSpeaker,
    VoiceError,
)

FAKE_PCM = b"\x01\x02" * 8000  # 16000 字节 int16 PCM


def make_profile(tmp_path, granted=True):
    wav = tmp_path / "ref.wav"
    wav.write_bytes(b"RIFF----WAVEfmt ")  # 内容由 mock 服务端忽略，仅存在即可
    return {
        "voice_id": "testvoice01",
        "display_name": "测试音色",
        "reference_text": "参考录音原文",
        "consent_status": CONSENT_GRANTED if granted else CONSENT_PENDING,
        "_audio_file": str(wav),
    }


def make_client(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    defaults = dict(base_url="http://127.0.0.1:9880", timeout_seconds=5.0)
    defaults.update(kwargs)
    return CosyVoiceClient(transport=transport, **defaults)


# ---- pcm_to_wav ----

def test_pcm_to_wav_header_fields():
    wav = pcm_to_wav(FAKE_PCM, 24000)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    riff_size = struct.unpack("<I", wav[4:8])[0]
    assert riff_size == 36 + len(FAKE_PCM)
    channels, sample_rate = struct.unpack("<HH", wav[22:26])
    assert channels == 1 and sample_rate == 24000
    assert wav[44:] == FAKE_PCM


def test_pcm_to_wav_empty_raises():
    with pytest.raises(CosyVoiceClientError):
        pcm_to_wav(b"", 24000)


# ---- 客户端：请求格式与响应 ----

def test_zero_shot_request_format_and_success(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content.decode("utf-8", errors="replace")
        return httpx.Response(200, content=FAKE_PCM)

    client = make_client(handler)
    profile = make_profile(tmp_path)
    wav = client.synthesize("你好，卧室现在的温度是二十五度。", profile)
    assert wav[:4] == b"RIFF"
    assert captured["url"].endswith("/inference_zero_shot")
    # multipart 表单：包含 tts_text / prompt_text（带官方前缀）/ prompt_wav 文件
    assert "multipart/form-data" in captured["content_type"]
    body = captured["body"]
    assert 'name="tts_text"' in body
    assert "卧室现在的温度" in body
    assert 'name="prompt_text"' in body
    assert DEFAULT_PROMPT_PREFIX in body
    assert "参考录音原文" in body
    assert 'name="prompt_wav"' in body
    assert 'filename="ref.wav"' in body


def test_sft_request_when_no_profile():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode("utf-8", errors="replace")
        return httpx.Response(200, content=FAKE_PCM)

    client = make_client(handler)
    wav = client.synthesize("测试文本")
    assert wav[:4] == b"RIFF"
    assert captured["url"].endswith("/inference_sft")
    assert "spk_id" in captured["body"]


def test_text_length_and_empty_rejected_before_http():
    client = make_client(lambda request: httpx.Response(200, content=FAKE_PCM),
                         max_text_chars=10)
    with pytest.raises(CosyVoiceClientError) as excinfo:
        client.synthesize("超过十个字符的很长文本吧啦啦啦啦")
    assert excinfo.value.kind == "bad_request"
    with pytest.raises(CosyVoiceClientError) as excinfo:
        client.synthesize("   ")
    assert excinfo.value.kind == "bad_request"


def test_timeout_maps_to_timeout_kind():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    client = make_client(handler)
    with pytest.raises(CosyVoiceClientError) as excinfo:
        client.synthesize("文本")
    assert excinfo.value.kind == "timeout"


def test_connection_error_maps_to_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = make_client(handler)
    with pytest.raises(CosyVoiceClientError) as excinfo:
        client.synthesize("文本")
    assert excinfo.value.kind == "unavailable"


def test_server_error_maps_and_truncates_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Traceback...\nRuntimeError: CUDA out of memory")

    client = make_client(handler)
    with pytest.raises(CosyVoiceClientError) as excinfo:
        client.synthesize("文本")
    assert excinfo.value.kind == "server_error"
    assert "CUDA out of memory" in str(excinfo.value)
    assert "Traceback" not in str(excinfo.value)


def test_422_maps_to_bad_request():
    client = make_client(lambda request: httpx.Response(422, text="validation"))
    with pytest.raises(CosyVoiceClientError) as excinfo:
        client.synthesize("文本")
    assert excinfo.value.kind == "bad_request"


def test_health_check_true_and_false():
    client = make_client(lambda request: httpx.Response(200, content="ok"))
    assert client.health_check() is True

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client_down = make_client(broken)
    assert client_down.health_check() is False


def test_busy_when_concurrency_slot_held():
    client = make_client(lambda request: httpx.Response(200, content=FAKE_PCM))
    client._slots.acquire()  # 占满并发额度（模拟另一路合成在跑）
    try:
        with pytest.raises(CosyVoiceClientError) as excinfo:
            client.synthesize("文本")
        assert excinfo.value.kind == "busy"
    finally:
        client._slots.release()


def test_unauthorized_profile_rejected_by_client(tmp_path):
    client = make_client(lambda request: httpx.Response(200, content=FAKE_PCM))
    profile = make_profile(tmp_path, granted=False)
    profile["consent_status"] = CONSENT_PENDING
    with pytest.raises(VoiceProfileError):
        client.synthesize("文本", profile)


# ---- CosyVoiceSpeaker ----

class FakeClient:
    def __init__(self, fail=None):
        self.calls = []
        self._wav = pcm_to_wav(FAKE_PCM, 24000)  # 客户端应返回 WAV 字节
        self._fail = fail

    def synthesize(self, text, profile=None):
        self.calls.append((text, profile))
        if self._fail is not None:
            raise self._fail
        return self._wav


def _speaker(tmp_path, client, profiles=None):
    cache = AudioCache(tmp_path / "voices" / "cache")
    return CosyVoiceSpeaker(client, profiles, voice_id=None, cache=cache), cache


def test_speaker_speak_to_file_writes_wav(tmp_path):
    client = FakeClient()
    speaker, _cache = _speaker(tmp_path, client)
    out = tmp_path / "out.wav"
    result = speaker.speak_to_file("你好", out)
    assert result == out
    assert out.read_bytes()[:4] == b"RIFF"
    assert len(client.calls) == 1


def test_speaker_uses_cache_for_repeated_text(tmp_path):
    client = FakeClient()
    speaker, cache = _speaker(tmp_path, client)
    out1 = tmp_path / "a.wav"
    out2 = tmp_path / "b.wav"
    speaker.speak_to_file("重复文本", out1)
    speaker.speak_to_file("重复文本", out2)
    assert len(client.calls) == 1  # 第二次命中缓存，不再请求
    assert out1.read_bytes() == out2.read_bytes()


def test_speaker_voice_profile_flow(tmp_path):
    client = FakeClient()
    profiles = VoiceProfileManagerStub(tmp_path)
    speaker, _ = _speaker(tmp_path, client, profiles)
    speaker.set_voice("testvoice01")
    speaker.speak_to_file("你好", tmp_path / "o.wav")
    text, profile = client.calls[0]
    assert profile["voice_id"] == "testvoice01"
    assert profile["_audio_file"].endswith(".wav")


def test_speaker_voice_id_without_manager_is_non_degradable(tmp_path):
    client = FakeClient()
    speaker, _ = _speaker(tmp_path, client)
    speaker.set_voice("somevoice")
    with pytest.raises(VoiceError) as excinfo:
        speaker.speak_to_file("你好", tmp_path / "o.wav")
    assert excinfo.value.degradable is False


def test_speaker_maps_client_errors_with_degradable_flag(tmp_path):
    cases = [
        (CosyVoiceClientError("超时", "timeout"), True),
        (CosyVoiceClientError("连不上", "unavailable"), True),
        (CosyVoiceClientError("参数错", "bad_request"), False),
    ]
    for fail, expected_degradable in cases:
        client = FakeClient(fail=fail)
        speaker, _ = _speaker(tmp_path, client)
        with pytest.raises(VoiceError) as excinfo:
            speaker.speak_to_file("你好", tmp_path / "o.wav")
        assert excinfo.value.degradable is expected_degradable


class VoiceProfileManagerStub:
    """最小档案管理器替身：只为 CosyVoiceSpeaker 测试服务。"""

    def __init__(self, tmp_path):
        self._tmp = tmp_path

    def resolve_for_synthesis(self, voice_id):
        return make_profile(self._tmp)


# ---- FallbackSpeaker 降级 ----

class RecordingSpeaker(SilentSpeaker):
    def __init__(self):
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)


def test_fallback_speaker_degrades_on_degradable_error():
    class AlwaysFail(SilentSpeaker):
        def speak(self, text):
            raise VoiceError("服务不可达", degradable=True)

        def speak_to_file(self, text, path):
            raise VoiceError("服务不可达", degradable=True)

    primary = AlwaysFail()
    fallback = RecordingSpeaker()
    speaker = FallbackSpeaker(primary, fallback)
    speaker.speak("回复文本")
    assert fallback.spoken == ["回复文本"]
    assert speaker.last_degradation["from"] == "cosyvoice"
    assert speaker.last_degradation["to"] == "sapi"
    assert "服务不可达" in speaker.last_degradation["reason"]


def test_fallback_speaker_no_degrade_for_policy_error():
    class PolicyFail(SilentSpeaker):
        def speak(self, text):
            raise VoiceError("未获授权", degradable=False)

    fallback = RecordingSpeaker()
    speaker = FallbackSpeaker(PolicyFail(), fallback)
    with pytest.raises(VoiceError):
        speaker.speak("回复文本")
    assert fallback.spoken == []
    assert speaker.last_degradation is None


def test_fallback_speaker_no_degrade_when_primary_healthy():
    primary = RecordingSpeaker()
    fallback = RecordingSpeaker()
    speaker = FallbackSpeaker(primary, fallback)
    speaker.speak("文本")
    assert primary.spoken == ["文本"]
    assert fallback.spoken == []
    assert speaker.last_degradation is None


def test_fallback_speak_to_file(tmp_path):
    class FailToFile(SilentSpeaker):
        def speak_to_file(self, text, path):
            raise VoiceError("合成失败", degradable=True)

    class Fake(SilentSpeaker):
        def speak_to_file(self, text, path):
            Path(path).write_bytes(b"RIFFfake")
            return Path(path)

    speaker = FallbackSpeaker(FailToFile(), Fake())
    out = tmp_path / "x.wav"
    assert speaker.speak_to_file("文本", out) == out
    assert out.read_bytes() == b"RIFFfake"


# ---- AudioCache ----

def test_audio_cache_lru_eviction(tmp_path):
    import os

    cache = AudioCache(tmp_path / "cache", max_entries=2)
    # 逐个写入并显式把已有文件的 mtime 压到过去（1970 年附近，必然早于新写入的
    # 系统时间），使"淘汰最旧"的判定与文件系统时间粒度/文件名顺序完全无关
    p1 = cache.put_bytes("cosyvoice", "v1", "文本一", b"a")
    os.utime(p1, (1000000, 1000000))
    p2 = cache.put_bytes("cosyvoice", "v1", "文本二", b"b")
    os.utime(p2, (1000100, 1000100))
    cache.put_bytes("cosyvoice", "v1", "文本三", b"c")  # 最旧的"文本一"被淘汰
    assert not p1.exists()  # 用文件系统判断，避免 get() 的 LRU touch 干扰时间戳
    cache.put_bytes("cosyvoice", "v1", "文本四", b"d")  # 次旧的"文本二"被淘汰
    assert not p2.exists()
    assert len(list((tmp_path / "cache").glob("*.wav"))) == 2
    assert cache.get("cosyvoice", "v1", "文本一") is None
    assert cache.get("cosyvoice", "v1", "文本二") is None
    assert cache.get("cosyvoice", "v1", "文本三") is not None
    assert cache.get("cosyvoice", "v1", "文本四") is not None


def test_audio_cache_purge_voice_only_removes_that_voice(tmp_path):
    cache = AudioCache(tmp_path / "cache")
    cache.put_bytes("cosyvoice", "voiceA", "文本", b"a")
    cache.put_bytes("cosyvoice", "voiceB", "文本", b"b")
    removed = cache.purge_voice("voiceA")
    assert removed == 1
    assert cache.get("cosyvoice", "voiceA", "文本") is None
    assert cache.get("cosyvoice", "voiceB", "文本") is not None


# ---- 装配：后端选择 ----

def _load_config(tmp_path, monkeypatch, backend):
    monkeypatch.setenv("TTS_BACKEND", backend)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LLM_API_KEY", "test-key-offline")
    monkeypatch.setenv("COSYVOICE_URL", "http://127.0.0.1:9880")
    from butler.config import load_config

    return load_config()


def test_backend_default_is_sapi(tmp_path, monkeypatch):
    monkeypatch.delenv("TTS_BACKEND", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LLM_API_KEY", "test-key-offline")
    from butler.config import load_config

    config = load_config()
    assert config.tts_backend == "sapi"


def test_backend_sapi_uses_plain_speaker(tmp_path, monkeypatch):
    from butler.voice.assembly import build_tts_stack

    config = _load_config(tmp_path, monkeypatch, "sapi")
    stack = build_tts_stack(config)
    assert stack.backend == "sapi"
    # SAPI 不可用时装配降级为静音；可用时应为 SapiSpeaker 本体
    assert isinstance(stack.speaker, (SapiSpeaker, SilentSpeaker))
    assert not isinstance(stack.speaker, FallbackSpeaker)


def test_backend_cosyvoice_wraps_fallback(tmp_path, monkeypatch):
    from butler.voice.assembly import build_tts_stack

    config = _load_config(tmp_path, monkeypatch, "cosyvoice")
    stack = build_tts_stack(config)
    assert stack.backend == "cosyvoice"
    assert isinstance(stack.speaker, FallbackSpeaker)
    assert isinstance(stack.cosyvoice_speaker, CosyVoiceSpeaker)
    assert stack.fallback_speaker is not None


def test_backend_cosyvoice_no_fallback_configured(tmp_path, monkeypatch):
    from butler.voice.assembly import build_tts_stack

    config = _load_config(tmp_path, monkeypatch, "cosyvoice")
    monkeypatch.setenv("COSYVOICE_FALLBACK_SAPI", "0")
    # 重新加载配置使新 env 生效
    from butler.config import load_config

    config = load_config()
    stack = build_tts_stack(config)
    assert isinstance(stack.speaker, CosyVoiceSpeaker)
    assert stack.fallback_speaker is None


def test_invalid_backend_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("TTS_BACKEND", "piper")
    monkeypatch.setenv("LLM_API_KEY", "test-key-offline")
    from butler.config import load_config, ConfigError

    with pytest.raises(ConfigError):
        load_config()


def test_stack_status_shape(tmp_path, monkeypatch):
    from butler.voice.assembly import build_tts_stack

    config = _load_config(tmp_path, monkeypatch, "cosyvoice")
    # 与真实环境解耦：指向必然无服务的端口（本机可能恰好跑着真实 CosyVoice）
    monkeypatch.setenv("COSYVOICE_URL", "http://127.0.0.1:59999")
    from butler.config import load_config

    stack = build_tts_stack(load_config())
    status = stack.status()
    assert set(status) >= {
        "backend", "cosyvoice_healthy", "fallback_to_sapi",
        "default_voice", "voice_count", "last_degradation", "ai_disclaimer",
    }
    assert status["backend"] == "cosyvoice"
    assert status["cosyvoice_healthy"] is False  # 确定性不可达
