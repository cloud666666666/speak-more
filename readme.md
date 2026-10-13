# Speak More

> 在 **[CapsWriter-Offline](https://github.com/HaujetZhao/CapsWriter-Offline)**（作者 [HaujetZhao](https://github.com/HaujetZhao)，MIT）
> 之上加的一层**「装好就能用」的外壳**：图形设置面板、首次启动向导、一个托盘图标，
> 以及一堆"让新手不用碰配置文件"的细节。**识别内核一行未改。**

---

## 下载就能用

**[→ 下载最新发行包](https://github.com/cloud666666666/speak-more/releases)**

1. 解压 `SpeakMore-*.zip`
2. **双击 `Speak More.exe`**
   （同目录的 `start_server.exe` 不用管 —— 客户端会自动拉起它、退出时收掉、崩了自动重启）
3. 第一次启动会弹**向导**：选麦克风（带 3 秒试录）→ 填 LLM 接口（可跳过）→ 设快捷键

**系统要求**：Windows 10 / 11（64 位）

### 还需要自己下载模型

发行包**不含模型**（2GB 级，而且不同引擎体积差很多，留给你选）。放到 `models/` 下即可 ——
**缺文件时客户端启动会直接弹窗告诉你缺哪个、该放哪个目录、去哪里下**，不会像上游那样按了没反应。

默认引擎是 Qwen3-ASR，模型在上游的模型发布页获取：
<https://github.com/HaujetZhao/CapsWriter-Offline/releases/tag/models>

> 想省 1.5GB 可以换 SenseVoice（413MB），改 `config_local.json` 的 `server.model_type` 即可，
> 详见 `docs/识别语言如何配置.md`。

---

## 和上游的区别

| | 上游 | 这一版 |
|---|---|---|
| 改配置 | 手改 `config_client.py` / `LLM/default.py` | 托盘 → ⚙️ **设置面板**（常用 / 热词 / 关于） |
| 首次使用 | 自己看文档配 | **首次启动向导**：选麦克风（带 3 秒试录）→ 填 API → 选快捷键 |
| 快捷键 | 手改配置里的键名 | **点一下按钮，按下你想用的键**（鼠标侧键也能收） |
| 托盘 | 客户端 + 服务端**两个图标** | 一个图标；服务端由客户端托管（实测退出时 0.3 秒收掉整条进程树，零残留） |
| 启动 | 依次双击 `start_server.exe`、`start_client.exe` | **只双击 `Speak More.exe` 一个** |
| 启动自检 | 缺模型时静默退出 | **弹窗说明**缺哪个文件、放哪、去哪下；顺带检查 llama.cpp 运行库 |
| 显卡加速 | 手改 provider 配置 | **自动探测 + 验活**（不验活的话 onnxruntime 会静默回落到 CPU） |
| 托盘坏掉时 | 没有任何图形入口 | `Speak More.exe --settings` **独立设置模式** |
| 历史记录 | 按天写进 md 文件，只能自己去翻 | **「记录」页**：听写 / 对话分类、搜索、逐条复制 / 打回光标 / 放原始录音 / 删除，保留时长可选 |
| 热词 / 词典 | 手动编辑 `hot.txt` | 「热词」页可视化词条管理 + **CSV 导入** + **从你的纠正里自动学词** |
| 图标 / 其他 | 各处默认图标不统一 | 图标统一、结果自动进剪贴板、录音波形浮层、开机自启一键开关 |

**关于改动范围**：运行时配置**不回写上游源码** —— 所有设置走 `config_local.json` 覆盖层
（见 `config_local.py` 的文件头说明），`LLM/default.py` 一个字节没动。
源码层面改了 22 个上游文件、`+720/−158` 行，其中**识别内核（`core/server/engines/`、
`core/server/merger/`、`core/protocol.py`）一行未改**；每处改动都带 `[补丁]` 注释说明原因。

---

## 从源码运行

```bash
git clone https://github.com/cloud666666666/speak-more speak-more
cd speak-more
uv sync                      # 或 pip install -r requirements-client.txt requirements-server.txt
python start_client.py
```

还需要两样上游要求的东西（仓库里不带）：

- **llama.cpp 的 DLL**（`ggml.dll` / `ggml-base.dll` / `llama.dll` 加后端 DLL）：
  每个引擎从**自己的** `inference/bin` 加载，中间没有回退路径 ——
  - 默认引擎（Qwen3-ASR）：铺到 `core/server/engines/qwen_asr_gguf/inference/bin/`
  - 用 Fun-ASR-Nano：铺到 `core/server/engines/fun_asr_gguf/inference/bin/`
  - 要文件转录（需要时间戳对齐）：再铺一份到
    `core/server/engines/force_aligner_gguf/inference/bin/`

  版本与下载地址见各目录里的 `llama.cpp二进制下载到这里.txt`（本仓库用 b10621）。
  （`core/server/engines/llama/bin/` 是历史遗留目录，现在的引擎已经不用它了。）
- **模型**：放到 `models/` 下，结构见 `config_server.py` 的 `ModelPaths`。

### 打包发行版

```bash
uv sync --group build
uv run pyinstaller build.spec        # → dist/Speak More/
uv run python zip_release.py         # → release/SpeakMore-<日期>.zip（纯 Python，不需要 7-Zip）
```

> ⚠️ **打包前必须先退出所有正在运行的实例** —— exe 被占用时 PyInstaller 写不进 `dist/`，
> 却照样打印"完成"、退出码还是 0，`dist/` 里留下的是上一次的产物（踩过一次）。

---

## 出问题时

| 症状 | 怎么办 |
|---|---|
| 按住快捷键没反应 | 托盘 → ⚙️ 设置 → **关于**页看自检：模型、推理库、推理后端都会显示 |
| 启动时弹窗说缺文件 | 按弹窗提示放文件（缺的是模型或 llama.cpp 运行库） |
| 托盘图标不见了 | 用下面的**独立设置模式**改配置；托盘需要控制台窗口，用 pythonw / 重定向输出启动会丢掉它 |
| 想强制用 CPU | `config_local.json` 的 `server` 段写 `{"gpu_auto": false, "onnx_provider": "cpu"}` |
| 要排查细节 | `logs/client_latest.log`、`logs/server_latest.log`（托盘 → 设置 → 关于 → 打开日志文件夹） |

**独立设置模式**（不启动客户端、不连服务端，只改配置，保存后下次启动生效）：

```bash
Speak More.exe --settings            # 打包版
python start_client.py --settings    # 源码
```

---

## 许可

上游是 MIT（作者 HaujetZhao），本仓库的改动同样以 MIT 发布，`LICENSE` 保留原作者署名。
上游的完整说明、原理文档与 FAQ 见
[CapsWriter-Offline](https://github.com/HaujetZhao/CapsWriter-Offline)
以及本仓库 `docs/` 目录。
