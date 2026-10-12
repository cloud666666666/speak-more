# 显卡加速与推理后端

> 这一版（Speak More）会自动检测显卡、优先把推理放到显卡上跑，并且**验证后端是否真的生效**
> ——因为 onnxruntime 在加速库装不全时会**静默回落到 CPU**，不验证的话你永远以为自己用上了显卡。

相关代码：`core/tools/gpu_probe.py`（探测与选择）、`config_server.py` 末尾的补丁块（触发时机）、
`core/server/__init__.py`（打日志）、设置面板「关于」页（显示结果）。

---

## 一、两个推理后端，各管一段

| 部件 | 后端 | 说明 |
|---|---|---|
| ONNX 编码器（音频→特征） | onnxruntime 的 provider | 有 CPU / DML / CUDA / TensorRT 可选，**本页主要讲这个** |
| GGUF 解码器（特征→文字） | llama.cpp 的 Vulkan | 上游的 bin 目录里就是 Vulkan 版 DLL，默认已在显卡上跑 |

也就是说：换 provider 影响的是编码器那一段；解码器一直走 Vulkan。

---

## 二、实测数据（RTX 5070 Laptop 8GB，Qwen3-ASR 1.7B）

编码器耗时，3 次取中位、`temperature=0`：

| 音频长度 | CPU | DML | CUDA（前后端都在 GPU） |
|---|---|---|---|
| 1.7s | 50.8ms | 23.5ms（2.2x） | **8.8ms（5.7x）** |
| 5.6s | 107.5ms | 41.6ms（2.6x） | **30.6ms（4.2x）** |
| 8.9s | 154.1ms | 41.5ms（3.7x） | **25.3ms（7.5x）** |
| 16.9s | 294.5ms | — | **45.8ms（6.4x）** |
| 25.9s | 437.0ms | — | **49.4ms（8.8x）** |

端到端（编码器 + 提示词 + GGUF 解码）在 DML 下：1.7s 音频 103ms → **72ms**；8.9s 音频 342ms → **201ms**。

**三种后端的识别文本逐字一致**——没有精度差异。

---

## 三、为什么默认是 DML 而不是 CUDA

| | DML | CUDA |
|---|---|---|
| 依赖体积 | **68MB** | 约 **2.5GB**（ORT-gpu + cuDNN + cuBLAS + cuFFT + cuRAND） |
| 硬件 | NVIDIA / AMD / Intel 通吃 | 仅 NVIDIA |
| 安装 | pip 装完即可 | 要 CUDA 12 运行时 + cuDNN 9，版本必须对得上 |
| 性能 | 比 CPU 快 2.2–3.7x | 比 DML 再快 1.4–2.7x |

DML 是"零门槛、大部分收益"；CUDA 是"高门槛、再快一截"。所以默认给 DML，CUDA 留给愿意折腾的人。

---

## 四、想用 CUDA：实测可用的组合

> 下面的版本组合是 2026-10 在 Python 3.14 + ORT 上逐个试出来的，**照抄可复现**。

```bash
# 注意 ONNX Runtime 的版本：1.31 要 CUDA 13，而 PyPI 上 nvidia-cublas-cu13 /
# nvidia-cuda-runtime-cu13 是 0MB 的空占位包，装不起来。要退到 CUDA 12 的 1.24.x
uv pip install "onnxruntime-gpu==1.24.1" \
    "nvidia-cudnn-cu12==9.8.0.87" \
    nvidia-cublas-cu12 nvidia-cufft-cu12 nvidia-curand-cu12 nvidia-cuda-runtime-cu12
```

两个坑，都踩过：

1. **cuDNN 版本必须对**。装 9.27 时 ORT 会在建会话时报 `CUDNN Frontend ... CUDNN_BACKEND_API_FAILED`，
   然后**静默回落到 CPU**——不报错、不中断，只是慢。9.8.0.87 实测可用。
2. **必须在建会话前调用 `ort.preload_dlls()`**。pip 装的 CUDA 运行库在
   `site-packages/nvidia/*/bin`，不在 PATH 上；不 preload 的话 CUDA **完全不生效**且无提示。
   本项目的 `gpu_probe.ensure_cuda_dlls()` 已经在引擎加载前替你调了。

装好之后重启服务端即可，无需改任何配置——探测会自动选中 CUDA。

---

## 五、怎么确认到底用没用上显卡

三种方式，任选：

1. **设置面板 → 关于页**：会显示一行「硬件：… 推理后端：ONNX 编码器 DML（自动）…」
2. **日志**：`logs/server_latest.log` 里搜「显卡:」，每次启动都会写一行（主进程与识别子进程各一条）
3. **`logs/gpu_probe.json`**：机器可读的完整探测结果（显卡清单、是否虚拟、决策、被拒绝的 provider）

如果日志里出现这样的提示，说明某个后端**装了但没用起来**：

```
注意: CUDA 虽然被 onnxruntime 报告为可用，但实际建会话时会静默回落到 CPU
（依赖没装全或版本不匹配），已跳过
```

---

## 六、想强制用 CPU（排障或省电）

在 `config_local.json` 的 `server` 段写：

```json
{
  "server": {
    "gpu_auto": false,
    "onnx_provider": "cpu"
  }
}
```

其他可选项：
- `"gpu_auto": true` + `"onnx_provider": "auto"`（默认）：自动挑最优并验活
- `"onnx_provider": "dml"` / `"cuda"` / `"trt"`：显式指定；**指定的后端验活失败时会自动回落并在日志里说明**
- `"llm_use_gpu": false`：让 GGUF 解码器也走 CPU（显存紧张时用）
- 改完需要重启服务端（覆盖层在进程启动时读取）

---

## 七、已知限制

- **Paraformer 引擎不受本机制影响**：它走 sherpa_onnx 自己的 provider 参数，名字体系不同，无法用 onnxruntime 的可用列表判断，所以不自动改。
- **双显卡机器**：llama.cpp 的 Vulkan 后端能同时看到独显和集显（本机就看到了 2 个设备），
  理论上存在选错的可能。要钉死设备可在启动环境里设 `GGML_VK_VISIBLE_DEVICES=0`（索引以日志里
  `ggml_vulkan: N = <名字>` 为准）。
- **`dml_pad_to`（默认 30 秒）没动**：DML 路径会把后端输入固定填充到该长度换取固定形状，
  实测短音频改成 10 能再快约 10ms，但超过 10 秒的音频会回落动态形状、慢一倍。
  权衡下来保持上游的 30。真要调，改 `Qwen3ASRGGUFArgs.dml_pad_to`。
- **验活有代价**：每个进程启动时多约 0.35s（要真建一次小会话）。这是"不被静默回落骗到"的成本，没有做缓存——缓存过期反而更容易骗人。
