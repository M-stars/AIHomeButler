"""语音后端装配（阶段 12）：按 TTS_BACKEND 配置把 SAPI / CosyVoice 组装成 Speaker。

主项目其他入口（voice CLI / Web）都通过 build_tts_stack 拿到同一套装配，
避免各入口自行拼装造成行为不一致：

  TTS_BACKEND=sapi       → SapiSpeaker（默认，历史行为完全不变）
  TTS_BACKEND=cosyvoice  → FallbackSpeaker(CosyVoiceSpeaker, SAPI)
                           （COSYVOICE_FALLBACK_SAPI=0 时失败直接报错不降级）

装配本身不会连接 CosyVoice 服务（HTTP 客户端惰性连接），因此后端选了
cosyvoice 而服务未启动，不影响系统启动，只影响实际合成时（按配置降级）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..config import ButlerConfig
from .cosyvoice_client import DEFAULT_PROMPT_PREFIX, CosyVoiceClient, CosyVoiceClientError
from .profiles import VoiceProfileManager
from .speakers import MemberContext, SpeakerRegistry
from .tts import (
    AudioCache,
    CosyVoiceSpeaker,
    FallbackSpeaker,
    SapiSpeaker,
    SilentSpeaker,
    Speaker,
    VoiceError,
)

TTS_TAG = "cosyvoice"  # 缓存键里的后端标识
_assembly_log = logging.getLogger("butler.voice.assembly")


@dataclass
class TTSStack:
    """一次装配的全部语音输出组件。speaker 是按配置可直接使用的最终实现。"""

    speaker: Speaker
    backend: str
    profiles: VoiceProfileManager
    client: CosyVoiceClient
    cosyvoice_speaker: CosyVoiceSpeaker
    cache: AudioCache
    fallback_speaker: Optional[Speaker]

    def status(self) -> dict:
        """语音子系统状态（供 Web 界面/排查用；含降级留痕）。"""
        degradation = getattr(self.speaker, "last_degradation", None)
        default_voice = self.profiles.default()
        return {
            "backend": self.backend,
            "cosyvoice_healthy": self.client.health_check(timeout_seconds=2.0),
            "fallback_to_sapi": isinstance(self.speaker, FallbackSpeaker),
            "default_voice": default_voice,
            "voice_count": len(self.profiles.list()),
            "last_degradation": degradation,
            "ai_disclaimer": "语音由 AI 合成生成，非真人录音",
        }


def build_tts_stack(config: ButlerConfig) -> TTSStack:
    profiles = VoiceProfileManager(config.data_dir / "voices")
    cache = AudioCache(config.data_dir / "voices" / "cache")
    client = CosyVoiceClient(
        config.cosyvoice_url,
        timeout_seconds=config.cosyvoice_timeout_seconds,
        sample_rate=config.cosyvoice_sample_rate,
        max_text_chars=config.cosyvoice_max_text_chars,
        # CosyVoice3 官方 zero-shot prompt 前缀；改用 CosyVoice2 模型时在 .env 置空
        prompt_prefix=os.environ.get("COSYVOICE_PROMPT_PREFIX", DEFAULT_PROMPT_PREFIX),
    )
    default_profile = profiles.default()
    cosyvoice_speaker = CosyVoiceSpeaker(
        client,
        profiles,
        voice_id=(default_profile or {}).get("voice_id"),
        cache=cache,
    )

    sapi: Optional[Speaker] = None
    try:
        sapi = SapiSpeaker()
    except Exception as exc:  # 无声卡/SAPI 异常等，播报层面自动降级为静音
        import logging

        logging.getLogger("butler.voice").warning("SAPI 不可用：%s", exc)

    if config.tts_backend == "cosyvoice":
        if config.cosyvoice_fallback_sapi:
            fallback = sapi if sapi is not None else SilentSpeaker()
            speaker: Speaker = FallbackSpeaker(cosyvoice_speaker, fallback)
        else:
            speaker = cosyvoice_speaker  # 显式禁用降级：失败直接报错
    else:
        speaker = sapi if sapi is not None else SilentSpeaker()
    if config.tts_backend != "cosyvoice" or not config.cosyvoice_fallback_sapi:
        fallback = None

    return TTSStack(
        speaker=speaker,
        backend=config.tts_backend,
        profiles=profiles,
        client=client,
        cosyvoice_speaker=cosyvoice_speaker,
        cache=cache,
        fallback_speaker=fallback,
    )


@dataclass
class SpeakerIdentity:
    """声纹识别子系统（阶段 13）：注册表 + 当前说话人上下文。"""

    registry: SpeakerRegistry
    context: MemberContext
    enabled: bool
    threshold: float

    def status(self) -> dict:
        members = self.registry.list()
        return {
            "speaker_recognition": self.enabled,
            "threshold": self.threshold,
            "member_count": len(members),
            "members": members,
            "ai_disclaimer": "声纹识别仅用于个性化体验，不作为身份验证；"
                             "声纹向量只存本机，识别失败自动按陌生人处理",
        }


def build_speaker_identity(config: ButlerConfig, client: CosyVoiceClient) -> SpeakerIdentity:
    """装配声纹子系统：默认关闭（SPEAKER_RECOGNITION=0）时零行为变化。"""
    return SpeakerIdentity(
        registry=SpeakerRegistry(config.data_dir / "speakers"),
        context=MemberContext(),
        enabled=bool(config.speaker_recognition),
        threshold=config.speaker_threshold,
    )


def identify_from_wav(
    identity: SpeakerIdentity,
    client: CosyVoiceClient,
    wav_path,
) -> tuple:
    """对一段录音做声纹识别，返回 (成员|None, 相似度|None)。

    任何失败（服务离线/格式问题/未开启）都收敛为 (None, None)，绝不打断
    对话主流程；失败原因写入日志。命中时写入该成员的识别记录。
    """
    if not identity.enabled:
        return None, None
    try:
        embedding = client.speaker_embedding(Path(wav_path).read_bytes(),
                                             filename=Path(wav_path).name)
        member, similarity = identity.registry.identify(embedding, identity.threshold)
        if member is not None:
            identity.registry.record_hit(member["member_id"], similarity)
        return member, similarity
    except CosyVoiceClientError as exc:
        _assembly_log.warning("声纹识别失败（不影响对话）：%s", exc)
        return None, None
    except Exception as exc:  # 识别是锦上添花，任何异常都不打断对话
        _assembly_log.warning("声纹识别异常（不影响对话）：%s", exc)
        return None, None
