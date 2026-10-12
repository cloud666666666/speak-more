# coding: utf-8
"""
模型与推理库（llama.cpp DLL）的路径表 —— 服务端与客户端共用的纯数据。

为什么单独放一份：
  客户端要做「缺模型 / 缺 DLL」的启动预检，但它**不能 import config_server** ——
  那会在客户端进程里连带跑一遍显卡探测（建 ONNX 会话、占显存），而且客户端本来
  只该依赖 config_client。所以把这张表抽出来，只依赖标准库。

  config_server 依旧照常提供 ModelPaths / ModelDownloadLinks（从本模块导入），
  服务端那边的 `from config_server import ModelPaths` 一个都不用改。

路径都是相对「安装目录」的（运行时 cwd 就是安装目录，见 app.py 的 os.chdir）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class ModelDownloadLinks:
    """模型下载链接配置"""
    # 统一导向 GitHub Release 模型页面
    models_page = "https://github.com/HaujetZhao/CapsWriter-Offline/releases/tag/models"


class ModelPaths:
    """模型文件路径配置"""

    # 基础目录
    model_dir = Path() / 'models'

    # Paraformer 模型路径
    paraformer_dir = model_dir / 'Paraformer' / "speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-onnx"
    paraformer_model = paraformer_dir / 'model.onnx'
    paraformer_tokens = paraformer_dir / 'tokens.txt'

    # 标点模型路径
    punc_model_dir = model_dir / 'Punct-CT-Transformer' / 'sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12' / 'model.onnx'

    # SenseVoice 模型路径，自带标点
    sensevoice_dir = model_dir / 'SenseVoice-Small' / 'Sensevoice-Small-ONNX'
    sensevoice_encoder = sensevoice_dir / 'SenseVoice-Encoder.fp16.onnx'
    sensevoice_decoder = sensevoice_dir / 'SenseVoice-CTC.fp16.onnx'
    sensevoice_tokenizer = sensevoice_dir / 'tokenizer.bpe.model'

    # Fun-ASR-Nano 模型路径，自带标点
    fun_asr_nano_gguf_dir = model_dir / 'Fun-ASR-Nano' / 'Fun-ASR-Nano-GGUF'
    fun_asr_nano_gguf_encoder_adaptor = fun_asr_nano_gguf_dir / 'Fun-ASR-Nano-Encoder-Adaptor.fp16.onnx'
    fun_asr_nano_gguf_ctc = fun_asr_nano_gguf_dir / 'Fun-ASR-Nano-CTC.fp16.onnx'
    fun_asr_nano_gguf_llm_decode = fun_asr_nano_gguf_dir / 'Fun-ASR-Nano-Decoder.q5_k.gguf'
    fun_asr_nano_gguf_token = fun_asr_nano_gguf_dir / 'tokens.txt'
    fun_asr_nano_gguf_hotwords = Path() / 'hot-server.txt'

    # Qwen3-ASR 模型路径，自带标点
    qwen3_asr_gguf_dir = model_dir / 'Qwen3-ASR' / 'Qwen3-ASR-1.7B'
    qwen3_asr_gguf_encoder_frontend = qwen3_asr_gguf_dir / 'qwen3_asr_encoder_frontend.onnx'
    qwen3_asr_gguf_encoder_backend = qwen3_asr_gguf_dir / 'qwen3_asr_encoder_backend.onnx'
    qwen3_asr_gguf_llm_decode = qwen3_asr_gguf_dir / 'qwen3_asr_llm.gguf'

    # Force-Aligner 模型路径
    force_aligner_gguf_dir = model_dir / 'Qwen3-ForcedAligner' / 'Qwen3-ForcedAligner-0.6B'
    force_aligner_gguf_encoder_frontend = force_aligner_gguf_dir / 'qwen3_aligner_encoder_frontend.int4.onnx'
    force_aligner_gguf_encoder_backend = force_aligner_gguf_dir / 'qwen3_aligner_encoder_backend.int4.onnx'
    force_aligner_gguf_llm_decode = force_aligner_gguf_dir / 'qwen3_aligner_llm.q5_k.gguf'


# ============================================================
#  每种模型类型需要哪些文件
# ============================================================

_MODEL_REQUIREMENTS = {
    'fun_asr_nano': (
        ModelPaths.fun_asr_nano_gguf_dir,
        (ModelPaths.fun_asr_nano_gguf_encoder_adaptor,
         ModelPaths.fun_asr_nano_gguf_ctc,
         ModelPaths.fun_asr_nano_gguf_llm_decode,
         ModelPaths.fun_asr_nano_gguf_token),
    ),
    'sensevoice': (
        ModelPaths.sensevoice_dir,
        (ModelPaths.sensevoice_encoder,
         ModelPaths.sensevoice_decoder,
         ModelPaths.sensevoice_tokenizer),
    ),
    'paraformer': (
        ModelPaths.paraformer_dir,
        (ModelPaths.paraformer_model,
         ModelPaths.paraformer_tokens),
    ),
    'qwen_asr': (
        ModelPaths.qwen3_asr_gguf_dir,
        (ModelPaths.qwen3_asr_gguf_encoder_frontend,
         ModelPaths.qwen3_asr_gguf_encoder_backend,
         ModelPaths.qwen3_asr_gguf_llm_decode),
    ),
}

SUPPORTED_MODEL_TYPES = tuple(_MODEL_REQUIREMENTS.keys())


def required_model_files(model_type: str) -> Optional[Tuple[Path, List[Path]]]:
    """
    返回 (模型目录, 必需文件列表)；模型类型不认识时返回 None。
    """
    entry = _MODEL_REQUIREMENTS.get((model_type or '').lower())
    if entry is None:
        return None
    model_dir, files = entry
    return model_dir, list(files)


# ============================================================
#  llama.cpp 的 DLL：每个引擎各自从自己的 inference/bin 加载
# ============================================================

# 注意：core/server/engines/llama/bin 是**历史遗留**，现在的引擎只从各自的
# inference/bin 加载（每个 *_gguf/inference/llama.py 里写死了 Path(__file__).parent/'bin'），
# 没有回退路径。上游文档里还写着往 engines/llama 里替换 DLL，那是旧版本的说明。
_ENGINE_DLL_DIRS = {
    'qwen_asr': 'core/server/engines/qwen_asr_gguf/inference/bin',
    'fun_asr_nano': 'core/server/engines/fun_asr_gguf/inference/bin',
    'paraformer': None,          # 走 sherpa_onnx，不用这套 DLL
    'sensevoice': None,          # 同上（ONNX 推理）
}

# 对齐器（文件转录用）也走 GGUF，需要它自己的那一份
ALIGNER_DLL_DIR = 'core/server/engines/force_aligner_gguf/inference/bin'

# 三个必装的库文件（缺一个 llama.cpp 都起不来）
REQUIRED_LLAMA_DLLS = ('ggml.dll', 'ggml-base.dll', 'llama.dll')


def required_dll_dirs(model_type: str) -> List[str]:
    """返回该模型类型下必须齐备的 DLL 目录（相对安装目录）。"""
    dirs: List[str] = []
    d = _ENGINE_DLL_DIRS.get((model_type or '').lower())
    if d:
        dirs.append(d)
    return dirs


# ============================================================
#  检查：只读、不打印、不退出 —— 服务端与客户端共用同一份判断
# ============================================================

def find_missing_models(model_type: str) -> Dict[str, Any]:
    """
    核对模型文件是否齐备。

    返回:
        {
          'model_type': str,
          'supported': bool,        # 模型类型认不认识
          'model_dir': Path | None, # 该模型应该放的目录
          'missing': [Path, ...],   # 缺哪些文件
          'misplaced': bool,        # 有没有被放到上级目录（最常见的新手错误）
          'download_page': str,
        }

    **不打日志、不打印、不退出** —— 调用方自己决定怎么呈现。
    """
    mt = (model_type or '').lower()
    result: Dict[str, Any] = {
        'model_type': mt,
        'supported': False,
        'model_dir': None,
        'missing': [],
        'misplaced': False,
        'download_page': ModelDownloadLinks.models_page,
    }
    req = required_model_files(mt)
    if req is None:
        return result

    model_dir, files = req
    result['supported'] = True
    result['model_dir'] = model_dir

    missing = [f for f in files if not f.exists()]
    result['missing'] = missing

    # 模型文件错放到上一级目录是很常见的：只提示，不自动搬
    for parent in model_dir.parents:
        if any((parent / f.name).exists() for f in missing):
            result['misplaced'] = True
            break
    return result


def find_missing_dlls(model_type: str, root: Optional[Path] = None) -> List[str]:
    """
    核对 llama.cpp 的 DLL 是否齐备（每个引擎从各自的 inference/bin 加载）。

    root 传安装目录；不传就按相对路径检查（依赖 cwd 已是安装目录）。
    返回缺失文件的说明串列表（空 = 没问题）。
    """
    base = Path(root) if root is not None else Path()
    missing: List[str] = []
    for rel in required_dll_dirs(model_type):
        d = base / rel
        if not d.is_dir():
            missing.append(f'{rel}（整个目录都不在）')
            continue
        for dll in REQUIRED_LLAMA_DLLS:
            if not (d / dll).exists():
                missing.append(f'{rel}/{dll}')
    return missing
