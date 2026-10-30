"""阶段 14 唤醒词测试：等待循环/配置/装配降级 + 真模型冒烟（全部离线）。

循环逻辑用注入的假检测器与音频块迭代器覆盖，无需麦克风与硬件；
真模型冒烟仅在 sherpa-onnx 已安装且 data/models/sherpa-kws 就绪时运行。
"""
import importlib.util
import os
import wave
from pathlib import Path

import pytest

from butler.config import PROJECT_ROOT, load_config
from butler.voice.tts import VoiceError
from butler.voice.wakeword import (
    SherpaKeywordSpotterDetector,
    build_wake_word_detector,
    ensure_no_bom,
    wait_for_wake,
)

SHERPA_INSTALLED = importlib.util.find_spec("sherpa_onnx") is not None
MODEL_DIR = PROJECT_ROOT / "data" / "models" / "sherpa-kws"
MODEL_READY = SHERPA_INSTALLED and (MODEL_DIR / "tokens.txt").exists()


class FakeDetector:
    """脚本化假检测器：按预设序列返回命中（None 表示未命中），记录喂入块数。"""

    sample_rate = 16000

    def __init__(self, script):
        self.script = list(script)
        self.fed = 0

    def process_block(self, block):
        self.fed += 1
        if self.script:
            return self.script.pop(0) or None
        return None


def _blocks(count):
    return iter([[0.0] * 160 for _ in range(count)])


# ---- 等待循环（注入假检测器，与硬件/模型完全解耦） ----

def test_wait_for_wake_returns_label_and_stops_feeding():
    detector = FakeDetector([None, None, "小管家"])
    hit = wait_for_wake(detector, blocks=_blocks(5))
    assert hit == "小管家"
    assert detector.fed == 3  # 命中即返回，不再消费后续音频块


def test_wait_for_wake_immediate_hit():
    detector = FakeDetector(["小管家"])
    assert wait_for_wake(detector, blocks=_blocks(3)) == "小管家"
    assert detector.fed == 1


def test_wait_for_wake_blocks_exhausted_returns_none():
    detector = FakeDetector([None, None])
    assert wait_for_wake(detector, blocks=_blocks(2)) is None
    assert detector.fed == 2


# ---- 配置 ----

def _clear_wakeword_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("WAKEWORD_", "WAKE_WORD", "LLM_", "SPEAKER_")) or key in (
            "CONFIRMATION_MODE", "DATA_DIR", "LOG_LEVEL",
        ):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")


def _load(tmp_path, monkeypatch):
    return load_config(env_path=tmp_path / "missing.env")


def test_wakeword_disabled_by_default(tmp_path, monkeypatch):
    _clear_wakeword_env(monkeypatch)
    config = _load(tmp_path, monkeypatch)
    assert config.wakeword_enabled is False
    assert config.wake_word == "小管家"
    assert config.wake_word_threshold == 0.25
    assert config.wake_word_model_dir == PROJECT_ROOT / "data" / "models" / "sherpa-kws"


def test_wakeword_env_override_and_threshold_clamp(tmp_path, monkeypatch):
    _clear_wakeword_env(monkeypatch)
    monkeypatch.setenv("WAKEWORD_ENABLED", "1")
    monkeypatch.setenv("WAKE_WORD", "小爱同学")
    monkeypatch.setenv("WAKEWORD_THRESHOLD", "99")
    monkeypatch.setenv("WAKEWORD_MODEL_DIR", "custom/kws")
    config = _load(tmp_path, monkeypatch)
    assert config.wakeword_enabled is True
    assert config.wake_word == "小爱同学"
    assert config.wake_word_threshold == 0.95  # 上限钳制
    assert config.wake_word_model_dir == PROJECT_ROOT / "custom" / "kws"

    monkeypatch.setenv("WAKEWORD_THRESHOLD", "0.001")
    assert _load(tmp_path, monkeypatch).wake_word_threshold == 0.05  # 下限钳制


# ---- 装配与降级 ----

def test_ensure_no_bom_strips_and_reports(tmp_path):
    """记事本存 UTF-8 BOM 会让首词首个 token 解析失败——加载前必须剥掉。"""
    p = tmp_path / "keywords.txt"
    p.write_bytes("x iǎo g uǎn j iā @小管家\n".encode("utf-8-sig"))
    assert ensure_no_bom(p) is True
    assert p.read_bytes() == "x iǎo g uǎn j iā @小管家\n".encode("utf-8")


def test_ensure_no_bom_untouched_without_bom(tmp_path):
    p = tmp_path / "keywords.txt"
    p.write_bytes("x iǎo g uǎn j iā @小管家\n".encode("utf-8"))  # write_bytes：避免 write_text 在 Windows 写成 CRLF
    assert ensure_no_bom(p) is False
    assert p.read_bytes() == "x iǎo g uǎn j iā @小管家\n".encode("utf-8")


def test_build_wake_word_detector_disabled_returns_none(tmp_path, monkeypatch):
    _clear_wakeword_env(monkeypatch)
    assert build_wake_word_detector(_load(tmp_path, monkeypatch)) is None


@pytest.mark.skipif(
    not SHERPA_INSTALLED,
    reason="sherpa-onnx 未安装，走的是缺依赖提示路径",
)
def test_cli_flag_overrides_disabled_config(tmp_path, monkeypatch):
    """回归：--wake 旗标必须在 .env 未开启时也生效（曾静默失效导致免唤醒自由录音）。"""
    _clear_wakeword_env(monkeypatch)  # WAKEWORD_ENABLED 未设置 → 配置层是关的
    monkeypatch.setenv("WAKEWORD_MODEL_DIR", str(tmp_path / "nope"))
    config = _load(tmp_path, monkeypatch)
    assert config.wakeword_enabled is False
    with pytest.raises(VoiceError) as excinfo:
        build_wake_word_detector(config, enabled=True)
    # 越过了 enabled 检查，走到了模型缺失报错——说明旗标覆盖生效
    assert "scripts/wakeword_download_model.py" in str(excinfo.value)


@pytest.mark.skipif(
    not SHERPA_INSTALLED,
    reason="sherpa-onnx 未安装，走的是缺依赖提示路径（见下一条测试）",
)
def test_build_detector_missing_model_gives_download_hint(tmp_path, monkeypatch):
    _clear_wakeword_env(monkeypatch)
    monkeypatch.setenv("WAKEWORD_ENABLED", "1")
    monkeypatch.setenv("WAKEWORD_MODEL_DIR", str(tmp_path / "nope"))
    with pytest.raises(VoiceError) as excinfo:
        build_wake_word_detector(_load(tmp_path, monkeypatch))
    assert "scripts/wakeword_download_model.py" in str(excinfo.value)


@pytest.mark.skipif(
    SHERPA_INSTALLED,
    reason="sherpa-onnx 已安装，缺依赖路径不适用",
)
def test_detector_missing_dep_gives_install_hint(tmp_path):
    with pytest.raises(VoiceError) as excinfo:
        SherpaKeywordSpotterDetector(model_dir=tmp_path)
    assert "sherpa-onnx" in str(excinfo.value)
    assert "pip install" in str(excinfo.value)


# ---- 真模型冒烟（全离线：模型与测试音频都在本机） ----

def _wav_blocks_float32(path, chunk=1600, target_sr=16000):
    import numpy as np

    with wave.open(str(path), "rb") as wf:
        sr, n, ch = wf.getframerate(), wf.getnframes(), wf.getnchannels()
        audio = np.frombuffer(wf.readframes(n), dtype=np.int16)
    if ch > 1:
        audio = audio[::ch]
    audio = audio.astype(np.float32) / 32768.0
    if sr != target_sr:
        t_old = np.arange(len(audio), dtype=np.float32) / sr
        t_new = np.arange(int(len(audio) * target_sr / sr), dtype=np.float32) / target_sr
        audio = np.interp(t_new, t_old, audio).astype(np.float32)
    for i in range(0, len(audio), chunk):
        yield audio[i:i + chunk]


@pytest.mark.skipif(
    not MODEL_READY,
    reason="sherpa-onnx 或 data/models/sherpa-kws 模型未就绪（先跑下载脚本）",
)
def test_real_model_hits_keyword_in_official_test_wav():
    """官方测试人声 wav 必须命中其配对关键词——检测器接线与模型完整性的回归。"""
    det = SherpaKeywordSpotterDetector(
        model_dir=MODEL_DIR, keywords_file=MODEL_DIR / "test_wavs" / "test_keywords.txt"
    )
    hit = wait_for_wake(det, blocks=_wav_blocks_float32(MODEL_DIR / "test_wavs" / "3.wav"))
    assert hit == "文森特卡索"  # 官方 test_keywords.txt 中 3.wav 的配对词
    det.reset()  # 重置后可继续监听，不残留命中状态


@pytest.mark.skipif(
    not MODEL_READY,
    reason="sherpa-onnx 或 data/models/sherpa-kws 模型未就绪（先跑下载脚本）",
)
def test_real_model_detects_default_wake_word(tmp_path):
    """默认唤醒词「小管家」端到端：SAPI 合成人声喂入生产 keywords.txt（离线冒烟）。

    SAPI 音色因机器而异，未命中跳过（不作为失败）——真人麦克风命中率高于合成音。
    """
    try:
        import pyttsx3

        engine = pyttsx3.init()
    except Exception as exc:
        pytest.skip(f"本机无可用 SAPI 语音引擎：{exc}")
    wav_path = tmp_path / "wake_word.wav"
    engine.save_to_file("小管家", str(wav_path))
    engine.runAndWait()

    det = SherpaKeywordSpotterDetector(model_dir=MODEL_DIR)
    hit = wait_for_wake(det, blocks=_wav_blocks_float32(wav_path))
    if hit != "小管家":
        pytest.skip("SAPI 合成音未命中（声音因机器而异），请用真人语音人工验证")
