"""CosyVoice 官方服务端 HTTP 客户端（阶段 12）。

对接官方 runtime/python/fastapi/server.py（不改官方代码）：
  - POST /inference_sft         预设音色合成（tts_text + spk_id）
  - POST /inference_zero_shot   参考音频克隆合成（tts_text + prompt_text + prompt_wav）
  - GET  /docs                  FastAPI 自带页面，作为健康检查探针

服务端返回的是裸 PCM（int16）字节流，无 WAV 头与采样率元数据；
本客户端负责按配置的采样率封装成标准 WAV。

安全与资源约束：
  - 文本长度上限、并发上限、HTTP 超时都在客户端收敛，防止推理占满资源；
  - 授权门禁：带参考音色合成前强制检查 consent_status（见 profiles.py）；
  - 日志只记录 voice_id 与字节数，不记录参考文本与录音路径。
"""
from __future__ import annotations

import logging
import struct
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from .profiles import voice_profile_require_consent

_log = logging.getLogger("butler.voice.cosyvoice")

# CosyVoice3 官方 zero-shot 用法要求 prompt_text 带该前缀（见官方 example.py
# cosyvoice3_example）；换用 CosyVoice2 模型时应置为空字符串
DEFAULT_PROMPT_PREFIX = "You are a helpful assistant.<|endofprompt|>"


class CosyVoiceClientError(Exception):
    """CosyVoice 调用失败。kind 取值：
    timeout（超时）/ unavailable（服务不可达）/ busy（并发已满）/
    bad_request（输入非法）/ server_error（服务端错误）。
    message 面向用户可读。"""

    def __init__(self, message: str, kind: str = "server_error") -> None:
        super().__init__(message)
        self.kind = kind


def pcm_to_wav(pcm: bytes, sample_rate: int, channels: int = 1, bits: int = 16) -> bytes:
    """把裸 int16 PCM 封装为标准 WAV（RIFF）字节流。"""
    if not pcm:
        raise CosyVoiceClientError("服务端返回了空音频", "server_error")
    block_align = channels * bits // 8
    byte_rate = sample_rate * block_align
    fmt = struct.pack(
        "<HHIIHH", 1, channels, sample_rate, byte_rate, block_align, bits
    )
    data_size = len(pcm)
    riff_size = 36 + data_size
    header = b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
    header += b"fmt " + struct.pack("<I", 16) + fmt
    header += b"data" + struct.pack("<I", data_size)
    return header + pcm


class CosyVoiceClient:
    """与官方 CosyVoice FastAPI 服务端通信的薄客户端。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 120.0,
        sample_rate: int = 24000,
        max_text_chars: int = 500,
        max_concurrent: int = 1,
        prompt_prefix: str = DEFAULT_PROMPT_PREFIX,
        default_spk_id: str = "中文女",
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(
            connect=5.0, read=timeout_seconds, write=timeout_seconds, pool=5.0
        )
        self._sample_rate = sample_rate
        self._max_text_chars = max_text_chars
        self._prompt_prefix = prompt_prefix
        self._default_spk_id = default_spk_id
        self._slots = threading.BoundedSemaphore(max_concurrent)
        # trust_env=False：本服务只访问本机回环地址，必须绕过系统代理——
        # 否则 HTTP_PROXY 环境变量会把 127.0.0.1 请求发往代理（实测得到 502）
        self._client = httpx.Client(
            timeout=self._timeout, transport=transport, trust_env=False
        )

    def close(self) -> None:
        self._client.close()

    # ---- 健康检查 ----

    def health_check(self, timeout_seconds: float = 5.0) -> bool:
        """服务可达即健康（官方服务端无专门 health 端点，用自带 /docs 探测）。"""
        try:
            resp = self._client.get(
                f"{self._base_url}/docs", timeout=httpx.Timeout(timeout_seconds)
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    # ---- 声纹提取（阶段 13：复用服务端 campplus 模型，不做任何本地音频算法）----

    def speaker_embedding(self, wav_bytes: bytes, filename: str = "clip.wav") -> list:
        """提取一段音频的声纹向量（192 维 float 列表）。

        服务端 /speaker_embed 由适配层提供（官方 server 无此路由）：把音频
        重采样到 16k 后过 campplus 模型。调用方随后应丢弃原始音频字节。
        """
        if not wav_bytes:
            raise CosyVoiceClientError("要提取声纹的音频为空", "bad_request")
        if not self._slots.acquire(timeout=self._timeout.read):
            raise CosyVoiceClientError("已有语音任务在执行，请稍后再试", "busy")
        try:
            try:
                resp = self._client.post(
                    f"{self._base_url}/speaker_embed",
                    files={"wav": (filename, wav_bytes)},
                )
            except httpx.TimeoutException as exc:
                raise CosyVoiceClientError("声纹提取超时，请重试", "timeout") from exc
            except httpx.HTTPError as exc:
                raise CosyVoiceClientError(
                    f"无法连接 CosyVoice 服务（{self._base_url}）：{exc.__class__.__name__}。"
                    "声纹提取需要服务在线（见 docs/COSYVOICE_SETUP.md）",
                    "unavailable",
                ) from exc
            if resp.status_code != 200:
                detail = (resp.text or "").strip().splitlines()
                snippet = detail[-1][:200] if detail else ""
                kind = "bad_request" if resp.status_code in (400, 422) else "server_error"
                raise CosyVoiceClientError(
                    f"声纹提取失败（HTTP {resp.status_code}）：{snippet}", kind
                )
            try:
                payload = resp.json()
                embedding = list(payload["embedding"])
            except (ValueError, KeyError, TypeError) as exc:
                raise CosyVoiceClientError(
                    "声纹服务返回格式异常", "server_error"
                ) from exc
            return embedding
        finally:
            self._slots.release()

    # ---- 合成 ----

    def synthesize(
        self,
        text: str,
        profile: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        """合成一段文本，返回 WAV 字节。

        profile 为音色档案（profiles.py 的 dict）时走零样本克隆：
        上传其参考录音 + 参考文本；为 None 时走模型预设音色（inference_sft）。
        """
        text = (text or "").strip()
        if not text:
            raise CosyVoiceClientError("要合成的文本不能为空", "bad_request")
        if len(text) > self._max_text_chars:
            raise CosyVoiceClientError(
                f"要合成的文本长度 {len(text)} 超过上限 {self._max_text_chars} 字符",
                "bad_request",
            )

        if not self._slots.acquire(timeout=self._timeout.read):
            raise CosyVoiceClientError(
                "已有语音合成在执行，请稍后再试", "busy"
            )
        try:
            start = time.monotonic()
            if profile is not None:
                wav = self._zero_shot(text, profile)
                route = "zero_shot"
            else:
                wav = self._sft(text)
                route = "sft"
            _log.info(
                "合成完成 route=%s voice=%s audio_bytes=%d 耗时=%.2fs",
                route,
                (profile or {}).get("voice_id", "-"),
                len(wav),
                time.monotonic() - start,
            )
            return wav
        finally:
            self._slots.release()

    def _sft(self, text: str) -> bytes:
        pcm = self._post_pcm(
            "/inference_sft",
            data={"tts_text": text, "spk_id": self._default_spk_id},
            files=None,
        )
        return pcm_to_wav(pcm, self._sample_rate)

    def _zero_shot(self, text: str, profile: Dict[str, Any]) -> bytes:
        voice_profile_require_consent(profile)  # 授权门禁：未授权档案禁止克隆
        audio_path = profile.get("_audio_file")  # 调用方注入的本地路径
        if not audio_path:
            raise CosyVoiceClientError(
                "音色缺少本地参考录音文件，无法克隆合成", "bad_request"
            )
        prompt_text = f"{self._prompt_prefix}{profile.get('reference_text', '')}"
        with open(audio_path, "rb") as fh:
            pcm = self._post_pcm(
                "/inference_zero_shot",
                data={"tts_text": text, "prompt_text": prompt_text},
                files={"prompt_wav": (Path(audio_path).name, fh)},
            )
        return pcm_to_wav(pcm, self._sample_rate)

    def _post_pcm(self, route: str, data: Dict[str, Any], files: Any) -> bytes:
        """POST 表单请求，返回服务端流式响应聚合后的 PCM 字节。"""
        try:
            resp = self._client.post(f"{self._base_url}{route}", data=data, files=files)
        except httpx.TimeoutException as exc:
            raise CosyVoiceClientError(
                f"CosyVoice 服务合成超时（{route}），可稍后重试或降低文本长度", "timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise CosyVoiceClientError(
                f"无法连接 CosyVoice 服务（{self._base_url}）：{exc.__class__.__name__}。"
                "请确认服务已启动（见 docs/COSYVOICE_SETUP.md）",
                "unavailable",
            ) from exc
        if resp.status_code != 200:
            # 服务端错误体可能是 traceback 片段，只保留末尾一行且截断，避免刷日志
            detail = (resp.text or "").strip().splitlines()
            snippet = detail[-1][:200] if detail else ""
            if resp.status_code == 422:
                raise CosyVoiceClientError(
                    f"CosyVoice 服务拒绝了请求参数（422）：{snippet}", "bad_request"
                )
            raise CosyVoiceClientError(
                f"CosyVoice 服务返回错误 {resp.status_code}（{route}）：{snippet}",
                "server_error",
            )
        return resp.content
