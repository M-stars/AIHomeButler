"""CosyVoice 服务端验收自测（不依赖主项目配置，只依赖 httpx）。

用法（主项目 Python 环境，服务已启动时）：
  python scripts/cosyvoice_selftest.py

流程：
  1. 健康检查（GET /docs）；
  2. 预设音色合成一句中文（/inference_sft，若模型无预设音色则自动跳过）；
  3. 零样本合成（/inference_zero_shot，参考音频用官方示例 asset/zero_shot_prompt.wav）；
  4. 输出 WAV 文件路径与耗时/音频时长/RTF 指标。

只调用官方服务端的官方 API，不包含任何模型推理代码。
"""
import sys
import time
import wave
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9880"
SAMPLE_RATE = int(sys.argv[2]) if len(sys.argv) > 2 else 24000
TEXT = "你好，卧室现在的温度是二十五度。"
ASSET_WAV = REPO / "cosyvoice" / "CosyVoice" / "asset" / "zero_shot_prompt.wav"
# CosyVoice3 官方 zero-shot 格式：prompt_text 必须带该前缀（llm.py 会断言检查）
PROMPT_PREFIX = "You are a helpful assistant.<|endofprompt|>"
PROMPT_TEXT = PROMPT_PREFIX + "希望你以后能够做的比我还好呦。"
OUT_DIR = Path.home() / ".butler_selftest"


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    import struct

    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack("<I", 16) + struct.pack(
        "<HHIIHH", 1, 1, sample_rate, sample_rate * 2, 2, 16
    )
    header += b"data" + struct.pack("<I", len(pcm))
    return header + pcm


def save_wav(raw: bytes, name: str) -> tuple:
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / name
    if raw[:4] == b"RIFF":
        path.write_bytes(raw)
    else:
        path.write_bytes(pcm_to_wav(raw, SAMPLE_RATE))
    with wave.open(str(path), "rb") as wf:
        seconds = wf.getnframes() / wf.getframerate()
    return path, seconds


def main() -> int:
    client = httpx.Client(timeout=httpx.Timeout(connect=5, read=300, write=60, pool=5))

    print(f"[1/3] 健康检查 {BASE_URL}/docs ...", end=" ")
    try:
        resp = client.get(f"{BASE_URL}/docs", timeout=5)
    except httpx.HTTPError as exc:
        print("失败（服务未启动？）:", exc.__class__.__name__)
        return 2
    print("HTTP", resp.status_code, "OK" if resp.status_code == 200 else "(异常)")

    results = []

    print(f"[2/3] 预设音色合成（/inference_sft）...")
    t0 = time.monotonic()
    try:
        resp = client.post(f"{BASE_URL}/inference_sft",
                           data={"tts_text": TEXT, "spk_id": "中文女"})
    except httpx.HTTPError as exc:
        resp = None
        print(f"      跳过：连接中断（{exc.__class__.__name__}；模型通常无预设音色 spk2info.pt，属预期）")
    took = time.monotonic() - t0
    if resp is not None:
        if resp.status_code == 200 and len(resp.content) > 44:
            path, seconds = save_wav(resp.content, "sft_preset.wav")
            results.append(("预设音色", path, took, seconds))
            print(f"      OK：{took:.1f}s 合成 {seconds:.1f}s 音频（RTF {took / seconds:.2f}）→ {path}")
        else:
            print(f"      跳过/失败：HTTP {resp.status_code}（模型可能无预设音色，属预期）")

    if ASSET_WAV.exists():
        print(f"[3/3] 零样本克隆合成（/inference_zero_shot，官方示例音频）...")
        t0 = time.monotonic()
        try:
            with open(ASSET_WAV, "rb") as fh:
                resp = client.post(
                    f"{BASE_URL}/inference_zero_shot",
                    data={"tts_text": TEXT, "prompt_text": PROMPT_TEXT},
                    files={"prompt_wav": ("zero_shot_prompt.wav", fh)},
                )
        except httpx.HTTPError as exc:
            resp = None
            print(f"      失败：连接中断 {exc.__class__.__name__}（详见服务端日志）")
        took = time.monotonic() - t0
        if resp is not None and resp.status_code == 200 and len(resp.content) > 44:
            path, seconds = save_wav(resp.content, "zero_shot_sample.wav")
            results.append(("零样本(官方示例)", path, took, seconds))
            print(f"      OK：{took:.1f}s 合成 {seconds:.1f}s 音频（RTF {took / seconds:.2f}）→ {path}")
        elif resp is not None:
            print(f"      失败：HTTP {resp.status_code} {resp.text[:200]}")
    else:
        print(f"[3/3] 跳过零样本：未找到 {ASSET_WAV}")

    if not results:
        print("\n结论：未生成任何音频——检查服务日志。")
        return 1
    print("\n摘要：")
    for label, path, took, seconds in results:
        print(f"  {label}: {path}（{seconds:.1f}s 音频 / {took:.1f}s 耗时 / RTF {took / seconds:.2f}）")
    print("请人工播放确认音质与音色。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
