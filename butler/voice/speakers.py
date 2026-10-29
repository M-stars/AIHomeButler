"""家庭成员声纹库（阶段 13）：说话人注册、识别与成员上下文。

职责边界：
  - SpeakerRegistry 只管理"声纹档案"（成员元数据 + 192 维声纹向量）与相似度比对；
    向量提取由 CosyVoice 服务端的 /speaker_embed 完成（复用零样本克隆用的
    campplus 模型），本模块不实现任何音频算法；
  - MemberContext 是"当前说话人"的线程安全占位：入口层（语音模式）在每轮
    对话前设置、结束后清理，系统提示词与记忆工具据此做成员个性化。

隐私与安全（与音色库同等约束）：
  - 声纹向量与成员信息只存本地数据目录（data/speakers/，.gitignore 覆盖），
    绝不入 Git、绝不上传第三方；注册用的原始录音在提取向量后即丢弃（最小化
    生物特征留存），API 响应永远不包含向量本身；
  - 声纹 ≠ 身份认证：录音可冒充，识别结果只用于个性化（称呼/记忆/音色），
    设备控制的确认流程不因此放松；
  - 识别只考虑已获成员本人（或监护人）授权的档案，相似度低于阈值一律判
    "陌生人"回落通用模式，不硬猜。
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger("butler.voice.speakers")

CONSENT_GRANTED = "granted"  # 声纹仅在 granted 时参与识别
HITS_KEEP = 20               # 每个成员保留最近 N 条识别记录


class SpeakerError(Exception):
    """声纹库操作失败，message 面向用户可读。"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _validate_embedding(embedding: Any, expected_dim: Optional[int]) -> int:
    """校验向量并返回维度。"""
    if not isinstance(embedding, (list, tuple)) or not embedding:
        raise SpeakerError("声纹向量缺失或为空（服务端提取失败？）")
    if len(embedding) > 4096:
        raise SpeakerError("声纹向量维度异常")
    for v in embedding:
        if not isinstance(v, (int, float)) or math.isnan(float(v)):
            raise SpeakerError("声纹向量含非法数值")
    dim = len(embedding)
    if expected_dim is not None and dim != expected_dim:
        raise SpeakerError(
            f"声纹向量维度不一致：已有档案 {expected_dim} 维，新向量 {dim} 维"
            "（服务端模型是否更换过？）"
        )
    return dim


def _cosine(a: List[float], b: List[float]) -> float:
    """纯标准库余弦相似度（192 维 × 少量成员，性能足够）。"""
    dot = norm_a = norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


class SpeakerRegistry:
    """声纹档案库：JSON 索引（含向量），无音频文件留存。"""

    def __init__(self, speakers_dir: Path) -> None:
        self._dir = Path(speakers_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._dir / "speakers.json"
        self._members: List[Dict[str, Any]] = []
        self._embed_dim: Optional[int] = None
        self._load()

    # ---- 查询 ----

    def list(self) -> List[Dict[str, Any]]:
        """公开视图：不包含声纹向量（隐私）。"""
        return [{k: v for k, v in m.items() if k != "embedding"} for m in self._members]

    def get(self, member_id: str, with_embedding: bool = False) -> Dict[str, Any]:
        member = self._find(member_id)
        if member is None:
            raise SpeakerError(f"成员不存在：{member_id}")
        if with_embedding:
            return dict(member)
        return {k: v for k, v in member.items() if k != "embedding"}

    def get_by_name(self, display_name: str) -> Optional[Dict[str, Any]]:
        name = (display_name or "").strip()
        for m in self._members:
            if m["display_name"] == name:
                return {k: v for k, v in m.items() if k != "embedding"}
        return None

    @property
    def embed_dim(self) -> Optional[int]:
        return self._embed_dim

    # ---- 变更 ----

    def enroll(
        self,
        display_name: str,
        embedding: List[float],
        *,
        consent_granted: bool = False,
        voice_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        display_name = (display_name or "").strip()
        if not display_name:
            raise SpeakerError("成员显示名不能为空")
        if not consent_granted:
            raise SpeakerError(
                "采集声纹需要该成员本人（未成年人由监护人）明确同意，请先勾选授权"
            )
        if self.get_by_name(display_name) is not None:
            raise SpeakerError(f"成员「{display_name}」已存在，如需重录请先删除")
        dim = _validate_embedding(embedding, self._embed_dim)
        member = {
            "member_id": uuid.uuid4().hex[:12],
            "display_name": display_name,
            "embedding": [float(v) for v in embedding],
            "consent_status": CONSENT_GRANTED,
            "voice_id": voice_id or None,  # 该成员偏好的回答音色（音色库 voice_id）
            "created_at": _now(),
            "updated_at": _now(),
            "hits": {"count": 0, "last_at": None, "last_similarity": None},
        }
        self._members.append(member)
        self._embed_dim = dim
        self._save()
        _log.info("声纹档案已注册 member=%s dim=%d", member["member_id"], dim)
        return self.get(member["member_id"])

    def update_voice(self, member_id: str, voice_id: Optional[str]) -> Dict[str, Any]:
        member = self._find(member_id)
        if member is None:
            raise SpeakerError(f"成员不存在：{member_id}")
        member["voice_id"] = (voice_id or "").strip() or None
        member["updated_at"] = _now()
        self._save()
        return self.get(member_id)

    def update_name(self, member_id: str, display_name: str) -> Dict[str, Any]:
        display_name = (display_name or "").strip()
        if not display_name:
            raise SpeakerError("成员显示名不能为空")
        existing = self.get_by_name(display_name)
        if existing is not None and existing["member_id"] != member_id:
            raise SpeakerError(f"名称「{display_name}」已被其他成员使用")
        member = self._find(member_id)
        if member is None:
            raise SpeakerError(f"成员不存在：{member_id}")
        member["display_name"] = display_name
        member["updated_at"] = _now()
        self._save()
        return self.get(member_id)

    def delete(self, member_id: str) -> Dict[str, Any]:
        member = self.get(member_id)  # 不含向量的公开视图，供界面反馈
        self._members = [m for m in self._members if m["member_id"] != member_id]
        if not self._members:
            self._embed_dim = None
        self._save()
        _log.info("声纹档案已删除 member=%s", member_id)
        return member

    # ---- 识别 ----

    def identify(
        self, embedding: List[float], threshold: float, *, record: bool = False
    ) -> Tuple[Optional[Dict[str, Any]], float]:
        """与全部已授权成员比对，返回 (成员|None, 最高相似度)。

        低于阈值或无已授权成员 → (None, 相似度)——调用方按"陌生人"处理。
        record=True 时把命中写入该成员的识别记录（供界面查看）。
        """
        _validate_embedding(embedding, self._embed_dim)
        best: Optional[Dict[str, Any]] = None
        best_sim = -1.0
        for m in self._members:
            if m.get("consent_status") != CONSENT_GRANTED:
                continue
            sim = _cosine(embedding, m["embedding"])
            if sim > best_sim:
                best, best_sim = m, sim
        if best is None or best_sim < threshold:
            if best is None:
                _log.info("声纹识别：无已授权成员档案")
            else:
                _log.info("声纹识别：最高相似度 %.3f 低于阈值 %.3f，按陌生人处理",
                          best_sim, threshold)
            return None, max(best_sim, 0.0)
        if record:
            self.record_hit(best["member_id"], best_sim)
        return self.get(best["member_id"]), best_sim

    def record_hit(self, member_id: str, similarity: float) -> None:
        member = self._find(member_id)
        if member is None:
            return
        hits = member["hits"]
        hits["count"] = int(hits.get("count", 0)) + 1
        hits["last_at"] = _now()
        hits["last_similarity"] = round(float(similarity), 4)
        member["hits"]["recent"] = (member["hits"].get("recent") or [])[-(HITS_KEEP - 1):] + [
            {"at": _now(), "similarity": round(float(similarity), 4)}
        ]
        self._save()

    # ---- 持久化 ----

    def _find(self, member_id: str) -> Optional[Dict[str, Any]]:
        for m in self._members:
            if m["member_id"] == member_id:
                return m
        return None

    def _load(self) -> None:
        if not self._index_path.exists():
            return
        try:
            payload = json.loads(self._index_path.read_text(encoding="utf-8"))
            members = payload.get("members", [])
        except (json.JSONDecodeError, OSError) as exc:
            raise SpeakerError(
                f"声纹档案索引损坏（{self._index_path}）：{exc}。"
                "可修复该文件或将其移走以重建声纹库。"
            ) from exc
        dims = set()
        for m in members:
            if not (isinstance(m, dict) and m.get("member_id") and m.get("embedding")):
                continue
            m.setdefault("hits", {"count": 0, "last_at": None, "last_similarity": None})
            m["embedding"] = [float(v) for v in m["embedding"]]
            dims.add(len(m["embedding"]))
            self._members.append(m)
        if len(dims) > 1:
            raise SpeakerError("声纹档案向量维度不一致，索引可能已损坏")
        if dims:
            self._embed_dim = dims.pop()

    def _save(self) -> None:
        payload = {
            "version": 1,
            "embed_dim": self._embed_dim,
            "members": self._members,
        }
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self._dir, delete=False
            ) as tf:
                json.dump(payload, tf, ensure_ascii=False)
                temp_name = Path(tf.name)
            os.replace(temp_name, self._index_path)
        except OSError as exc:
            raise SpeakerError(f"保存声纹档案失败：{exc}") from exc


class MemberContext:
    """当前说话人的线程安全占位。

    语音模式每轮：录音 → 声纹识别 → context.set(member) → agent.process()
    → context.clear()。Web 文本对话不设置（None = 全局模式，行为不变）。
    """

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._member: Optional[Dict[str, Any]] = None

    def set(self, member: Optional[Dict[str, Any]]) -> None:
        with self._lock:
            self._member = dict(member) if member else None

    def get(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._member) if self._member else None

    def clear(self) -> None:
        with self._lock:
            self._member = None
