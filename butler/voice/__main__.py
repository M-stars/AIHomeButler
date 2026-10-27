"""语音会话入口：python -m butler.voice

语音输出默认启用（Windows SAPI5 离线合成）；配置 TTS_BACKEND=cosyvoice 时
改用 CosyVoice 官方服务端合成（含音色库克隆音色），失败按配置降级回 SAPI。
语音输入两种方式：
  1. 默认：键盘输入文字（纯语音输出模式，无需任何音频采集依赖）；
  2. --mic：麦克风采集 + faster-whisper 识别（需 pip install faster-whisper sounddevice）。

与文本 CLI 完全共用同一套 Agent/工具/确认/安全机制，高风险操作不会因语音绕过确认。
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from ..cli import _build_agent, _build_registry, _deliver_notifications  # 复用装配逻辑
from ..config import ConfigError, load_config
from ..logging_setup import setup_logging
from ..memory.store import MemoryStore
from .assembly import build_tts_stack
from .tts import SilentSpeaker, Speaker, Transcriber, VoiceError, WhisperTranscriber


def _build_transcriber(enabled: bool) -> Transcriber:
    if not enabled:
        return None
    return WhisperTranscriber()


def _speak_safely(speaker: Speaker, text: str) -> None:
    """播报失败绝不中断对话主流程：只打印错误并继续。"""
    try:
        speaker.speak(text)
    except VoiceError as exc:
        print(f"[语音输出失败] {exc}")
    except Exception as exc:  # 播放器底层未知异常同样不冒泡
        print(f"[语音输出失败] 未知错误：{exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Home Butler 语音模式")
    parser.add_argument("--no-speak", action="store_true", help="禁用语音输出（调试用）")
    parser.add_argument("--mic", action="store_true", help="启用麦克风语音输入（需可选依赖）")
    parser.add_argument("--wav", type=str, default=None, help="处理单个 wav 文件后退出")
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"[配置错误] {exc}")
        return 2
    setup_logging(config.log_dir, config.log_level)

    speaker: Speaker
    using_cosyvoice = False
    if args.no_speak:
        speaker = SilentSpeaker()
    else:
        try:
            from .profiles import VoiceProfileError

            stack = build_tts_stack(config)
            speaker = stack.speaker
            using_cosyvoice = stack.backend == "cosyvoice"
        except (VoiceError, VoiceProfileError) as exc:
            print(f"[语音输出不可用] {exc}，切换为静音模式")
            speaker = SilentSpeaker()
        else:
            if stack.backend == "cosyvoice":
                default_voice = stack.profiles.default()
                voice_name = default_voice["display_name"] if default_voice else "模型预设音色"
                print(f"语音后端: CosyVoice（{config.cosyvoice_url}）默认音色: {voice_name}")
                if stack.fallback_speaker is not None:
                    print("CosyVoice 不可用时自动降级到 Windows SAPI（降级会打印提示）")
            else:
                print("语音后端: Windows SAPI5（如需 CosyVoice 克隆音色，在 .env 设置 TTS_BACKEND=cosyvoice）")

    transcriber = None
    try:
        transcriber = _build_transcriber(args.mic)
    except VoiceError as exc:
        print(f"[语音输入不可用] {exc}，回退为键盘输入")

    registry = _build_registry(config)
    memory_store = MemoryStore(config.data_dir / "memory.db")
    agent, rule_engine, _executor = _build_agent(config, registry, memory_store)

    print("=" * 56)
    print("AI Home Butler 语音模式（共用文本模式的安全与确认机制）")
    if args.no_speak:
        print("语音输出: 关闭")
    elif using_cosyvoice:
        print("语音输出: CosyVoice（默认音色见上）")
    else:
        print("语音输出: Windows SAPI5")
    print("输入文字后回车；控制类操作仍会在终端请求 y/N 确认。/exit 退出")
    print("=" * 56)

    if args.wav:
        if transcriber is None:
            print("[错误] --wav 需要先 --mic 启用识别")
            return 2
        text = transcriber.transcribe_wav(Path(args.wav))
        print(f"识别结果: {text}")
        result = agent.process(text)
        print(f"管家: {result.reply}")
        _speak_safely(speaker, result.reply)
        return 0

    while True:
        try:
            if transcriber is not None:
                from .tts import MicrophoneRecorder

                recorder = MicrophoneRecorder()
                wav_path = Path(tempfile.gettempdir()) / "butler_utterance.wav"
                print("\n（请说话，停止说话 1 秒后自动识别）")
                try:
                    recorder.record_to_wav(wav_path)
                except VoiceError as exc:
                    print(f"[录音失败] {exc}")
                    continue
                line = transcriber.transcribe_wav(wav_path)
                print(f"你（识别）: {line}")
                if not line.strip():
                    continue
            else:
                line = input("\n你: ").strip()
                if not line:
                    continue
        except (EOFError, KeyboardInterrupt):
            print("\n管家: 再见！")
            return 0
        if line.strip().lower() in ("/exit", "/quit", "退出"):
            _speak_safely(speaker, "好的，再见")
            return 0
        result = agent.process(line)
        for event in result.tool_events:
            print(f"  [工具] {event.tool}: {event.status}")
        print(f"管家: {result.reply}")
        _speak_safely(speaker, result.reply)
        _deliver_notifications(rule_engine)


if __name__ == "__main__":
    sys.exit(main())
