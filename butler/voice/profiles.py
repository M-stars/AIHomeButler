"""音色档案库（阶段 12）：参考音频音色库的存储、校验与授权门禁。

职责边界：
  - 只管理"音色档案"（元数据 + 本地参考录音文件），不做任何合成；
    合成由 TTS 后端（见 tts.py / cosyvoice_client.py）完成；
  - 参考录音只保存在本数据目录（voices_dir/audio/），绝不入 Git、不上传第三方；
  - 未经说话人明确授权（consent_status != granted）的档案，禁止用于克隆合成
    ——门禁在 voice_profile_require_consent()，由合成调用方强制检查。

日志约定：只记录 voice_id 与结果，不记录参考文本内容与录音绝对路径。
"""
from __future__ import annotations

import json
import logging
import os
import re
import struct
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_log = logging.getLogger("butler.voice.profiles")

# 授权状态：pending=未获明确授权（档案仅保存，不可用于合成）；granted=说话人已明确授权
CONSENT_PENDING = "pending"
CONSENT_GRANTED = "granted"

# 允许上传的音频扩展名与魔数（真实校验文件头，不信任扩展名）
_AUDIO_MAGIC: Dict[str, bytes] = {
    ".wav": b"RIFF",
    ".mp3": b"",  # mp3 可能是 ID3 头或帧同步字节，单独判断
    ".flac": b"fLaC",
    ".ogg": b"OggS",
    ".m4a": b"",  # m4a 容器为 mp4，头为 ....ftyp，单独判断
}
MAX_AUDIO_BYTES = 10 * 1024 * 1024          # 参考录音上限 10 MB
MAX_WAV_SECONDS = 60.0                      # 参考录音时长上限（克隆提示音建议 5~15 秒）
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class VoiceProfileError(Exception):
    """音色库操作失败，message 面向用户可读。"""


def voice_profile_require_consent(profile: Dict[str, Any]) -> None:
    """克隆合成前的授权门禁：未获授权的档案禁止使用参考音频合成。"""
    if profile.get("consent_status") != CONSENT_GRANTED:
        raise VoiceProfileError(
            f"音色「{profile.get('display_name', profile.get('voice_id'))}」"
            "尚未获得说话人授权（consent_status != granted），"
            "不能使用其参考音频克隆合成。请先在音色库中确认授权。"
        )


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _check_id(voice_id: str) -> str:
    if not _ID_PATTERN.match(voice_id or ""):
        raise VoiceProfileError(f"音色 ID 非法：{voice_id!r}（只允许字母/数字/下划线/短横线）")
    return voice_id


def _sniff_format(data: bytes, ext: str) -> str:
    """按文件头校验真实格式，返回规范化扩展名；不符时抛错。

    仅做结构级校验（魔数/WAV 头解析）；解码能力由 CosyVoice 服务端兜底——
    结构合法但内容损坏的文件会在服务端解码失败并返回明确错误。
    """
    ext = ("." + (ext or "").lstrip(".").lower()) if ext else ""
    if ext not in _AUDIO_MAGIC:
        raise VoiceProfileError(f"不支持的音频格式：{ext}（仅支持 wav/mp3/flac/ogg/m4a）")
    magic = _AUDIO_MAGIC[ext]
    if magic and not data.startswith(magic):
        raise VoiceProfileError(f"文件内容不是有效的 {ext} 音频（文件头校验失败）")
    if ext == ".mp3":
        # ID3v2 头或 MPEG 帧同步（0xFF Ex/Fx）
        if not (data.startswith(b"ID3") or (len(data) > 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)):
            raise VoiceProfileError("文件内容不是有效的 mp3 音频（文件头校验失败）")
    if ext == ".m4a":
        # mp4 容器：偏移 4~8 为 "ftyp"
        if len(data) < 12 or data[4:8] != b"ftyp":
            raise VoiceProfileError("文件内容不是有效的 m4a 音频（文件头校验失败）")
    if ext == ".wav":
        _wav_duration(data)  # 解析失败或超时长会抛错
    return ext.lstrip(".")  # 统一返回不带点的扩展名，供文件命名使用


def _wav_duration(data: bytes) -> float:
    """解析 RIFF/WAV 头计算时长（秒）；结构非法或超长时抛 VoiceProfileError。

    纯标准库实现：只找 fmt 块取平均字节率、data 块取长度，不解码音频。
    """
    try:
        if len(data) < 44 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise ValueError("not RIFF/WAVE")
        pos = 12
        byte_rate = 0
        data_size = None
        while pos + 8 <= len(data):
            cid = data[pos:pos + 4]
            (csize,) = struct.unpack("<I", data[pos + 4:pos + 8])
            body = data[pos + 8:pos + 8 + csize]
            if cid == b"fmt " and len(body) >= 16:
                byte_rate = struct.unpack("<I", body[8:12])[0]
            elif cid == b"data":
                data_size = csize
                break
            pos += 8 + csize + (csize % 2)  # RIFF 块按字对齐
        if not byte_rate or data_size is None:
            raise ValueError("missing fmt/data chunk")
        seconds = data_size / byte_rate
    except VoiceProfileError:
        raise
    except Exception as exc:
        raise VoiceProfileError(f"无法解析 wav 文件头：{exc}") from exc
    if seconds <= 0:
        raise VoiceProfileError("wav 音频时长为 0，请检查录音")
    if seconds > MAX_WAV_SECONDS:
        raise VoiceProfileError(
            f"wav 音频时长 {seconds:.0f} 秒超过上限 {MAX_WAV_SECONDS:.0f} 秒"
            "（克隆参考音建议 5~15 秒）"
        )
    return seconds


class VoiceProfileManager:
    """音色档案库：JSON 索引 + audio/ 目录下的参考录音文件。"""

    def __init__(self, voices_dir: Path, max_audio_bytes: int = MAX_AUDIO_BYTES) -> None:
        self._dir = Path(voices_dir)
        self._audio_dir = self._dir / "audio"
        self._index_path = self._dir / "profiles.json"
        self._max_audio_bytes = max_audio_bytes
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._profiles: List[Dict[str, Any]] = self._load()

    # ---- 查询 ----

    def list(self) -> List[Dict[str, Any]]:
        return [dict(p) for p in self._profiles]

    def get(self, voice_id: str) -> Dict[str, Any]:
        _check_id(voice_id)
        for p in self._profiles:
            if p["voice_id"] == voice_id:
                return dict(p)
        raise VoiceProfileError(f"音色不存在：{voice_id}")

    def resolve_for_synthesis(self, voice_id: str) -> Dict[str, Any]:
        """取档案并注入本地录音路径（_audio_file），供合成调用方直接使用。

        与 audio_path() 一样只返回受管目录内的文件；此处即执行授权门禁：
        未获授权的档案抛 VoiceProfileError（client 内还有二次检查作纵深防御）。
        """
        profile = self.get(voice_id)
        voice_profile_require_consent(profile)
        profile["_audio_file"] = str(self.audio_path(voice_id))
        return profile

    def default(self) -> Optional[Dict[str, Any]]:
        for p in self._profiles:
            if p.get("is_default"):
                return dict(p)
        return None

    def audio_path(self, voice_id: str) -> Path:
        """返回参考录音的本地路径；仅限数据目录内的受管文件，防路径穿越。"""
        profile = self.get(voice_id)
        path = (self._dir / profile["reference_audio_path"]).resolve()
        if self._dir.resolve() not in path.parents and path.parent != self._dir.resolve():
            raise VoiceProfileError("音色录音路径非法（越出数据目录）")
        if not path.exists():
            raise VoiceProfileError(f"音色「{profile['display_name']}」的参考录音文件缺失")
        return path

    # ---- 变更 ----

    def create(
        self,
        display_name: str,
        reference_text: str,
        audio_bytes: bytes,
        audio_ext: str,
        *,
        provider: str = "cosyvoice",
        model_id: str = "",
        consent_granted: bool = False,
        source_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """新建音色档案。audio_bytes 为空且给 source_path 时从本地文件复制
        （用于把官方示例音频登记为预设音色），两者都需通过同样的格式校验。"""
        display_name = (display_name or "").strip()
        reference_text = (reference_text or "").strip()
        if not display_name:
            raise VoiceProfileError("音色显示名不能为空")
        if not reference_text:
            raise VoiceProfileError("参考录音文本不能为空（需要录音中实际说出的内容）")
        if audio_bytes and source_path is not None:
            raise VoiceProfileError("audio_bytes 与 source_path 只能二选一")
        if source_path is not None:
            src = Path(source_path)
            if not src.exists():
                raise VoiceProfileError("参考音频源文件不存在")
            audio_bytes = src.read_bytes()
            audio_ext = src.suffix
        if not audio_bytes:
            raise VoiceProfileError("参考音频内容为空")
        if len(audio_bytes) > self._max_audio_bytes:
            raise VoiceProfileError(
                f"参考音频 {len(audio_bytes) / 1024 / 1024:.1f} MB 超过上限"
                f" {self._max_audio_bytes / 1024 / 1024:.0f} MB"
            )
        ext = _sniff_format(audio_bytes, (audio_ext or "").lstrip(".").lower())

        voice_id = uuid.uuid4().hex[:12]
        filename = f"{voice_id}.{ext}"
        target = self._audio_dir / filename
        # 写入受管目录；voice_id 由本模块生成，不含任何用户可控路径成分
        with tempfile.NamedTemporaryFile(dir=self._audio_dir, delete=False) as tf:
            tf.write(audio_bytes)
            temp_name = Path(tf.name)
        try:
            os.replace(temp_name, target)
        except OSError as exc:
            temp_name.unlink(missing_ok=True)
            raise VoiceProfileError(f"保存参考录音失败：{exc}") from exc

        profile = {
            "voice_id": voice_id,
            "display_name": display_name,
            "provider": provider,
            "model_id": model_id,
            "reference_audio_path": f"audio/{filename}",
            "reference_text": reference_text,
            "consent_status": CONSENT_GRANTED if consent_granted else CONSENT_PENDING,
            "created_at": _now(),
            "updated_at": _now(),
            "is_default": not self._profiles,  # 第一个音色自动设为默认
        }
        self._profiles.append(profile)
        self._save()
        _log.info("音色档案已创建 id=%s consent=%s", voice_id, profile["consent_status"])
        return dict(profile)

    def update_consent(self, voice_id: str, granted: bool) -> Dict[str, Any]:
        profile = self.get(voice_id)
        profile["consent_status"] = CONSENT_GRANTED if granted else CONSENT_PENDING
        profile["updated_at"] = _now()
        self._save()
        _log.info("音色授权状态已更新 id=%s granted=%s", voice_id, granted)
        return dict(profile)

    def set_default(self, voice_id: str) -> Dict[str, Any]:
        profile = self.get(voice_id)
        for p in self._profiles:
            p["is_default"] = p["voice_id"] == voice_id
        profile["updated_at"] = _now()
        self._save()
        return dict(profile)

    def delete(self, voice_id: str) -> Dict[str, Any]:
        """删除档案并移除其参考录音文件；返回被删档案（供界面反馈）。

        只删该档案自己的录音与档案记录；生成缓存由调用方决定是否清理。
        """
        profile = self.get(voice_id)
        self._profiles = [p for p in self._profiles if p["voice_id"] != voice_id]
        audio = self._dir / profile["reference_audio_path"]
        if audio.exists() and self._audio_dir.resolve() in audio.resolve().parents:
            audio.unlink()
        self._save()
        _log.info("音色档案已删除 id=%s", voice_id)
        return profile

    # ---- 持久化 ----

    def _load(self) -> List[Dict[str, Any]]:
        if not self._index_path.exists():
            return []
        try:
            payload = json.loads(self._index_path.read_text(encoding="utf-8"))
            profiles = payload.get("profiles", [])
        except (json.JSONDecodeError, OSError) as exc:
            raise VoiceProfileError(
                f"音色档案索引损坏（{self._index_path}）：{exc}。"
                "可修复该文件或将其移走以重建音色库。"
            ) from exc
        return [p for p in profiles if isinstance(p, dict) and p.get("voice_id")]

    def _save(self) -> None:
        payload = {"version": 1, "profiles": self._profiles}
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self._dir, delete=False
            ) as tf:
                json.dump(payload, tf, ensure_ascii=False, indent=2)
                temp_name = Path(tf.name)
            os.replace(temp_name, self._index_path)
        except OSError as exc:
            raise VoiceProfileError(f"保存音色档案失败：{exc}") from exc
