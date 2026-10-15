# Speak More 开发指南

> 本仓库是 **CapsWriter-Offline**（上游，作者 HaujetZhao，MIT）的二次开发分支，
> 定位是「在上游之上加一层**装好就能用**的外壳」。**上游的识别内核功能代码一行未改** ——
> `merger/`、`protocol.py` 完全没动，`engines/` 只删过确认零引用的死文件（2026-10-15 删了 8 个共 787 行）；
> 改动集中在外壳层，每处都带 `[补丁]` 注释说明原因。
>
> 与上游的关系：合并基线是上游的 `84912d5`（2026-09-14），
> `git diff 84912d5..HEAD` 就是本分支的全部差异（30 多个上游文件被改、30 多个新文件，合计一万多行）。

## 核心设计 (Core Design)
**"快、准、稳、离线"**
- **离线 (Offline)**: 全本地模型 (ASR, 标点, LLM)，保护隐私。
- **C/S 架构**:
    - **Server**: 主进程处理 WebSocket，**独立子进程** (`multiprocessing.Process`) 运行 AI 模型，确保推理（CPU密集）不阻塞网络心跳。
    - **Client**: 轻量启动，负责全局快捷键监听、录音采集、UI 展示。
- **源代码开放**: 入口 [`start_server.py`](start_server.py) / [`start_client.py`](start_client.py) 为冻结入口；核心源码在 [`core/`](core/) 目录，发行版保留为源码供用户修改。
- **配置化**: [`config_client.py`](config_client.py) / [`config_server.py`](config_server.py) 及 `hot*.txt`、[`LLM/*.py`](LLM/) 位于根目录。
  **本分支的运行时配置不回写这些文件**：设置走 [`config_local.py`](config_local.py) 的
  `config_local.json` 覆盖层，优先级 = 源码默认 < 补丁默认(`PATCH_*`) < JSON。
- **版本**: 上游内核 v2.6；本分支外壳 **v0.2**（2026-10-15；v0.1 首发于 2026-10-13）
  发行版与说明见 <https://github.com/cloud666666666/speak-more/releases>

## 架构细节与流程 (Architecture & Workflows)

### 1. 识别全链路 (Recognition Flow)
- **采集**: Client 监听快捷键（默认 CapsLock 和 X2）。按下就开始收集录音chunk，超过 **0.3s (Threshold)** 不松则触发识别，**实时流式**通过 WebSocket 发送。
- **切片 (Slicing)**: Client 配置 `mic_seg_duration` (60s) 和 `mic_seg_overlap` (4s)。Server 仅基于时间切片，**禁用 VAD** 以保留完整上下文。
- **Server 处理**:
    - **双重结果**: 同时计算 `text` (简单文本拼接, Robust) 和 `text_accu` (基于 Token 时间戳去重, Precision)。
    - **拼接算法**: `text_accu`使用 **Token 时间戳去重** ([`core/server/merger/`](core/server/merger/))，`text` 使用 **模糊文本匹配**。
- **Client 后处理**:
    - **触发**: 用户**松开按键** -> Server 返回 IsFinal 结果。
    - **热词 (RAG)**: 基于 **音素 (Phoneme)** 的两阶段模糊检索，匹配 `hot.txt`（统一中英文热词）。
    - **规则替换**: `hot-rule.txt` 正则替换。
    - **LLM 润色**: 根据角色配置进行智能润色或回答。
    - **上屏**: 模拟键盘输入或 Toast 显示。

### 2. 客户端模式 (Client Modes)
- **听写 (Dictation)**: 默认模式。按住快捷键 -> 发送音频 -> 松开上屏。
- **转录 (Transcription)**: 拖入文件 -> `ffmpeg` 提取音频 -> 发送 Server -> 接收带时间戳结果 -> 生成 `.srt` / `.txt` / `.json`。

### 3. LLM Agent & 智能修正
- **实时监控 (Hot Reload)**: Client 启动 `watchdog` 文件监视器，实时响应 `hot*.txt` 和 `LLM/*.py` 的修改（3秒防抖）。
- **角色系统**: 模块化的 LLM 角色配置，支持多角色切换。
- **角色触发**: 检测识别结果前缀（如"翻译"、"助理"），匹配 [`LLM/`](LLM/) 下定义的角色。
- **Context 组装**（根据角色配置决定是否启用）:
    1.  **潜在热词**: RAG 检索 `hot.txt`（`enable_hotwords`）。
    2.  **选中文字**: 模拟 Ctrl+C 获取的鼠标选中文本（`enable_read_selection`）。
    3.  **对话历史**: 保留上下文历史记录（`enable_history`）。
    4.  **用户指令**: 当前语音输入内容。
- **输出模式**:
    - **typing**: 直接模拟键盘打字输出。
    - **toast**: 在 Toast 弹窗中显示，支持 Markdown 渲染。
- **UI**: 结果流式显示在 **Toast** (Tkinter 无边框置顶窗)，支持 Markdown 渲染。

### 4. 热词系统 (Hotword System)
- **服务器热词**: `hot-server.txt` 用于服务端热词增强。
- **统一文件**: `hot.txt` 统一管理中英文热词（基于音素匹配）。
- **两阶段检索**:
    1.  **FastRAG**: 倒排索引 + Numba JIT 快速粗筛（减少 90% 计算量）。
    2.  **AccuRAG**: 模糊音权重精确匹配（前后鼻音、平翘舌等）。
- **双阈值机制**:
    - `hot_thresh` (0.85): 高阈值用于实际替换。
    - `hot_similar` (0.6): 低阈值用于 LLM 上下文参考。
- **规则替换**: `hot-rule.txt` 支持正则表达式规则替换 (`pattern = replacement`)。

### 5. 历史归档 (Diary)
- **按日期归档**: `年份/月份/日期.md`。
- **音频**: 原始录音存入 `年份/月份/assets/`，Markdown 中自动生成 HTML 音频控件链接。

### 6. UDP 广播与控制
- **UDP 广播**: 识别结果可通过 UDP 广播到局域网（`udp_broadcast=True`）。
- **UDP 控制**: 支持通过 UDP 命令远程控制录音启停（`udp_control=True`）。

## 关键路径 (Key Paths)
- **服务端配置**: [`config_server.py`](config_server.py) — 模型选择、网络、格式化、对齐器。
- **客户端配置**: [`config_client.py`](config_client.py) — 快捷键、音频、热词、LLM、输出、UDP。
- **热词**:
    - [`hot.txt`](hot.txt) - 统一 RAG 音素匹配（中英文）
    - [`hot-rule.txt`](hot-rule.txt) - 规则替换
    - [`hot-server.txt`](hot-server.txt) - 服务端热词
- **LLM角色**: [`LLM/*.py`](LLM/) (根目录, 定义 Role/Prompt/Model)
    角色开关字段叫 **`enabled`**（不是 `process`/`match` —— 上游早期的文档里是那两个名字，早改了）
    - [`default.py`](LLM/default.py) - 默认角色（润色）。`enabled = False`：它不是"靠前缀触发"的角色，普通听写走的就是它
    - [`翻译.py`](LLM/翻译.py) - 翻译角色（lmstudio / local-model）
    - [`大助理.py`](LLM/大助理.py) - 大助理（deepseek / deepseek-v4-flash）
    - [`小助理.py`](LLM/小助理.py) - 小助理（lmstudio / local-model）
- **服务端核心**: [`core/server/`](core/server/)
    - [`app.py`](core/server/app.py) - `CapsWriterServer` 门面类
    - [`state.py`](core/server/state.py) - `ServerState` / `WorkerState` 共享状态
    - [`schema.py`](core/server/schema.py) - `Task` / `Result` / `RecognitionSession` 数据结构
    - [`connection/server_manager.py`](core/server/connection/server_manager.py) - `SocketManager` WebSocket 服务端生命周期
    - [`connection/ws_recv.py`](core/server/connection/ws_recv.py) - 音频接收与切片
    - [`connection/ws_send.py`](core/server/connection/ws_send.py) - 识别结果发送
    - [`worker/process_manager.py`](core/server/worker/process_manager.py) - `ProcessManager` 子进程管理
    - [`worker/worker.py`](core/server/worker/worker.py) - `RecognizerWorker` 推理循环
    - [`worker/model_loader.py`](core/server/worker/model_loader.py) - `ModelLoader` 模型加载
    - [`worker/pipeline.py`](core/server/worker/pipeline.py) - `TaskPipeline` 识别流水线
    - [`engines/`](core/server/engines/) - ASR 引擎实现（见下方模型支持）
    - [`merger/`](core/server/merger/) - 文本/Token 合并算法
    - [`formatter/text_formatter.py`](core/server/formatter/text_formatter.py) - `TextFormatter` 后处理
- **客户端核心**: [`core/client/`](core/client/)
    - [`app.py`](core/client/app.py) - `CapsWriterClient` 门面类
    - [`state.py`](core/client/state.py) - `ClientState` 共享状态
    - [`connection/websocket_manager.py`](core/client/connection/websocket_manager.py) - `WebSocketManager`
    - [`audio/`](core/client/audio/) - `AudioStreamManager` / `Recorder` / `FileManager`
        - ⚠️ `queue_in` 是**所有录音会话共用**的：松开快捷键那一瞬间，音频回调可能把最后一块推在 `finish` 后面，本会话读不到就走了 —— 那块音频会被**下一次录音**当成自己的开头，于是下一句里混进上一句的尾音。取消路径本来就排空，**正常结束路径也要排**（`recorder._drop_stale_blocks`，按时间戳只丢早于本次结束时刻的块 —— 直接原地过滤内部 deque，取出再放回会打乱顺序）
    - [`shortcut/`](core/client/shortcut/) - `ShortcutManager`（pynput）快捷键系统
        - ⚠️ **单击模式（`hold_mode=False`）会被按键自动重复打成筛子**：按住不放时 Windows 每 ~30ms 送一次 keydown，而"点一下开始／再点一下结束"的开关语义会把每一次重复都当成一次点击 → 录音 30ms 一开一停。挡法是 `task.pressed`（按下置位、keyup 清位，重复事件直接丢），实现在 [`event_handler.py`](core/client/shortcut/event_handler.py)
        - ⚠️ 被 `_check_emulating` / `_check_restoring` 放行的事件也要同步 `pressed` 状态，否则 keyup 被吞一次，那个键的单击模式就永久卡在"按过了"
    - [`output/result_processor.py`](core/client/output/result_processor.py) - `ResultProcessor` 后处理核心
        - ⚠️ **空的识别结果必须就地丢掉**（`_has_content` = `format_tools.has_content`）：作者原版会拿空字符串一路走到 LLM 和上屏 —— 空输入喂给润色角色，模型回一句"你好，请把需要校对的语音转写文本发给我"，**这句话会被当成识别结果打进用户正在写的文档里**（实测被塞了十几遍）。两道闸分工不同：**第一道**判「有没有实义字符」（原始文本，拦 ASR 直出的纯标点），**第二道**判「是不是空串」（后处理产物可以是合法的纯标点 —— hot-rule.txt 自带 `句号 → 。`，用 `_has_content` 判会把它误杀，踩过）
        - ⚠️ **`state.last_output_text` 是"最近一次成功上屏的文本"，不是"本次"**：LLM 失败（认证 / 超时 / 按 ESC / toast 建不出来）时它**不刷新**，拿 `last_output_text or text` 当本次结果 → 剪贴板被写成上一句话、学词也会拿上一句去回读比对（实测复现：logs/verify_stale_last_output_text.py）。本次结果要从 `llm_result.result` 取，取不到退回 `text`
        - ⚠️ 两处空结果早退都要先 `_drop_audio_file(task_id)`：录音文件在录音器里登记过，正常路径末尾才 pop 出来改名挂进日记，早退就漏成"随机后缀的孤儿 mp3 + 一直不释放的登记项"
    - [`output/text_output.py`](core/client/output/text_output.py) - `TextOutput` 上屏
    - [`hotword/`](core/client/hotword/) - 热词系统（Phoneme RAG + Rule + Rectification）
        - [`hot_file.py`](core/client/hotword/hot_file.py) - hot.txt 读写 + 体检(lint) + 词条级增删改 / CSV 导入
        - [`learn.py`](core/client/hotword/learn.py) - 改动 diff → 词条（`diff_words` / `save_pairs`）；手动纠正与自动学词共用
    - [`correction_learner.py`](core/client/correction_learner.py) - **从你的纠正里自动学词**：上屏后**盯着那个输入框看一会儿**（默认最多 20s，前 8s 每 0.25s 采样），一有变化就学；**输入框空了就用最后看到的内容结算**（对标 Typeless 的 Auto-added）。四道闸：同一个控件 / 原话还在 / 只认小幅替换 / **读音对得上**
        - ⚠️ **必须是连续采样，不能"隔几秒读一次"**：用户常常改完立刻回车发送，输入框当场就空了，隔几秒再读只能读到空白、改动永远学不到（实测丢过）。"空"这个信号**只在已经见过我们那句话之后**才算结束 —— 刚上屏那会儿输入框本来就是空的
        - ⚠️ 判"是不是听错了一个词"**不能靠改动长度**：difflib 会把"你把半句话重写了"拆成好几个**短**替换（实测 `周三→周五`、`接口文档→验收报告`、`给我→给他们`，每段才 2~4 字），长度和条数都拦不住。拦得住的是**读音** —— 中文比 `algo_phoneme.get_phoneme_seq` 的音素序列（去掉声调，`老汪→老王` 这种正是要认的），英文只认错一两个字母
    - [`history.py`](core/client/history.py) - 识别历史：解析日记归档（听写 / LLM 对话）、搜索、精确删除、保留策略
    - [`llm/`](core/client/llm/) - LLM 子系统（角色加载、上下文、API 调用）
        - ⚠️ **别把空内容发给模型**：空输入只会换回一句客套话，而那句客套话会进剪贴板 / LLM 归档，角色配成 typing 时**直接打进用户文档**。两个已知入口都要拦：识别结果为空（见上）、以及**角色前缀把整句吃掉之后**（用户单独说"翻译"/"小助理"，`detect_role` 剥完前缀 content 是空的 → 会被当成"没匹配角色"按普通听写处理）
        - [`llm_get_selection.py`](core/client/llm/llm_get_selection.py) - 取「选中的文字」。**无障碍接口优先**（不发按键、不碰剪贴板，还能区分"没选中"和"读不了"），读不了才退回模拟 Ctrl+C，**且终端窗口里绝不退回** —— 那里 Ctrl+C 是 SIGINT，会打断前台跑着的进程
    - [`manager/`](core/client/manager/) - `MicRunner` / `FileRunner` / `TrayManager`
    - [`transcribe/`](core/client/transcribe/) - `FileTranscriber` 文件转录
    - [`diary/diary_writer.py`](core/client/diary/diary_writer.py) - `DiaryWriter` 日记归档
    - [`udp/`](core/client/udp/) - UDP 广播与远程控制
    - [`server_lifecycle.py`](core/client/server_lifecycle.py) - **服务端托管**：拉起 / 退出收尾 / 看门狗 / 孤儿清理
    - [`model_precheck.py`](core/client/model_precheck.py) - 启动预检：缺模型 / 缺 DLL 时弹窗说清
    - [`standalone_settings.py`](core/client/standalone_settings.py) - `--settings` 独立设置模式（托盘坏掉时的逃生通道）
    - [`settings_apply.py`](core/client/settings_apply.py) - 设置落盘 + 热生效（`apply_result`，`app=None` 即独立模式）
        - ⚠️ **热词表是整份写回的**：设置面板一打开就固化了当时的 hot.txt 快照，保存时无条件写盘会把**面板开着期间由别处产生的改动**抹掉（自动学词就是那个"别处"）。所以 `_hotwords_if_changed()` 在原文一字未改时返回 None、跳过写盘（实测复现：logs/verify_hot_clobber.py）
- **共享工具**: [`core/tools/`](core/tools/) — ITN、格式化、信号处理、窗口检测、简繁转换
    - [`paths.py`](core/tools/paths.py) - `install_root()`：程序根目录。**别自己写 `Path(__file__).resolve()`** —— 打包时 `core/` 是目录联接，`resolve()` 会把它展开成源码树的路径（实测坑过四处：孤儿清理、打开日志目录、热词表、显卡缓存）
    - [`model_paths.py`](core/tools/model_paths.py) - 模型 / llama.cpp DLL 路径表 + 缺失检查（客户端与服务端共用同一份判断）
    - [`gpu_probe.py`](core/tools/gpu_probe.py) - 显卡探测 + 后端选择 + **验活**（onnxruntime 会静默回落到 CPU，只查 provider 列表不算数）
    - [`ui_reader.py`](core/tools/ui_reader.py) - 用 Windows UI Automation 读**焦点输入框里的文本**（`read_focused_text` / `focused_info`）和**选中的文字**（`read_selection`，三态：`None`=控件读不了 / `''`=确实没选中 / 非空=选中内容）。依赖 `comtypes`；只在本地读、只在刚上屏后的几秒读一次；控件不给读就返回 None，**不做任何降级**（不截图、不翻剪贴板、不模拟键盘）
        - ⚠️ `read_selection` 里有一道自检：某些控件会把**整篇文档**当成"选中内容"报上来（实测没获得焦点的记事本 RichEdit 就这样）—— 信了它就会把整篇文档塞进提示词。所以"选区 == 整框文本"时一律按"没选中"处理
- **UI 组件**: [`core/ui/`](core/ui/) — Toast、Tray、对话框、**设置面板**（[`settings_dialog.py`](core/ui/settings_dialog.py) + [`settings_handler.py`](core/ui/settings_handler.py) + [`settings_schema.py`](core/ui/settings_schema.py)，列表类页面自己画：热词/词典页、[`record_page.py`](core/ui/record_page.py) 记录页）、首启向导、按键捕获、录音浮层、图标统一（[`app_icon.py`](core/ui/app_icon.py)）
    - [`recording_overlay.py`](core/ui/recording_overlay.py) - 录音浮层：屏幕下方一条跟着声音起伏的**波形带**（对标 Typeless）。画法是 **Pillow 离屏渲染再贴到 Tk 画布**（Tk 没有抗锯齿，直接画曲线会毛糙；放大 3 倍画再缩回来当抗锯齿）。外观旋钮都在文件头部：`WAVE_GAIN`（波形长多高，0.6 = 现在这档）、`GLOW`（外发光）、`SAMPLES`（采样点）、`DB_FLOOR`/`DB_CEIL`（电平映射）。**Pillow 不在时有竖条兜底**；渲染失败只写 debug 日志、绝不影响录音
        - 出预览图：`logs/_verify_overlay_wave.py`（离屏渲染，会写 `logs/overlay_wave_preview.png` + `.gif`），调外观时先看它、别每次去录音试
    - ⚠️ **工作线程不能碰 Tk**（连 `after()` 都不行）：对话框打开时（主线程在 `wait_window`）会卡住或抛 `RuntimeError: main thread is not in main loop`，异常被 `except` 吞掉后表现就是"点了没反应"（测试麦克风 / 测试连接都中过招）。回传结果用 [`dialogs.py`](core/ui/dialogs.py) 的 `ThreadSlot` + `poll_slot`
- **协议**: [`core/protocol.py`](core/protocol.py) — `AudioMessage` + `RecognitionMessage`
- **日志**: `logs/client_latest.log` & `logs/server_latest.log`（排查问题唯一入口）

## 打包与部署 (Build)
- [`build.spec`](build.spec): 服务端 + 客户端 → `dist/Speak More/`（客户端产物叫 `Speak More.exe`，服务端 `start_server.exe`）。
- [`build-client.spec`](build-client.spec): 仅客户端。
- [`zip_release.py`](zip_release.py): **纯 Python 打发行 zip**（不依赖 7-Zip），产出 `release/SpeakMore-<日期>.zip`。
  默认不含模型（`--with-models` 可打一体包）；会自动排除日记归档、logs、本地配置。
- **策略**: 所有 Python 依赖放入 `internal/`。根目录仅保留配置文件、源码入口 ([`start_*.py`](start_server.py))、核心源码 ([`core/`](core/))、模型文件夹 ([`models/`](models/)) 和说明文档。
- **PyInstaller 6.0+**: 使用现代化打包配置，支持 CUDA provider 可选收集。
- ⚠️ **打包前必须退出所有正在运行的实例** —— exe 被占用时 PyInstaller 写不进 `dist/`，
  却照样打印"完成"、退出码 0，`dist/` 里留的是上一次的产物（踩过一次）。
- ⚠️ **不要排除 `internal/*.pyc`** —— noarchive 模式下整个运行时就是松散的 .pyc（近 5000 个），
  排掉之后 exe 会报 `Failed to import encodings module` 起不来。
- ⚠️ PyInstaller 按 **exe 自己所在的目录**找 `internal/`，所以 `start_server.exe` 挪不进子目录。
- 客户端比上游多一个依赖 **comtypes**（UI Automation，见 [`ui_reader.py`](core/tools/ui_reader.py)），
  两个 spec 的 `hiddenimports` 都已列上。**`comtypes.gen` 是运行时生成的**（`GetModule()` 把
  `UIAutomationCore.dll` 的接口编译成 .py 写进那个包），所以别把它写进 hiddenimports ——
  需要的是别把 comtypes 压进 archive（客户端本来就是 `noarchive=True`），另外安装目录得可写。
- ⚠️ **llama.cpp 的绑定与 DLL：三个 GGUF 引擎共用 `core/server/engines/llama/`，不是各自的 `inference/bin/`**。
  机理：三个 `inference/__init__.py` 都写 `try: from ...llama import llama`，包属性一旦绑上，
  后面 `from . import llama` 拿到的是共享那份；运行时日志会打「初始化 llama.cpp，跳转至：…\engines\llama\bin」。
  实测：`logs/_probe_engine_llama_bind.py`（绑定归属）、`logs/_probe_llama_abi.py` vs `_probe_llama_shared_abi.py`（新旧绑定的 ABI）。
  **各引擎自带的 `inference/llama.py` 是旧版**（把 `llama_context_params` 读成 136 字节、真实 160 → `n_threads=0`、`rope_freq_base=nan`），
  只在那条共享 import 失败时才会用到 —— 别去"修"它，也别把 DLL 装到 `inference/bin/`。
  `zip_release.py` 据此：**发** `engines/llama/`（共享绑定 + 一份 DLL）、**不发**三个 `inference/bin/` 的 DLL 副本（省 ~190MB）。
  2026-10-15 之前这里是反的，v0.1 发行包因此跑的正是那份错位的旧绑定（`logs/_verify_zip_rules.py` 现在会守住这条）。

## 模型支持 (Models)

### ASR 引擎

| 引擎类型 | 类名 | 文件 | 能力 | 说明 |
|---------|------|------|------|------|
| `paraformer` | `ParaformerEngine` | [`engines/paraformer_onnx/`](core/server/engines/paraformer_onnx/) | ASR + TIMESTAMPS | 通过 `sherpa_onnx.OfflineRecognizer`，准确率高 |
| `sensevoice` | `SenseVoiceEngine` | [`engines/sensevoice_onnx/`](core/server/engines/sensevoice_onnx/) | ASR + PUNC + HOTWORDS + TIMESTAMPS | 自有 ONNX 推理，多语言（中英日韩粤） |
| `fun_asr_nano` | `FunASREngine` | [`engines/fun_asr_gguf/`](core/server/engines/fun_asr_gguf/) | ASR + PUNC + HOTWORDS + TIMESTAMPS | GGUF LLM 解码器 + ONNX 编码器/CTC，最准 |
| `qwen_asr` | `QwenASREngine` | [`engines/qwen_asr_gguf/`](core/server/engines/qwen_asr_gguf/) | ASR + PUNC | GGUF 版 Qwen3-ASR 模型 |

### 辅助模型
- **Punct-CT-Transformer**: 标点模型（`CTTransformerPuncEngine`），引擎无 PUNC 能力时自动加载。
- **QwenForceAligner**: 对齐器（`ManagedAlignerProxy` 延迟加载+闲置卸载），用于文件转录时间戳对齐。

### 引擎能力检测
引擎通过 `EngineCapabilities` 标志位声明能力（`ASR` / `PUNC` / `TIMESTAMPS` / `STREAMING` / `HOTWORDS`）。`ModelLoader` 在加载时智能补丁：若引擎缺少 `PUNC` 则外挂标点模型，若缺少 `TIMESTAMPS` 则外挂对齐器。

## LLM 提供商支持 (LLM Providers)
- **Ollama**: 本地部署（默认）。
- **LMStudio**: 本地 OpenAI 兼容 API。
- **OpenAI**: GPT 系列。
- **DeepSeek**: deepseek 系列。
- **Moonshot**: 月之暗面。
- **Zhipu**: 智谱 AI。
- **Volcengine**: 火山引擎。
- **Cerebras**: Cerebras。

## 数据流 (Data Flow)
```
[Microphone] -> sounddevice callback -> asyncio.Queue
   |
   v  (ShortcutManager 检测按键)
[AudioStreamManager] 开始录音 -> WebSocketManager 发送 AudioMessage (base64 chunks)
   |
   v  (WebSocket, 子协议 "binary")
[Server: SocketManager] -> ws_recv -> AudioCache 切片 -> Task -> multiprocessing.Queue
   |
   v
[Worker 子进程: RecognizerWorker]
   |-- TaskPipeline: 音频预处理 -> ASR 解码 -> 文本合并 -> 格式化
   |-- 输出两路结果: text (简单合并) + text_accu (时间戳去重)
   |
   v
[Server: ws_send] -> RecognitionMessage -> WebSocket -> Client
   |
   v
[Client: ResultProcessor]
   |-- 音素热词纠正 (FastRAG + AccuRAG)
   |-- 正则规则替换 (hot-rule.txt)
   |-- LLM 角色检测 -> 上下文组装 -> API 调用 -> 流式输出
   |-- TextOutput 上屏 (type/paste) 或 Toast 显示
   |-- DiaryWriter 日记归档
   |-- UDP 广播识别结果
```

## 用户偏好 (User Preferences)
- **语言**: 中文 (Chinese)，总结、Plan、WalkThrough、注释都要用中文。
- **环境**: 用项目自带的 uv 虚拟环境 —— `.venv/Scripts/python.exe`（Python 3.14），或 `uv run python ...`。
  （旧说明里的 `conda activate c` 已不存在：上游 2026-09 改用 uv 管理环境。）
  所有的临时 Python 代码要先写到临时脚本文件，再运行，而不要直接用命令行跑代码。临时脚本用完不要删（默认放 `logs/`，那里被 gitignore 挡着）。

