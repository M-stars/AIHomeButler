"""语音交互层（阶段 8；阶段 12 扩展 CosyVoice 后端）。

策略（与需求 4.6 一致）：
  - 语音识别/合成的长期方案是复用 Home Assistant Voice / Wyoming 生态（见
    docs/VOICE.md），本项目不自行实现识别或合成引擎；
  - 本模块提供 Windows 下的桥接实现：
      * TTS：pyttsx3 → Windows SAPI5（离线可用，真实实现）；
        CosyVoice → 官方独立服务端（可选后端，见 cosyvoice_client.py）；
      * ASR：接口 + 可选 faster-whisper（本机安装后自动启用），未安装时给出明确指引；
      * 麦克风采集：可选 sounddevice（本机安装后自动启用）。
  - 所有可选依赖均为延迟导入：未安装不影响系统其余功能。
  - 后端选择与失败降级：FallbackSpeaker 包装主/备两个 Speaker，降级必留痕
    （日志 + last_degradation 状态），绝不静默换声。
"""
from __future__ import annotations

import hashlib
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from .cosyvoice_client import CosyVoiceClient, CosyVoiceClientError
from .profiles import VoiceProfileError, VoiceProfileManager

_log = logging.getLogger("butler.voice")


class VoiceError(Exception):
    """语音功能不可用或失败，message 面向用户可读。

    degradable=False 表示换一个语音后端也无济于事的策略性失败
    （如音色未获说话人授权），FallbackSpeaker 不应对其降级。
    """

    def __init__(self, message: str, *, degradable: bool = True) -> None:
        super().__init__(message)
        self.degradable = degradable


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


class AudioCache:
    """生成音频缓存（LRU by mtime）：同文本+同音色的重复播报不再重跑模型。

    只缓存合成语音文件，缓存内容永远落在数据目录（voices_dir/cache），
    不进代码仓库；clear() 供界面"清空生成缓存"，purge_voice() 在删除
    音色档案时清掉该音色的生成音频（隐私默认：档案删除则生成物一并清除）。
    """

    def __init__(self, cache_dir: Path, max_entries: int = 64) -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max = max(1, int(max_entries))

    @staticmethod
    def _voice_tag(voice_id: str) -> str:
        return voice_id if voice_id.replace("_", "").replace("-", "").isalnum() else "v"

    def _filename(self, backend_tag: str, voice_id: str, text: str) -> str:
        raw = f"{backend_tag}|{voice_id}|{text}".encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()[:24]
        return f"{self._voice_tag(voice_id)}_{digest}.wav"

    def get(self, backend_tag: str, voice_id: str, text: str) -> Optional[Path]:
        path = self._dir / self._filename(backend_tag, voice_id, text)
        if path.exists():
            path.touch()  # 刷新 mtime 实现 LRU
            return path
        return None

    def put_bytes(self, backend_tag: str, voice_id: str, text: str, data: bytes) -> Path:
        path = self._dir / self._filename(backend_tag, voice_id, text)
        path.write_bytes(data)
        self._evict()
        return path

    def clear(self) -> int:
        count = 0
        for f in self._dir.glob("*.wav"):
            f.unlink(missing_ok=True)
            count += 1
        return count

    def purge_voice(self, voice_id: str) -> int:
        tag = self._voice_tag(voice_id)
        count = 0
        for f in self._dir.glob(f"{tag}_*.wav"):
            f.unlink(missing_ok=True)
            count += 1
        return count

    def _evict(self) -> None:
        files = sorted(self._dir.glob("*.wav"), key=lambda f: f.stat().st_mtime)
        for stale in files[: max(0, len(files) - self._max)]:
            stale.unlink(missing_ok=True)


class CosyVoiceSpeaker(Speaker):
    """CosyVoice 官方服务端语音合成（可选后端）。

    - voice_id=None：使用模型预设音色（inference_sft）；
    - voice_id=档案 ID：零样本克隆（inference_zero_shot），档案必须已获授权；
    - 结果经 AudioCache 缓存；speak() 用 winsound 播放，speak_to_file() 落盘。
    合成失败抛 VoiceError（携带 degradable 标记），由调用方/降级层处理。
    """

    def __init__(
        self,
        client: CosyVoiceClient,
        profile_manager: Optional[VoiceProfileManager] = None,
        *,
        voice_id: Optional[str] = None,
        cache: Optional[AudioCache] = None,
    ) -> None:
        self._client = client
        self._profiles = profile_manager
        self._voice_id = voice_id
        self._cache = cache

    def set_voice(self, voice_id: Optional[str]) -> None:
        """切换音色；None 表示模型预设音色。"""
        self._voice_id = voice_id

    @property
    def voice_id(self) -> Optional[str]:
        return self._voice_id

    def synthesize_bytes(self, text: str) -> bytes:
        if not text or not text.strip():
            return b""
        profile = None
        voice_tag = "preset"
        if self._voice_id:
            if self._profiles is None:
                raise VoiceError("未配置音色库，无法使用音色档案合成", degradable=False)
            try:
                profile = self._profiles.resolve_for_synthesis(self._voice_id)
                voice_tag = self._voice_id
            except VoiceProfileError as exc:
                raise VoiceError(str(exc), degradable=False) from exc
        if self._cache is not None:
            cached = self._cache.get("cosyvoice", voice_tag, text)
            if cached is not None:
                return cached.read_bytes()
        try:
            wav = self._client.synthesize(text, profile)
        except CosyVoiceClientError as exc:
            raise VoiceError(str(exc), degradable=exc.kind != "bad_request") from exc
        if self._cache is not None:
            self._cache.put_bytes("cosyvoice", voice_tag, text, wav)
        return wav

    def _play_wav(self, path: Path) -> None:
        try:
            import winsound
        except ImportError as exc:
            raise VoiceError("当前系统没有 winsound，无法播放合成音频") from exc
        winsound.PlaySound(str(path), winsound.SND_FILENAME)

    def speak(self, text: str) -> None:
        wav = self.synthesize_bytes(text)
        if not wav:
            return
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tf.write(wav)
            tmp = Path(tf.name)
        try:
            self._play_wav(tmp)
        finally:
            tmp.unlink(missing_ok=True)

    def speak_to_file(self, text: str, path: Path) -> Path:
        wav = self.synthesize_bytes(text)
        if not wav:
            raise VoiceError("要合成的文本为空")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(wav)
        return path


class FallbackSpeaker(Speaker):
    """主语音后端失败时按配置降级到备用后端；降级必留痕。

    degradable=False 的失败（策略性失败，如音色未授权）不降级，原样上抛。
    last_degradation 供界面/状态接口展示"当前实际使用的后端"。
    """

    def __init__(self, primary: Speaker, fallback: Speaker,
                 primary_label: str = "cosyvoice", fallback_label: str = "sapi") -> None:
        self._primary = primary
        self._fallback = fallback
        self._primary_label = primary_label
        self._fallback_label = fallback_label
        self.last_degradation: Optional[dict] = None

    def _degrade(self, action: str, exc: VoiceError) -> None:
        self.last_degradation = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "action": action,
            "from": self._primary_label,
            "to": self._fallback_label,
            "reason": str(exc),
        }
        _log.warning("语音后端降级：%s 失败，改用 %s（原因：%s）",
                     self._primary_label, self._fallback_label, exc)

    def _run(self, action: str, method: str, args: tuple) -> object:
        try:
            return getattr(self._primary, method)(*args)
        except VoiceError as exc:
            if not exc.degradable:
                raise
            self._degrade(action, exc)
            return getattr(self._fallback, method)(*args)

    def speak(self, text: str) -> None:
        self._run("speak", "speak", (text,))

    def speak_to_file(self, text: str, path: Path) -> Path:
        return self._run("speak_to_file", "speak_to_file", (text, path))  # type: ignore[return-value]
