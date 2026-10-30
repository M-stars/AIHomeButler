"""唤醒词检测（阶段 14）：sherpa-onnx KeywordSpotter 离线桥接。

策略与语音链路一致（docs/VOICE.md §1）：不自研唤醒词引擎，复用 k2-fsa
sherpa-onnx 的 KWS 模型（wenetspeech 10000 小时数据训练，3.3M 参数 int8 量化，
Apache-2.0，本机实测约 50 倍实时，树莓派 4 可实时运行）。中文唤醒词以
"声母+带调韵母"形式写在模型目录 keywords.txt（如 `x iǎo g uǎn j iā @小管家`），
改唤醒词 = 改该文件，无需训练模型。

设计约定（与 tts.py 的可选依赖降级一致）：
  - sherpa_onnx 为延迟导入可选依赖：未安装/模型缺失抛 VoiceError（含修复指引），
    由入口捕获后退回免唤醒模式，绝不影响其余功能；
  - 检测器只吃 16kHz 单声道 float32 音频块、命中时吐关键词标签，音频块迭代器
    可注入——测试用假检测器即可覆盖全部循环逻辑，无需麦克风与模型；
  - 唤醒词只决定"何时开始聆听指令"，不参与任何权限判断——确认流程与声纹/
    文本模式完全相同，安全边界不因语音入口松动（阶段 8 验收条件延续）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, Optional

from .tts import VoiceError

_log = logging.getLogger("butler.voice.wakeword")

_UTF8_BOM = b"\xef\xbb\xbf"


def ensure_no_bom(path: Path) -> bool:
    """去掉文本文件开头的 UTF-8 BOM（Windows 记事本常见保存格式）。

    BOM 会粘在 keywords.txt 首个 token 上，导致第一个唤醒词永远无法命中；
    字节级剥除（不 decode/recode），换行风格等其余字节原样保留。
    返回是否发生了改写。
    """
    raw = path.read_bytes()
    if not raw.startswith(_UTF8_BOM):
        return False
    path.write_bytes(raw[len(_UTF8_BOM):])
    return True


class SherpaKeywordSpotterDetector:
    """sherpa-onnx KeywordSpotter 桥接：process_block 喂音频块，命中返回标签。"""

    sample_rate = 16000

    def __init__(self, model_dir, keywords_file=None, threshold: float = 0.25,
                 num_threads: int = 1) -> None:
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise VoiceError(
                "sherpa-onnx 未安装：pip install sherpa-onnx（唤醒词检测引擎，约 15 MB）"
            ) from exc

        model_dir = Path(model_dir)
        if not (model_dir / "tokens.txt").exists():
            raise VoiceError(
                f"未找到唤醒词模型（{model_dir}）。"
                "运行 python scripts/wakeword_download_model.py 下载（约 33 MB，不入 Git）"
            )
        keywords_file = Path(keywords_file) if keywords_file else model_dir / "keywords.txt"
        if not keywords_file.exists():
            raise VoiceError(
                f"缺少唤醒词表 {keywords_file}。每行格式（声母+带调韵母，空格分隔）："
                "x iǎo g uǎn j iā @小管家"
            )
        if ensure_no_bom(keywords_file):
            _log.info("唤醒词表 %s 带 UTF-8 BOM，已自动去除", keywords_file)
        try:
            self._kws = sherpa_onnx.KeywordSpotter(
                tokens=str(model_dir / "tokens.txt"),
                encoder=str(self._pick_model_file(model_dir, "encoder")),
                decoder=str(self._pick_model_file(model_dir, "decoder")),
                joiner=str(self._pick_model_file(model_dir, "joiner")),
                keywords_file=str(keywords_file),
                num_threads=num_threads,
                sample_rate=self.sample_rate,
                feature_dim=80,
                keywords_threshold=threshold,
            )
        except Exception as exc:
            raise VoiceError(f"唤醒词模型加载失败：{exc}") from exc
        self._stream = None

    @staticmethod
    def _pick_model_file(model_dir: Path, role: str) -> Path:
        """优先 int8 量化文件（体积小速度快，树莓派友好），退回普通 onnx。"""
        candidates = sorted(model_dir.glob(f"{role}*.int8.onnx")) \
            or sorted(model_dir.glob(f"{role}*.onnx"))
        if not candidates:
            raise VoiceError(
                f"唤醒词模型不完整：缺 {role}*.onnx。"
                "运行 python scripts/wakeword_download_model.py 重新下载"
            )
        return candidates[0]

    def process_block(self, samples) -> Optional[str]:
        """喂入一块 16kHz 单声道 float32 音频；命中返回关键词标签，否则 None。"""
        if self._stream is None:
            self._stream = self._kws.create_stream()
        self._stream.accept_waveform(self.sample_rate, samples)
        while self._kws.is_ready(self._stream):
            self._kws.decode_stream(self._stream)
        result = self._kws.get_result(self._stream)
        if result:
            self._kws.reset_stream(self._stream)
            return result
        return None

    def reset(self) -> None:
        """丢弃当前检测上下文（上一轮残留音频），从零开始监听。"""
        self._stream = None


def build_wake_word_detector(config, enabled: Optional[bool] = None
                             ) -> Optional[SherpaKeywordSpotterDetector]:
    """装配唤醒词检测器；未开启返回 None（默认，行为不变）。

    enabled=None 时以 config.wakeword_enabled 为准；显式传 True/False 可覆盖
    （CLI --wake 旗标需要在 .env 未开启时也能生效）。依赖缺失/模型缺失抛
    VoiceError（含修复指引），由入口捕获后退回免唤醒模式。
    """
    if enabled is None:
        enabled = config.wakeword_enabled
    if not enabled:
        return None
    return SherpaKeywordSpotterDetector(
        model_dir=config.wake_word_model_dir,
        threshold=config.wake_word_threshold,
    )


def wait_for_wake(detector, blocks=None) -> Optional[str]:
    """阻塞等待唤醒词命中，返回命中标签。

    blocks 为可注入的音频块迭代器（默认麦克风实时流）；迭代耗尽且未命中时
    返回 None（正常麦克风流是无限的，仅供测试）。Ctrl+C 原样上抛，由语音
    主循环统一按退出处理。
    """
    if blocks is None:
        blocks = _mic_blocks(detector.sample_rate)
    for block in blocks:
        hit = detector.process_block(block)
        if hit:
            return hit
    return None


def _mic_blocks(sample_rate: int = 16000, block_seconds: float = 0.1) -> Iterator:
    """默认音频源：麦克风实时音频块生成器（sounddevice 可选依赖）。"""
    import queue as _queue

    import sounddevice as sd

    frames: _queue.Queue = _queue.Queue()

    def callback(indata, _frames, _time, _status):
        frames.put(indata.copy()[:, 0])

    with sd.InputStream(samplerate=sample_rate, channels=1, dtype="float32",
                        blocksize=int(sample_rate * block_seconds), callback=callback):
        while True:
            yield frames.get()


def play_wake_chime() -> None:
    """唤醒提示音（短促上行两音）：给用户"已听到你"的即时反馈。

    无声卡/缺依赖/播放失败一律静默跳过——提示音是锦上添花，绝不影响主流程。
    """
    try:
        import numpy as np
        import sounddevice as sd

        sample_rate = 16000
        segments = []
        for freq, gain in ((880.0, 0.22), (1318.5, 0.22)):
            t = np.arange(int(sample_rate * 0.12), dtype=np.float32) / sample_rate
            segments.append(gain * np.sin(2 * np.pi * freq * t)
                            * np.linspace(1.0, 0.6, t.size))
        sd.play(np.concatenate(segments).astype(np.float32), sample_rate)
        sd.wait()
    except Exception:
        pass
