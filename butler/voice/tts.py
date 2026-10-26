"""语音交互层（阶段 8）。

策略（与需求 4.6 一致）：
  - 语音识别/合成的长期方案是复用 Home Assistant Voice / Wyoming 生态（见
    docs/VOICE.md），本项目不自行实现识别或合成引擎；
  - 本模块提供 Windows 下的桥接实现：
      * TTS：pyttsx3 → Windows SAPI5（离线可用，真实实现）；
      * ASR：接口 + 可选 faster-whisper（本机安装后自动启用），未安装时给出明确指引；
      * 麦克风采集：可选 sounddevice（本机安装后自动启用）。
  - 所有可选依赖均为延迟导入：未安装不影响系统其余功能。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

_log = logging.getLogger("butler.voice")


class VoiceError(Exception):
    """语音功能不可用或失败，message 面向用户可读。"""


class Speaker:
    """语音输出接口。"""

    def speak(self, text: str) -> None:
        raise NotImplementedError

    def speak_to_file(self, text: str, path: Path) -> Path:
        raise NotImplementedError


class SapiSpeaker(Speaker):
    """Windows SAPI5 离线语音合成（pyttsx3）。"""

    def __init__(self, rate: int = 180, volume: float = 1.0) -> None:
        try:
            import pyttsx3
        except ImportError as exc:
            raise VoiceError("pyttsx3 未安装：pip install pyttsx3") from exc
        try:
            self._engine = pyttsx3.init()
        except Exception as exc:  # 无声卡/SAPI 异常等
            raise VoiceError(f"Windows 语音引擎初始化失败：{exc}") from exc
        self._engine.setProperty("rate", rate)
        self._engine.setProperty("volume", max(0.0, min(1.0, volume)))

    def speak(self, text: str) -> None:
        if not text:
            return
        self._engine.say(text)
        self._engine.runAndWait()

    def speak_to_file(self, text: str, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._engine.save_to_file(text, str(path))
        self._engine.runAndWait()
        if not path.exists() or path.stat().st_size == 0:
            raise VoiceError("语音文件生成失败")
        return path


class SilentSpeaker(Speaker):
    """无语音输出环境下的占位实现：只写日志。"""

    def speak(self, text: str) -> None:
        _log.info("[静音模式] %s", text)

    def speak_to_file(self, text: str, path: Path) -> Path:
        raise VoiceError("静音模式不支持生成语音文件")


class Transcriber:
    """语音识别接口。"""

    def transcribe_wav(self, wav_path: Path) -> str:
        raise NotImplementedError


class WhisperTranscriber(Transcriber):
    """faster-whisper 本地识别（可选依赖，安装后自动可用）。"""

    def __init__(self, model_size: str = "small") -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise VoiceError(
                "faster-whisper 未安装：pip install faster-whisper"
                "（首次使用会下载模型，约数百 MB）"
            ) from exc
        self._model = WhisperModel(model_size, device="cpu", compute_type="int8")
        self._model_size = model_size

    def transcribe_wav(self, wav_path: Path) -> str:
        segments, _info = self._model.transcribe(str(wav_path), language="zh")
        return "".join(segment.text for segment in segments).strip()


class MicrophoneRecorder:
    """麦克风采集（可选依赖 sounddevice）。"""

    def __init__(self, sample_rate: int = 16000, silence_seconds: float = 1.2,
                 max_seconds: float = 12.0) -> None:
        try:
            import sounddevice  # noqa: F401
        except ImportError as exc:
            raise VoiceError(
                "sounddevice 未安装：pip install sounddevice numpy"
            ) from exc
        self._sample_rate = sample_rate
        self._silence = silence_seconds
        self._max = max_seconds

    def record_to_wav(self, path: Path) -> Path:
        import queue as _queue
        import wave as _wave

        import numpy as np
        import sounddevice as sd

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        frames: _queue.Queue = _queue.Queue()
        threshold = 0.01

        def callback(indata, _frames, _time, _status):
            frames.put(indata.copy())

        recorded = []
        silent_run = 0.0
        total = 0.0
        with sd.InputStream(samplerate=self._sample_rate, channels=1, dtype="float32",
                            blocksize=int(self._sample_rate * 0.1), callback=callback):
            while total < self._max:
                block = frames.get()
                recorded.append(block)
                total += len(block) / self._sample_rate
                level = float(np.abs(block).mean())
                if level < threshold and total > 0.6:
                    silent_run += 0.1
                    if silent_run >= self._silence:
                        break
                else:
                    silent_run = 0.0
        audio = np.concatenate(recorded)[:, 0] if recorded else np.zeros(1, dtype="float32")
        pcm = (audio * 32767).astype("int16")
        with _wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self._sample_rate)
            wf.writeframes(pcm.tobytes())
        return path
