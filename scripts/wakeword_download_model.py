"""下载唤醒词 KWS 模型（sherpa-onnx zipformer wenetspeech 3.3M，Apache-2.0）。

模型来源：k2-fsa/sherpa-onnx 官方 releases（kws-models 标签），训练数据为
wenetspeech L（10000 小时中文），int8 量化后约 33 MB。存放位置：
data/models/sherpa-kws（.gitignore 覆盖，不入 Git）。

下载后自动确保 keywords.txt 含默认唤醒词「小管家」；要改唤醒词直接编辑该文件，
每行格式：x iǎo g uǎn j iā @小管家（声母+带调韵母，空格分隔，@后为显示标签）。

用法：python scripts/wakeword_download_model.py [自定义下载URL]
"""
from __future__ import annotations

import io
import sys
import tarfile
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = PROJECT_ROOT / "data" / "models" / "sherpa-kws"
DEFAULT_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
    "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2"
)
DEFAULT_KEYWORD_LINE = "x iǎo g uǎn j iā @小管家"


def _extract_strip_topdir(archive_bytes: bytes, dest: Path) -> int:
    """解压 tar.bz2 并去掉压缩包顶层目录；防御路径穿越（不写 dest 之外）。"""
    count = 0
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:bz2") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            rel = Path(member.name)
            if rel.is_absolute() or ".." in rel.parts:
                print(f"[跳过] 可疑路径：{member.name}")
                continue
            parts = rel.parts[1:] if len(rel.parts) > 1 else rel.parts
            target = dest.joinpath(*parts)
            if not target.resolve().is_relative_to(dest.resolve()):
                print(f"[跳过] 解析到目录外：{member.name}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tf.extractfile(member).read())
            count += 1
    return count


def _install_keywords() -> None:
    """安装唤醒词表：模型自带的示例词表（8 个演示词）移到 keywords_examples.txt，
    keywords.txt 只保留默认唤醒词「小管家」——示例词全部生效会频繁误唤醒。
    要增删唤醒词直接编辑 keywords.txt（格式见文件内各行）。"""
    kw = MODEL_DIR / "keywords.txt"
    examples = MODEL_DIR / "keywords_examples.txt"
    if kw.exists() and not examples.exists():
        kw.rename(examples)
        print("模型自带示例词表已保留为 keywords_examples.txt（默认不生效）")
    kw.write_text(DEFAULT_KEYWORD_LINE + "\n", encoding="utf-8")
    print(f"已写入默认唤醒词：{DEFAULT_KEYWORD_LINE}")


def main() -> int:
    url = sys.argv[1].strip() if len(sys.argv) > 1 else DEFAULT_URL
    if (MODEL_DIR / "tokens.txt").exists():
        print(f"模型已存在（{MODEL_DIR}），如需重下请先删除该目录。")
        return 0

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"下载唤醒词模型（约 33 MB）……\n{url}")
    try:
        with urllib.request.urlopen(url, timeout=600) as resp:
            archive_bytes = resp.read()
    except Exception as exc:
        print(f"[错误] 下载失败：{exc}\n"
              "可换镜像源重试，例：\n"
              f"  python scripts/wakeword_download_model.py https://ghproxy.cn/{DEFAULT_URL}")
        return 1
    print(f"下载完成（{len(archive_bytes) / 1024 / 1024:.1f} MB），解压中……")
    count = _extract_strip_topdir(archive_bytes, MODEL_DIR)
    if not (MODEL_DIR / "tokens.txt").exists():
        print("[错误] 解压后未找到 tokens.txt，请检查下载内容是否完整。")
        return 1
    _install_keywords()
    print(f"完成：{count} 个文件 → {MODEL_DIR}")
    print("下一步：.env 设 WAKEWORD_ENABLED=1，python -m butler.voice --mic --wake")
    return 0


if __name__ == "__main__":
    sys.exit(main())
