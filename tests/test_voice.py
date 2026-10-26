"""阶段 8 语音层测试：SAPI TTS 真实合成、可选依赖的优雅降级。"""
import importlib.util

import pytest

from butler.voice.tts import (
    SapiSpeaker,
    SilentSpeaker,
    Transcriber,
    VoiceError,
    WhisperTranscriber,
)


def test_silent_speaker_never_raises():
    speaker = SilentSpeaker()
    speaker.speak("测试")
    with pytest.raises(VoiceError):
        speaker.speak_to_file("测试", None)


def test_sapi_tts_synthesizes_real_audio_file(tmp_path):
    """Windows 自带 SAPI5：合成到文件并验证非空（无 SAPI 环境自动跳过）。"""
    try:
        speaker = SapiSpeaker()
    except VoiceError as exc:
        pytest.skip(f"本机无可用 SAPI 语音引擎：{exc}")
    out = tmp_path / "tts_test.wav"
    result = speaker.speak_to_file("客厅温度二十三度", out)
    assert result == out
    assert out.exists()
    assert out.stat().st_size > 1000  # 16bit PCM 至少应有可感知的体积


def test_transcriber_interface_raises_by_default():
    with pytest.raises(NotImplementedError):
        Transcriber().transcribe_wav("x.wav")


@pytest.mark.skipif(
    importlib.util.find_spec("faster_whisper") is not None,
    reason="faster-whisper 已安装，降级路径不再适用",
)
def test_whisper_missing_gives_actionable_error():
    with pytest.raises(VoiceError) as excinfo:
        WhisperTranscriber()
    assert "faster-whisper" in str(excinfo.value)
    assert "pip install" in str(excinfo.value)
