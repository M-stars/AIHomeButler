"""语音会话入口：python -m butler.voice

语音输出默认启用（Windows SAPI5 离线合成）；配置 TTS_BACKEND=cosyvoice 时
改用 CosyVoice 官方服务端合成（含音色库克隆音色），失败按配置降级回 SAPI。
语音输入两种方式：
  1. 默认：键盘输入文字（纯语音输出模式，无需任何音频采集依赖）；
  2. --mic：麦克风采集 + faster-whisper 识别（需 pip install faster-whisper sounddevice）。

家庭成员声纹（阶段 13，SPEAKER_RECOGNITION=1 启用）：--mic 模式下每轮录音先做
声纹识别，识别出的成员获得：称呼个性化、按人隔离的记忆、其绑定的回答音色；
识别不出按"陌生人"回落通用模式。键盘输入可用 /who 手动指定成员（调试）。

唤醒词（阶段 14，--wake 或 WAKEWORD_ENABLED=1 启用，需 --mic）：待机时本地
离线检测唤醒词（sherpa-onnx KWS，默认「小管家」），命中后自动开始聆听指令，
处理完回落待机。依赖/模型缺失自动退回免唤醒模式，见 docs/VOICE.md。

与文本 CLI 完全共用同一套 Agent/工具/确认/安全机制，高风险操作不会因语音绕过确认。
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from ..cli import _build_agent, _build_registry, _deliver_notifications  # 复用装配逻辑
from ..config import ConfigError, load_config
from ..logging_setup import setup_logging
from ..memory.store import MemoryStore
from .assembly import SpeakerIdentity, build_speaker_identity, build_tts_stack
from .profiles import VoiceProfileError
from .tts import SilentSpeaker, Speaker, Transcriber, VoiceError, WhisperTranscriber
from .wakeword import build_wake_word_detector, play_wake_chime, wait_for_wake


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


def _recognize_member(stack, identity: SpeakerIdentity, wav_path: Path) -> Optional[dict]:
    """录音 → 声纹识别 → 打印结果。失败/陌生人都返回 None。"""
    from .assembly import identify_from_wav

    member, similarity = identify_from_wav(identity, stack.client, wav_path)
    if member is not None:
        print(f"[声纹] 已识别：{member['display_name']}（相似度 {similarity:.2f}）")
    else:
        print("[声纹] 未识别出家庭成员，按通用模式回答")
    return member


def _apply_member_voice(stack, default_voice_id: Optional[str], member: Optional[dict]) -> None:
    """按成员绑定音色回答：成员绑定了音色库档案就用它（档案已删则回落默认）。"""
    if stack.backend != "cosyvoice":
        return
    voice_id = default_voice_id
    if member and member.get("voice_id"):
        try:
            stack.profiles.get(member["voice_id"])
            voice_id = member["voice_id"]  # 档案仍存在才生效
        except VoiceProfileError:
            print(f"[提示] 成员「{member['display_name']}」绑定的音色已删除，使用默认音色")
    stack.cosyvoice_speaker.set_voice(voice_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Home Butler 语音模式")
    parser.add_argument("--no-speak", action="store_true", help="禁用语音输出（调试用）")
    parser.add_argument("--mic", action="store_true", help="启用麦克风语音输入（需可选依赖）")
    parser.add_argument(
        "--wake", action="store_true",
        help="待机监听唤醒词（需 --mic；.env 设 WAKEWORD_ENABLED=1 可默认开启）",
    )
    parser.add_argument("--wav", type=str, default=None, help="处理单个 wav 文件后退出")
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"[配置错误] {exc}")
        return 2
    setup_logging(config.log_dir, config.log_level)

    # 语音栈总是装配（不联网、不发声）：--no-speak 只静音输出，
    # 声纹识别依赖其中的服务客户端与音色库
    speaker: Speaker
    using_cosyvoice = False
    stack = None
    identity: Optional[SpeakerIdentity] = None
    try:
        stack = build_tts_stack(config)
        identity = build_speaker_identity(config, stack.client)
    except (VoiceError, VoiceProfileError) as exc:
        print(f"[语音输出不可用] {exc}，切换为静音模式")
        speaker = SilentSpeaker()
    else:
        speaker = SilentSpeaker() if args.no_speak else stack.speaker
        using_cosyvoice = stack.backend == "cosyvoice"
        if not args.no_speak:
            if stack.backend == "cosyvoice":
                default_voice = stack.profiles.default()
                voice_name = default_voice["display_name"] if default_voice else "模型预设音色"
                print(f"语音后端: CosyVoice（{config.cosyvoice_url}）默认音色: {voice_name}")
                if stack.fallback_speaker is not None:
                    print("CosyVoice 不可用时自动降级到 Windows SAPI（降级会打印提示）")
            else:
                print("语音后端: Windows SAPI5（如需 CosyVoice 克隆音色，在 .env 设置 TTS_BACKEND=cosyvoice）")
    default_voice_id = stack.cosyvoice_speaker.voice_id if stack else None

    transcriber = None
    try:
        transcriber = _build_transcriber(args.mic)
    except VoiceError as exc:
        print(f"[语音输入不可用] {exc}，回退为键盘输入")

    # 唤醒词（阶段 14）：只决定"何时开始聆听"，不参与任何权限判断。
    # 依赖/模型缺失时退回免唤醒模式，绝不因此拒绝启动；键盘输入模式与唤醒词
    # 互斥——显式给 --wake 说明命令写错要拦下，仅配置默认开启则提示后照常启动。
    wake_detector = None
    wake_requested = args.wake or config.wakeword_enabled
    if wake_requested and not args.mic:
        if args.wake:
            print("[配置错误] 唤醒词需要与 --mic 一起使用（待机监听依赖麦克风）")
            return 2
        print("[提示] WAKEWORD_ENABLED=1 但本次是键盘输入模式，忽略唤醒词")
    elif wake_requested:
        try:
            # 显式传 enabled=True：--wake 旗标要在 .env 未配置时也生效
            wake_detector = build_wake_word_detector(config, enabled=True)
        except VoiceError as exc:
            print(f"[唤醒词不可用] {exc}，本次退回免唤醒模式")
            wake_detector = None

    registry = _build_registry(config)
    memory_store = MemoryStore(config.data_dir / "memory.db")
    agent, rule_engine, _executor, _routine_runner = _build_agent(
        config, registry, memory_store,
        member_context=identity.context if identity else None,
    )

    print("=" * 56)
    print("AI Home Butler 语音模式（共用文本模式的安全与确认机制）")
    if args.no_speak:
        print("语音输出: 关闭")
    elif using_cosyvoice:
        print("语音输出: CosyVoice（默认音色见上）")
    else:
        print("语音输出: Windows SAPI5")
    if identity and identity.enabled:
        members = identity.registry.list()
        print(f"声纹识别: 开启（{len(members)} 位成员，阈值 {identity.threshold:.2f}）"
              + ("" if transcriber is not None else "（键盘输入模式不识别，可用 /who 手动指定）"))
    else:
        print("声纹识别: 关闭（.env 设 SPEAKER_RECOGNITION=1 并启动 CosyVoice 服务后启用）")
    if wake_detector is not None:
        print(f"唤醒词: 开启（待机时说「{config.wake_word}」唤醒）")
    elif args.mic:
        print("唤醒词: 关闭（--wake 或 .env 设 WAKEWORD_ENABLED=1；模型见 docs/VOICE.md）")
    print("输入文字后回车；控制类操作仍会在终端请求 y/N 确认。/exit 退出")
    print("键盘模式命令：/who 列出成员 ｜ /who 名字 切换身份 ｜ /who off 恢复陌生模式")
    print("=" * 56)

    if args.wav:
        if transcriber is None:
            print("[错误] --wav 需要先 --mic 启用识别")
            return 2
        text = transcriber.transcribe_wav(Path(args.wav))
        print(f"识别结果: {text}")
        member = None
        if identity is not None:
            member = _recognize_member(stack, identity, Path(args.wav))
            _apply_member_voice(stack, default_voice_id, member)
        if identity is not None:
            identity.context.set(member)
        try:
            result = agent.process(text)
        finally:
            if identity is not None:
                identity.context.clear()
        print(f"管家: {result.reply}")
        _speak_safely(speaker, result.reply)
        return 0

    while True:
        try:
            if transcriber is not None:
                from .tts import MicrophoneRecorder

                if wake_detector is not None:
                    print(f"\n（待机中，说「{config.wake_word}」唤醒；Ctrl+C 退出）")
                    try:
                        wake_label = wait_for_wake(wake_detector)
                    except Exception as exc:  # 麦克风被占用/拔出等：重试但不刷屏
                        print(f"[唤醒监听失败] {exc}，1 秒后重试")
                        time.sleep(1.0)
                        continue
                    if wake_label is None:  # 音频块迭代器耗尽（正常麦克风流不会发生）
                        print("[唤醒监听] 音频流已结束，退出")
                        return 0
                    play_wake_chime()
                    print(f"[已唤醒] {wake_label}，请说指令")
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
                member = None
                if identity is not None:
                    member = _recognize_member(stack, identity, wav_path)
                    _apply_member_voice(stack, default_voice_id, member)
                if identity is not None:
                    identity.context.set(member)
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
        if line.startswith("/who"):
            if identity is None:
                print("[提示] 声纹模块未装配（语音输出初始化失败或未开启）")
                continue
            arg = line[4:].strip()
            if not arg:
                members = identity.registry.list()
                if not members:
                    print("  （尚无成员。用 Web 界面「家庭成员声纹」卡片注册）")
                for m in members:
                    print(f"  {m['display_name']}（识别 {m['hits']['count']} 次）")
            elif arg.lower() == "off":
                identity.context.clear()
                _apply_member_voice(stack, default_voice_id, None)
                print("已恢复通用模式（无成员身份）")
            else:
                found = identity.registry.get_by_name(arg)
                if found is None:
                    print(f"没有叫「{arg}」的成员（/who 查看名单）")
                else:
                    identity.context.set(found)
                    _apply_member_voice(stack, default_voice_id, found)
                    print(f"当前身份已切换为：{found['display_name']}")
            continue
        try:
            result = agent.process(line)
        finally:
            # 麦克风模式按轮设置/清理；键盘 /who 设置的身份跨轮保留：
            # 若本轮没有新的成员信息（键盘模式 context 未被清），不动它。
            # 这里只在"本轮曾通过声纹设置过 context"时清理——用录音路径标记判断。
            if transcriber is not None and identity is not None:
                identity.context.clear()
        for event in result.tool_events:
            print(f"  [工具] {event.tool}: {event.status}")
        print(f"管家: {result.reply}")
        _speak_safely(speaker, result.reply)
        _deliver_notifications(rule_engine)


if __name__ == "__main__":
    sys.exit(main())
