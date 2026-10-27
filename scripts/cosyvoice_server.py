"""CosyVoice 服务端启动器（薄适配层）。

为什么不直接跑官方 runtime/python/fastapi/server.py：
  官方 server 把上传音频先 load_wav(file, 16000) 转成张量再传给
  inference_zero_shot，而当前推荐模型 Fun-CosyVoice3-0.5B 的前端
  （cosyvoice/cli/frontend.py）在 _extract_speech_feat/_extract_speech_token
  里会再次 load_wav(prompt_wav, 24000)，只接受文件路径——传张量必然
  TypeError（2026-10-28 实测复现，官方仓库对 CosyVoice3 的服务端未更新）。

因此本文件按任务原则"官方服务端不适用时编写最薄适配层"实现：
  - 复用官方 AutoModel 与官方推理代码（模型加载/inference 全部官方原版）；
  - 路由形状与官方 server 一致（表单参数、int16 PCM 流式响应），主项目
    客户端无需感知差异；
  - zero-shot 改为把上传音频落盘为临时文件、把"路径"交给官方推理代码；
  - 监听地址收敛为 127.0.0.1（官方 __main__ 固定 0.0.0.0，会暴露局域网）。

用法（在 AI Home Butler 仓库根目录）：
  cosyvoice/.venv/Scripts/python.exe scripts/cosyvoice_server.py ^
      --model_dir cosyvoice/pretrained_models/Fun-CosyVoice3-0.5B --port 9880

健康检查：GET http://127.0.0.1:9880/docs 返回 200 即服务就绪。
"""
import argparse
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import StreamingResponse

REPO = Path(__file__).resolve().parent.parent
OFFICIAL_SERVER = REPO / "cosyvoice" / "CosyVoice" / "runtime" / "python" / "fastapi" / "server.py"


def load_official_module():
    """以模块方式加载官方 server.py（不改其文件），拿到官方 AutoModel 导入。"""
    if not OFFICIAL_SERVER.exists():
        print(f"[错误] 未找到官方仓库：{OFFICIAL_SERVER.parent}")
        print("请先按 docs/COSYVOICE_SETUP.md 克隆官方仓库到 cosyvoice/CosyVoice")
        raise SystemExit(2)
    spec = importlib.util.spec_from_file_location("cosyvoice_official_server", OFFICIAL_SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # 官方模块把仓库根与 Matcha-TTS 加进 sys.path
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description="CosyVoice 服务端启动器（仅本机监听）")
    parser.add_argument("--host", default="127.0.0.1",
                        help="默认只监听本机；不要改成 0.0.0.0 暴露到局域网")
    parser.add_argument("--port", type=int, default=9880)
    parser.add_argument(
        "--model_dir",
        default=str(REPO / "cosyvoice" / "pretrained_models" / "Fun-CosyVoice3-0.5B"),
        help="本地模型目录（或 ModelScope 模型 ID，首次会自动下载）",
    )
    args = parser.parse_args()

    official = load_official_module()

    print(f"[CosyVoice] 开始加载模型：{args.model_dir}")
    print("[CosyVoice] 首次启动需数十秒（权重载入 + ONNX 初始化），请等待就绪提示")
    cosyvoice = official.AutoModel(model_dir=args.model_dir)
    try:
        spks = cosyvoice.list_available_spks()
    except Exception:
        spks = []
    print(f"[CosyVoice] 模型就绪：采样率 {cosyvoice.sample_rate} Hz，"
          f"预设音色 {spks or '（无 spk2info.pt，请使用音色库零样本克隆）'}")

    # 与官方 server 相同的响应封装：int16 PCM 字节流（裸流，客户端按采样率封 WAV）
    def generate_data(model_output, temp_path=None):
        # 清理挂在生成器 finally：StreamingResponse 惰性消费，
        # 路由函数返回时流尚未生成，删除必须等流读完
        try:
            for i in model_output:
                tts_audio = (i['tts_speech'].numpy() * (2 ** 15)).astype(np.int16).tobytes()
                yield tts_audio
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    app = FastAPI(title="CosyVoice (butler adapter)")  # 自带 /docs，用作健康检查探针

    @app.get("/inference_sft")
    @app.post("/inference_sft")
    async def inference_sft(tts_text: str = Form(), spk_id: str = Form()):
        model_output = cosyvoice.inference_sft(tts_text, spk_id)
        return StreamingResponse(generate_data(model_output))

    @app.get("/inference_zero_shot")
    @app.post("/inference_zero_shot")
    async def inference_zero_shot(tts_text: str = Form(), prompt_text: str = Form(),
                                  prompt_wav: UploadFile = File()):
        # 关键差异：CosyVoice3 前端要求文件路径（内部自行 load_wav 重采样），
        # 因此把上传内容落盘为临时文件，流式响应结束后由 generate_data 清理
        suffix = Path(prompt_wav.filename or "prompt.wav").suffix or ".wav"
        fd, temp_path = tempfile.mkstemp(suffix=suffix, prefix="cosyvoice_prompt_")
        os.close(fd)
        with open(temp_path, "wb") as out:
            out.write(await prompt_wav.read())
        model_output = cosyvoice.inference_zero_shot(tts_text, prompt_text, temp_path)
        return StreamingResponse(generate_data(model_output, temp_path))

    @app.get("/inference_cross_lingual")
    @app.post("/inference_cross_lingual")
    async def inference_cross_lingual(tts_text: str = Form(),
                                      prompt_wav: UploadFile = File()):
        suffix = Path(prompt_wav.filename or "prompt.wav").suffix or ".wav"
        fd, temp_path = tempfile.mkstemp(suffix=suffix, prefix="cosyvoice_prompt_")
        os.close(fd)
        with open(temp_path, "wb") as out:
            out.write(await prompt_wav.read())
        model_output = cosyvoice.inference_cross_lingual(tts_text, temp_path)
        return StreamingResponse(generate_data(model_output, temp_path))

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
