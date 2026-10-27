"""阶段 12 音色档案库测试：CRUD、授权门禁、格式校验、路径安全。

全部使用 tmp_path 与构造的音频字节，不触真实 CosyVoice 服务。
"""
import json
import struct

import pytest

from butler.voice.profiles import (
    CONSENT_GRANTED,
    CONSENT_PENDING,
    MAX_AUDIO_BYTES,
    MAX_WAV_SECONDS,
    VoiceProfileError,
    VoiceProfileManager,
    voice_profile_require_consent,
)


def make_wav_bytes(seconds: float = 2.0, sample_rate: int = 16000) -> bytes:
    """构造一个合法的 RIFF/WAV 字节流（正弦占位 PCM）。"""
    frames = int(seconds * sample_rate)
    pcm = b"\x00\x01" * frames
    fmt = struct.pack("<HHIIHH", 1, 1, sample_rate, sample_rate * 2, 2, 16)
    return (
        b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
        + b"fmt " + struct.pack("<I", 16) + fmt
        + b"data" + struct.pack("<I", len(pcm)) + pcm
    )


@pytest.fixture()
def manager(tmp_path):
    return VoiceProfileManager(tmp_path / "voices")


def test_create_list_get_delete_roundtrip(manager):
    profile = manager.create("我的声音", "你好世界", make_wav_bytes(), ".wav",
                             consent_granted=True)
    assert profile["consent_status"] == CONSENT_GRANTED
    assert profile["display_name"] == "我的声音"
    assert profile["reference_audio_path"].startswith("audio/")
    assert manager.get(profile["voice_id"])["voice_id"] == profile["voice_id"]
    assert len(manager.list()) == 1

    # 授权状态更新
    updated = manager.update_consent(profile["voice_id"], False)
    assert updated["consent_status"] == CONSENT_PENDING

    # 删除：档案与录音文件一并清除
    removed = manager.delete(profile["voice_id"])
    assert removed["voice_id"] == profile["voice_id"]
    assert manager.list() == []
    assert not (manager._audio_dir / f"{profile['voice_id']}.wav").exists()


def test_first_profile_becomes_default_and_set_default_switches(manager):
    a = manager.create("甲", "参考文本", make_wav_bytes(), ".wav", consent_granted=True)
    assert a["is_default"] is True
    b = manager.create("乙", "参考文本", make_wav_bytes(), ".wav", consent_granted=True)
    assert b["is_default"] is False
    manager.set_default(b["voice_id"])
    assert manager.default()["voice_id"] == b["voice_id"]
    assert manager.get(a["voice_id"])["is_default"] is False


def test_create_requires_display_name_and_reference_text(manager):
    with pytest.raises(VoiceProfileError):
        manager.create("", "文本", make_wav_bytes(), ".wav")
    with pytest.raises(VoiceProfileError):
        manager.create("名字", "  ", make_wav_bytes(), ".wav")


def test_reject_invalid_audio_format_by_magic_bytes(manager):
    # 扩展名是 .wav 但内容不是 RIFF → 拒绝（不信任扩展名）
    with pytest.raises(VoiceProfileError):
        manager.create("假wav", "文本", b"this is not audio at all", ".wav")
    # 完全不支持的扩展名
    with pytest.raises(VoiceProfileError):
        manager.create("文本文件", "文本", b"hello", ".txt")
    # mp3 必须以 ID3 或帧同步字节开头
    with pytest.raises(VoiceProfileError):
        manager.create("假mp3", "文本", b"\x00\x01\x02", ".mp3")


def test_reject_oversized_audio(manager):
    tiny_manager = VoiceProfileManager(manager._dir, max_audio_bytes=16)
    with pytest.raises(VoiceProfileError):
        tiny_manager.create("太大", "文本", make_wav_bytes(), ".wav")
    assert len(tiny_manager.list()) == 0


def test_reject_overlong_wav(manager):
    with pytest.raises(VoiceProfileError):
        manager.create("太长", "文本", make_wav_bytes(seconds=MAX_WAV_SECONDS + 10), ".wav")


def test_consent_gate_blocks_unauthorized_clone(manager):
    profile = manager.create("未授权", "参考文本", make_wav_bytes(), ".wav",
                             consent_granted=False)
    assert profile["consent_status"] == CONSENT_PENDING
    with pytest.raises(VoiceProfileError) as excinfo:
        voice_profile_require_consent(profile)
    assert "授权" in str(excinfo.value)
    # 授权后门禁放行（不抛异常）
    granted = manager.update_consent(profile["voice_id"], True)
    voice_profile_require_consent(granted)


def test_path_traversal_blocked(manager):
    profile = manager.create("正常", "文本", make_wav_bytes(), ".wav")
    # 篡改索引文件，把录音路径指到数据目录之外
    index = manager._index_path
    payload = json.loads(index.read_text(encoding="utf-8"))
    payload["profiles"][0]["reference_audio_path"] = "../../evil.wav"
    index.write_text(json.dumps(payload), encoding="utf-8")
    manager._profiles = manager._load()
    with pytest.raises(VoiceProfileError):
        manager.audio_path(profile["voice_id"])
    # 非法 voice_id 一律拒绝（含路径分隔符）
    with pytest.raises(VoiceProfileError):
        manager.get("../evil")
    with pytest.raises(VoiceProfileError):
        manager.get("a/b")


def test_missing_audio_file_detected(manager):
    profile = manager.create("缺文件", "文本", make_wav_bytes(), ".wav")
    (manager._audio_dir / f"{profile['voice_id']}.wav").unlink()
    with pytest.raises(VoiceProfileError):
        manager.audio_path(profile["voice_id"])


def test_profiles_persist_across_manager_instances(tmp_path):
    m1 = VoiceProfileManager(tmp_path / "voices")
    profile = m1.create("持久化", "文本", make_wav_bytes(), ".wav", consent_granted=True)
    m2 = VoiceProfileManager(tmp_path / "voices")
    assert m2.get(profile["voice_id"])["display_name"] == "持久化"
    assert m2.default()["voice_id"] == profile["voice_id"]


def test_corrupted_index_gives_actionable_error(tmp_path):
    (tmp_path / "voices").mkdir()
    (tmp_path / "voices" / "profiles.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(VoiceProfileError):
        VoiceProfileManager(tmp_path / "voices")


def test_create_from_source_path_for_preset(tmp_path):
    src = tmp_path / "sample.wav"
    src.write_bytes(make_wav_bytes())
    m = VoiceProfileManager(tmp_path / "voices")
    profile = m.create("官方示例", "参考文本", b"", ".wav",
                       consent_granted=True, source_path=src)
    assert profile["consent_status"] == CONSENT_GRANTED
    assert m.audio_path(profile["voice_id"]).read_bytes() == src.read_bytes()
    # bytes 与 source_path 互斥
    with pytest.raises(VoiceProfileError):
        m.create("冲突", "文本", b"x", ".wav", source_path=src)


def test_max_audio_bytes_default_is_10mb():
    assert MAX_AUDIO_BYTES == 10 * 1024 * 1024
