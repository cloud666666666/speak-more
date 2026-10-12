# coding: utf-8
"""
显卡探测与推理后端自动选择

目标：机器上只要有能用的显卡，就优先把推理放到显卡上跑，降低单句延迟。

三块能力彼此独立，任何一块失败都不影响其它两块：

1. ONNX 编码器后端（onnx_provider）
   依据 onnxruntime 自己报告的可用 provider 选最优：TensorRT > CUDA > DML > CPU。
   判据必须是 ort.get_available_providers()，**不能看显卡品牌** —— 能不能上 CUDA
   取决于装的是 onnxruntime-gpu 还是 onnxruntime-directml（本项目 pyproject 里是
   onnxruntime-directml，所以实际只有 DML + CPU）。

2. GGUF 解码器后端（llm_use_gpu，llama.cpp）
   依据引擎 bin 目录里实际存在的后端 DLL 判断（ggml-vulkan / ggml-cuda）。
   上游默认 llm_use_gpu=True 只有在 GPU 后端存在时才有意义。

3. 显卡清单（只用于展示）
   WMI 枚举 + 虚拟显示适配器过滤（Todesk / MuMu / GameViewer 这类"虚拟显示器"
   如果不滤掉，会让人以为机器上多插了几块卡）。结果缓存到 logs/gpu_probe.json。
   注意：清单**不参与后端决策** —— 决策只依赖上面两条随时可查的信息，
   这样在任何进程里都能便宜地算出结果。

写回对象是 config_server 里那几个 Args 类（Qwen3ASRGGUFArgs 等），
EngineFactory.create_asr_engine 会在构造引擎时把整个类的属性读走。

配置入口（写在 config_local.json 的 server 段）：
    {
      "server": {
        "gpu_auto": true,           # 总开关；关掉则完全按显式值来
        "onnx_provider": "auto",    # auto / cpu / dml / cuda / trt
        "llm_use_gpu": "auto"       # auto / true / false
      }
    }

硬性约束：只依赖标准库（onnxruntime 是可选依赖）；任何一步失败都回落保守值，
绝不让服务端起不来。
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

def _install_root() -> Path:
    """程序根目录（打包时是 exe 同级目录）。理由见 core/tools/paths.py。"""
    try:
        from core.tools.paths import install_root
        return install_root()
    except Exception:
        return Path(__file__).resolve().parents[2]


_REPO_ROOT = _install_root()
_CACHE_PATH = _REPO_ROOT / 'logs' / 'gpu_probe.json'
_CACHE_TTL_DAYS = 7          # 硬件清单最多缓存这么久，超期重新枚举
_WMI_TIMEOUT = 15            # 拉 powershell 的超时，超了就跳过（只影响展示）

# ORT 的 provider 全名 -> 引擎 Args 用的短名
# （core/server/engines/*/inference/encoder.py 只认 'TRT' / 'CUDA' / 'DML' / 'CPU'）
_PROVIDER_ORDER: Tuple[Tuple[str, str], ...] = (
    ('TensorrtExecutionProvider', 'TRT'),
    ('CUDAExecutionProvider', 'CUDA'),
    ('DmlExecutionProvider', 'DML'),
)

# 用户写在配置里的别名 -> 短名；'auto' 表示交给检测
_PROVIDER_ALIASES = {
    'auto': 'auto',
    'cpu': 'CPU',
    'dml': 'DML', 'directml': 'DML',
    'cuda': 'CUDA',
    'trt': 'TRT', 'tensorrt': 'TRT',
}

# 虚拟显示适配器的名字特征（远程桌面 / 安卓模拟器 / 投屏 / 显卡坞透传都会装一块）
_VIRTUAL_HINTS = (
    'virtual', 'todesk', 'mumu', 'gameviewer', 'parsec', 'sunshine', 'rainway',
    'rdp', 'remote display', 'basic display', 'basic render', 'indirect display',
    'idd ', 'displaylink', 'citrix', 'vmware', 'virtualbox', 'hyper-v',
    'anydesk', 'teamviewer', 'splashtop', 'sunlogin', '向日葵', 'oray',
    'meta quest', 'oculus', 'spacedesk', 'duet display', 'astropad',
)

_VENDORS = {
    0x10DE: 'NVIDIA',
    0x1002: 'AMD',
    0x1022: 'AMD',
    0x8086: 'Intel',
    0x13B5: 'ARM',
    0x5143: 'Qualcomm',
    0x1414: 'Microsoft',
}

# 这几个引擎都有 onnx_provider / llm_use_gpu 两个属性
_GPU_TARGET_ARGS = (
    'SenseVoiceArgs',
    'FunASRNanoGGUFArgs',
    'Qwen3ASRGGUFArgs',
    'ForceAlignerGGUFArgs',
)

_ENGINE_BIN_DIRS = (
    'core/server/engines/llama/bin',
    'core/server/engines/qwen_asr_gguf/inference/bin',
    'core/server/engines/fun_asr_gguf/inference/bin',
    'core/server/engines/force_aligner_gguf/inference/bin',
)

# 本进程最近一次探测结果（config_server 的模块体里算完，logger 建好后由
# core/server/__init__.py 取出来打日志）
_LAST_DECISION: Optional[Dict[str, Any]] = None


# ============================================================
#  1. ONNX Runtime 可用 provider（含「验活」）
# ============================================================

# 「验活」用的极小模型：单个 Conv 节点，约 230 字节，base64 内联，不依赖 onnx 包。
#
# 为什么非要真建一次会话：onnxruntime 在 provider 起不来时会**静默回落到 CPU**，
# 而 get_available_providers() 照样把它列出来。实测被坑过两次：
#   ① 装了 onnxruntime-gpu 但缺 CUDA 运行时 → provider 列着，实际跑 CPU；
#   ② cuDNN 版本不匹配（9.27 配 ORT 1.24）→ CUDNN Frontend 建图失败，
#      前端 session 静默回落 CPU，后端才用 GPU。
# 用 Identity 节点做探针也不行：CUDA EP 不接管 Identity，ORT 会把「没分到节点」的
# provider 从 sess.get_providers() 里剔掉，得到假阴性。Conv 会被 CUDA EP 接管，
# 而且会走 cuDNN，正好覆盖第 ② 类故障。
_PROBE_MODEL_B64 = (
    'CAcSEHNwZWFrLW1vcmUtcHJvYmU6vwEKSQoBWAoBVxIBWSIEQ29udioVCgxrZXJuZWxfc2hhcGWgAQdC'
    'AgMDKg8KBHBhZHOgAQdCBAEBAQEqEAoHc3RyaWRlc6ABB0ICAQESBXByb2JlKjEKBAEBAwMQAUIBV0ok'
    'zczMPc3MzD3NzMw9zczMPc3MzD3NzMw9zczMPc3MzD3NzMw9WhsKAVgSFgoUCAESEAoCCAEKAggBCgII'
    'CAoCCAhiGwoBWRIWChQIARIQCgIIAQoCCAEKAggICgIICEICEA0='
)

_probe_model_bytes: Optional[bytes] = None
_verified: Dict[str, bool] = {}          # provider 全名 -> 验活结果（进程内缓存）
_cuda_dlls_loaded: Optional[bool] = None


def _probe_model() -> Optional[bytes]:
    global _probe_model_bytes
    if _probe_model_bytes is None:
        try:
            import base64
            _probe_model_bytes = base64.b64decode(_PROBE_MODEL_B64)
        except Exception:
            _probe_model_bytes = b''
    return _probe_model_bytes or None


def ensure_cuda_dlls() -> bool:
    """
    让 onnxruntime 能找到 pip 安装的 CUDA 运行库（nvidia-*-cu12 那批）。

    为什么必须做：装了 onnxruntime-gpu + pip 版 CUDA 库时，那些 DLL 躺在
    site-packages/nvidia/*/bin 里，不在 PATH 上。不调 preload_dlls()，
    onnxruntime **不会报错**，只是静静地全部跑 CPU（实测：不调 → CPU，
    调了 → CUDAExecutionProvider 生效）。

    必须在任何 InferenceSession 创建之前调用 —— 本模块在 config_server 的模块体里
    执行，早于引擎加载，正好覆盖。返回是否成功调用（没有 CUDA 库时 onnxruntime
    自己会静默跳过，不抛异常，此时返回值不代表"CUDA 可用"）。
    """
    global _cuda_dlls_loaded
    if _cuda_dlls_loaded is not None:
        return _cuda_dlls_loaded
    _cuda_dlls_loaded = False
    try:
        import onnxruntime as ort
        preload = getattr(ort, 'preload_dlls', None)
        if preload is not None:
            preload()
            _cuda_dlls_loaded = True
    except Exception:
        # 没有 CUDA 库（比如 directml 版）时这里会抛，属正常情况
        _cuda_dlls_loaded = False
    return _cuda_dlls_loaded


def available_providers() -> List[str]:
    """返回 onnxruntime 自己报告的 provider 全名列表（**不代表真能生效**）。"""
    try:
        import onnxruntime as ort
        return list(ort.get_available_providers())
    except Exception:
        return []


def verify_provider(full_name: str) -> bool:
    """
    真建一个极小会话，看这个 provider 是否**实际生效**（而不是回落 CPU）。

    这是本模块最重要的一道防线：onnxruntime 的静默回落没有任何 API 能直接问出来，
    只能拿一个小模型试。结果按进程缓存。
    """
    if full_name in _verified:
        return _verified[full_name]
    ok = False
    try:
        import onnxruntime as ort
        if full_name.startswith('CUDA') or full_name.startswith('Tensorrt'):
            ensure_cuda_dlls()
        model = _probe_model()
        if model:
            opts = ort.SessionOptions()
            opts.log_severity_level = 4          # 静音，只看结果
            sess = ort.InferenceSession(model, sess_options=opts, providers=[full_name])
            actual = list(sess.get_providers() or [])
            ok = bool(actual) and actual[0] == full_name
    except Exception:
        ok = False
    _verified[full_name] = ok
    return ok


def best_onnx_provider(providers: Optional[List[str]] = None) -> str:
    """
    按优先级挑一个**验活通过**的 provider，返回引擎用的短名（TRT/CUDA/DML/CPU）。
    验活不通过的一律跳过，最后回落到 CPU。
    """
    providers = available_providers() if providers is None else providers
    for full, short in _PROVIDER_ORDER:
        if full in providers and verify_provider(full):
            return short
    return 'CPU'


def rejected_providers(providers: Optional[List[str]] = None) -> List[str]:
    """列出"报告可用但验活失败"的 provider —— 这是要给用户看的重点。"""
    providers = available_providers() if providers is None else providers
    return [full for full, _ in _PROVIDER_ORDER
            if full in providers and not verify_provider(full)]


# ============================================================
#  2. llama.cpp 后端
# ============================================================

def llama_backend() -> Dict[str, Any]:
    """
    扫引擎 bin 目录里的后端 DLL，判断 GGUF 解码器能走什么后端。

    返回 {'backend': 'vulkan'|'cuda'|'cpu', 'dirs': {相对路径: 后端}, 'dll_count': n}
    只看第一份能找到的目录即可（四份内容相同），但会把四份都扫一遍用于自检。
    """
    result: Dict[str, Any] = {'backend': 'cpu', 'dirs': {}, 'dll_count': 0, 'missing': []}
    found = set()
    for rel in _ENGINE_BIN_DIRS:
        d = _REPO_ROOT / rel
        if not d.is_dir():
            result['missing'].append(rel)
            continue
        try:
            names = [f.name.lower() for f in d.glob('*.dll')]
        except Exception:
            names = []
        result['dll_count'] = max(result['dll_count'], len(names))
        if any('vulkan' in n for n in names):
            kind = 'vulkan'
        elif any('cuda' in n for n in names):
            kind = 'cuda'
        elif any(('hip' in n) or ('rocm' in n) for n in names):
            kind = 'rocm'
        else:
            kind = 'cpu'
        result['dirs'][rel] = kind
        found.add(kind)
    # 只要有任意一份带 GPU 后端，就认为能上 GPU（四份理论上应一致）
    for prefer in ('cuda', 'vulkan', 'rocm'):
        if prefer in found:
            result['backend'] = prefer
            break
    return result


# ============================================================
#  3. 显卡清单（展示用，带缓存）
# ============================================================

def _is_child_process() -> bool:
    """worker 子进程里为 True（spawn 出来的进程 parent_process() 不是 None）。"""
    try:
        import multiprocessing
        return multiprocessing.parent_process() is not None
    except Exception:
        return False


def _run_wmi() -> List[Dict[str, Any]]:
    """用 WMI 枚举显卡。失败返回空列表（只影响展示，不影响决策）。"""
    ps = (
        "Get-CimInstance Win32_VideoController | ForEach-Object { "
        "$_.Name + '|' + $_.DriverVersion + '|' + $_.PNPDeviceID }"
    )
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
            capture_output=True, text=True, timeout=_WMI_TIMEOUT,
        )
    except Exception:
        return []
    gpus: List[Dict[str, Any]] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line or '|' not in line:
            continue
        parts = line.split('|')
        name = parts[0].strip()
        driver = parts[1].strip() if len(parts) > 1 else ''
        pnp = parts[2].strip().upper() if len(parts) > 2 else ''
        vendor = ''
        for vid, vname in _VENDORS.items():
            if f'VEN_{vid:04X}' in pnp:
                vendor = vname
                break
        low = name.lower()
        virtual = any(h in low for h in _VIRTUAL_HINTS) or (
            vendor == 'Microsoft' and 'basic' in low
        ) or not vendor and not pnp
        gpus.append({'name': name, 'vendor': vendor, 'driver': driver, 'virtual': virtual})
    return gpus


def _read_cache() -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _cache_fresh(data: Optional[Dict[str, Any]]) -> bool:
    if not data or not data.get('gpus'):
        return False
    stamp = data.get('updated_at') or ''
    try:
        age = (datetime.now() - datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S')).days
        return age < _CACHE_TTL_DAYS
    except Exception:
        return False


def list_gpus(refresh: bool = False) -> List[Dict[str, Any]]:
    """
    显卡清单。缓存命中直接返回；未命中且当前是主进程才去拉 WMI。
    worker 子进程不去拉 powershell（子进程可能被反复拉起，代价太高）。
    """
    cached = _read_cache()
    if not refresh and _cache_fresh(cached):
        return cached.get('gpus') or []
    if _is_child_process():
        return (cached or {}).get('gpus') or []
    gpus = _run_wmi()
    if gpus:
        _write_cache({'gpus': gpus})
    return gpus


def _write_cache(payload: Dict[str, Any]) -> None:
    """
    把内容并进缓存文件（保留原有字段，不整份覆盖）。

    用「临时文件 + os.replace」原子写：主进程与多个 worker 子进程都会写这个文件，
    直接覆盖有概率留下半个 JSON。读侧本来就能容错（解析失败当没有缓存），
    但没必要留这个失败面 —— 与 config_local.save_overrides 同一套做法。
    """
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = _read_cache() or {}
        data.update(payload)
        data['updated_at'] = f'{datetime.now():%Y-%m-%d %H:%M:%S}'
        tmp = _CACHE_PATH.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, _CACHE_PATH)
    except Exception:
        pass


# ============================================================
#  4. 决策与应用
# ============================================================

def normalize_provider(value: Any) -> str:
    """把用户配置里的 provider 写法归一成短名；不认识的值当 auto。"""
    if not isinstance(value, str):
        return 'auto'
    return _PROVIDER_ALIASES.get(value.strip().lower(), 'auto')


def decide(onnx_pref: Any = 'auto', llm_pref: Any = 'auto',
           providers: Optional[List[str]] = None,
           backend: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    算出实际要用的后端。返回:
        {'onnx_provider': 短名, 'llm_use_gpu': bool, 'onnx_source': '显式'|'自动',
         'llm_source': ..., 'available': [...], 'llama_backend': 'vulkan',
         'rejected': ['CUDAExecutionProvider', ...]}

    'rejected' 是"onnxruntime 说可用、但验活发现其实会回落 CPU"的那批。
    显式指定的后端如果验活失败，也会退回到自动选择，并在 onnx_source 里说明。
    """
    providers = available_providers() if providers is None else providers
    backend = llama_backend() if backend is None else backend

    pref = normalize_provider(onnx_pref)
    rejected = rejected_providers(providers)
    if pref == 'auto':
        onnx, onnx_src = best_onnx_provider(providers), '自动'
    else:
        full = next((f for f, s in _PROVIDER_ORDER if s == pref), None)
        if full and full in providers and verify_provider(full):
            onnx, onnx_src = pref, '显式'
        elif full is None:
            onnx, onnx_src = pref, '显式'      # CPU 或不在优先级表里的写法
        else:
            onnx = best_onnx_provider(providers)
            onnx_src = f'显式指定 {pref} 不可用，已回落'

    gpu_backend_ok = backend.get('backend') in ('vulkan', 'cuda', 'rocm')
    if isinstance(llm_pref, bool):
        llm, llm_src = llm_pref, '显式'
    elif isinstance(llm_pref, str) and llm_pref.strip().lower() in ('true', 'false'):
        llm, llm_src = llm_pref.strip().lower() == 'true', '显式'
    else:
        llm, llm_src = gpu_backend_ok, '自动'

    return {
        'onnx_provider': onnx,
        'llm_use_gpu': llm,
        'onnx_source': onnx_src,
        'llm_source': llm_src,
        'available': providers,
        'rejected': rejected,
        'llama_backend': backend.get('backend', 'cpu'),
    }


def _apply_to_args(args_obj, decision: Dict[str, Any]) -> List[str]:
    """把决策写到某个 Args 类上（只写它本来就有的属性）。"""
    applied = []
    if hasattr(args_obj, 'onnx_provider'):
        try:
            args_obj.onnx_provider = decision['onnx_provider']
            applied.append('onnx_provider')
        except Exception:
            pass
    if hasattr(args_obj, 'llm_use_gpu'):
        try:
            args_obj.llm_use_gpu = decision['llm_use_gpu']
            applied.append('llm_use_gpu')
        except Exception:
            pass
    return applied


def auto_configure(server_config=None, args_map: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    总入口：读 ServerConfig 上的三个开关 -> 检测 -> 写回各 Args 类 -> 汇报。

    参数传 None 时自己 import（config_server 在自己的模块体末尾调用本函数，
    此时那个模块已经执行完，属性都齐了）。
    返回决策字典，供调用方打日志；本函数不抛异常。
    """
    try:
        # 必须最先做：让 onnxruntime 找得到 pip 装的 CUDA 运行库。
        # 这一步早于引擎建会话，晚一步 CUDA 就会静默不生效。
        ensure_cuda_dlls()

        if server_config is None or args_map is None:
            import config_server as _cs
            server_config = server_config or _cs.ServerConfig
            if args_map is None:
                # 注意不含 ParaformerArgs：它走的是 sherpa_onnx 的 provider 参数，
                # 名字体系（cpu/cuda/directml）与 onnxruntime provider 不是一回事，
                # 没法靠 ort 的可用列表判断，所以不自动改它。
                args_map = {name: getattr(_cs, name)
                            for name in _GPU_TARGET_ARGS
                            if hasattr(_cs, name)}

        gpu_auto = bool(getattr(server_config, 'gpu_auto', True))
        onnx_pref = getattr(server_config, 'onnx_provider', 'auto')
        llm_pref = getattr(server_config, 'llm_use_gpu', 'auto')

        if not gpu_auto:
            # 关掉自动检测：只把显式写的值铺下去，auto 的保持类里的原值
            decision = {
                'onnx_provider': normalize_provider(onnx_pref) if normalize_provider(onnx_pref) != 'auto' else None,
                'llm_use_gpu': llm_pref if isinstance(llm_pref, bool) else None,
                'onnx_source': '显式' if normalize_provider(onnx_pref) != 'auto' else '未启用',
                'llm_source': '显式' if isinstance(llm_pref, bool) else '未启用',
                'available': [], 'llama_backend': '?', 'disabled': True,
            }
        else:
            decision = decide(onnx_pref, llm_pref)

        touched: Dict[str, List[str]] = {}
        for name, obj in (args_map or {}).items():
            # 显式配置优先：decision 里为 None 的项保持类里的原值
            partial = {k: v for k, v in decision.items() if v is not None}
            applied = _apply_to_args(obj, partial)
            if applied:
                touched[name] = applied

        decision['applied'] = touched
        decision['gpus'] = list_gpus()
        _write_cache({'decision': {k: v for k, v in decision.items() if k != 'gpus'}})
        global _LAST_DECISION
        _LAST_DECISION = decision
        return decision
    except Exception as e:
        return {'error': repr(e), 'onnx_provider': None, 'llm_use_gpu': None,
                'available': [], 'llama_backend': '?', 'gpus': [], 'applied': {}}


def last_decision() -> Optional[Dict[str, Any]]:
    """
    取本进程最近一次探测结果。

    存在的理由：探测发生在 config_server 的模块体里，那时候 server logger
    还没 setup（core/server/__init__.py 是先 import config_server，第 21 行才
    setup_logger）。所以探测只负责算和写盘，日志由 __init__ 在 setup 之后补打。
    """
    return _LAST_DECISION


def summary_line(decision: Dict[str, Any]) -> str:
    """一行人类可读的检测结论，给日志用。"""
    if decision.get('error'):
        return f'显卡检测失败（已回落默认值）: {decision["error"]}'

    real = [g for g in (decision.get('gpus') or []) if not g.get('virtual')]
    virt = [g for g in (decision.get('gpus') or []) if g.get('virtual')]
    names = '、'.join(f"{g['name']}" + (f"（{g['vendor']}）" if g.get('vendor') else '')
                      for g in real[:3]) or '未识别到物理显卡'
    if len(real) > 3:
        names += f' 等 {len(real)} 块'

    if decision.get('disabled'):
        return f'显卡自动选择已关闭（gpu_auto=false）。检测到: {names}'

    avail = decision.get('available') or []
    avail_txt = '/'.join(p.replace('ExecutionProvider', '') for p in avail) or '未安装 onnxruntime'
    llm_txt = 'GPU' if decision.get('llm_use_gpu') else 'CPU'
    line = (f"显卡: {names}；ONNX 编码器后端 = {decision.get('onnx_provider')}"
            f"（{decision.get('onnx_source')}，可用: {avail_txt}）；"
            f"GGUF 解码器 = {llm_txt}（{decision.get('llm_source')}，"
            f"llama.cpp 后端 = {decision.get('llama_backend')}）")
    rejected = decision.get('rejected') or []
    if rejected:
        names_r = '/'.join(p.replace('ExecutionProvider', '') for p in rejected)
        line += (f"；注意: {names_r} 虽然被 onnxruntime 报告为可用，但实际建会话时会"
                 f"静默回落到 CPU（依赖没装全或版本不匹配），已跳过")
    if virt:
        line += f"；已滤掉 {len(virt)} 个虚拟显示适配器"
    return line
