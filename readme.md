# Speak More

> 按住一个键说话，松开就上屏 —— **全程离线**的语音输入工具。
>
> 它是 **[CapsWriter-Offline](https://github.com/HaujetZhao/CapsWriter-Offline)**（作者 [HaujetZhao](https://github.com/HaujetZhao)，MIT）
> 的二次开发分支：**识别内核一行未改**，在它之上加了一层「装好就能用」的外壳 ——
> 图形设置面板、首次启动向导、一个托盘图标、会自己管自己的服务端，以及一堆
> "让新手不用碰配置文件"的细节。

---

## 下载就能用

**[→ 下载最新发行包](https://github.com/cloud666666666/speak-more/releases)**

1. 解压 `SpeakMore-*.zip`（挑个**自己有写权限**的目录，别放 `Program Files`）
2. **双击 `Speak More.exe`**
   （同目录的 `start_server.exe` 不用管 —— 客户端会自动拉起它、退出时收掉、崩了自动重启）
3. 第一次启动会弹**向导**：选麦克风（带 3 秒试录）→ 填 LLM 接口（可跳过）→ 设快捷键

**系统要求**：Windows 10 / 11（64 位）。

### 模型要自己下（发行包不带）

模型有 2GB 级、不同引擎体积差很多，所以留给你选。放到 `models/` 下即可 ——
**缺文件时客户端启动会直接弹窗**告诉你缺哪个、该放哪个目录、去哪里下，
不会像上游那样按了没反应。

默认引擎是 Qwen3-ASR，模型在上游的模型发布页获取：
<https://github.com/HaujetZhao/CapsWriter-Offline/releases/tag/models>

> 想省 1.5GB 可以换 SenseVoice（413MB），改 `config_local.json` 的 `server.model_type`，
> 详见 [`docs/模型下载的若干问题.md`](docs/模型下载的若干问题.md)。

---

## 它能干什么

| 功能 | 说明 |
|---|---|
| **按住说话** | 长按快捷键录音、松开上屏；也可以设成**点一下开始 / 再点一下结束**。键盘任意键、鼠标侧键都能当快捷键 |
| **麦克风按需占用** | 不用听写时自动关掉采集流（默认空闲 60 秒），任务栏的「麦克风正在使用」图标不再常亮；按下快捷键会自动重新打开 |
| **实时的波形浮层** | 屏幕下方一条跟着声音起伏的波形带（对标 Typeless），让你知道"它在听"、麦克风有没有声音。**不抢焦点、鼠标穿透** |
| **LLM 润色 / 角色** | 识别结果先过大模型：去口水词、修错别字、补标点。还能配**角色** —— 说「翻译 xxx」走翻译角色、说「小助理 xxx」走助理角色，每个角色独立的提示词、模型、输出方式 |
| **热词 + 词典** | 你的人名、公司名、项目代号写进词典，识别时按**读音**模糊匹配纠正（念错也能纠回来）。词典可以在设置面板里逐条增删、**从 CSV 导入** |
| **自动学词** | 你打完字之后自己改过的词，它会盯着那个输入框看一会儿、**一有变化就收进词典** —— 改完立刻回车发送也没关系，用你发送前的最后样子结算（对标 Typeless 的 Auto-added） |
| **记录页** | 听写与 LLM 对话的历史归档，能搜索、能逐条复制 / 打回光标 / 放原始录音 / 删除；保留时长可选 |
| **文件转录** | 拖入音视频文件 → 出 `.srt` / `.txt` / `.json` |
| **显卡加速** | 自动探测并**验活**推理后端（不验活的话 onnxruntime 会静默回落到 CPU，看着开了其实没开） |
| **全程离线** | 语音识别、标点、对齐全在本机跑。只有你**自己配的** LLM 接口（可选）会把文字发到你自己指定的服务 |

---

## 和上游的区别

| | 上游 | 这一版 |
|---|---|---|
| 改配置 | 手改 `config_client.py` / `LLM/default.py` | 托盘 → ⚙️ **设置面板**（常用 / 热词 / 记录 / 关于） |
| 首次使用 | 自己看文档配 | **首次启动向导**：选麦克风（带 3 秒试录）→ 填 API → 选快捷键 |
| 快捷键 | 手改配置里的键名 | **点一下按钮，按下你想用的键**（鼠标侧键也能收） |
| 托盘 | 客户端 + 服务端**两个图标** | 一个图标；服务端由客户端托管（实测退出时 0.3 秒收掉整条进程树，零残留） |
| 启动 | 依次双击 `start_server.exe`、`start_client.exe` | **只双击 `Speak More.exe` 一个** |
| 启动自检 | 缺模型时静默退出 | **弹窗说明**缺哪个文件、放哪、去哪下；顺带检查 llama.cpp 运行库 |
| 显卡加速 | 手改 provider 配置 | **自动探测 + 验活** |
| 托盘坏掉时 | 没有任何图形入口 | `Speak More.exe --settings` **独立设置模式** |
| 历史记录 | 按天写进 md 文件，只能自己去翻 | **「记录」页**：分类、搜索、复制 / 打回光标 / 放录音 / 删除，保留时长可选 |
| 热词 / 词典 | 手动编辑 `hot.txt` | 「热词」页可视化词条管理 + CSV 导入 + **从你的纠正里自动学词** |
| 录音反馈 | 控制台里一个看不见的动画 | **屏幕下方的波形浮层** |
| 图标 / 其他 | 各处默认图标不统一 | 图标统一、结果自动进剪贴板、开机自启一键开关 |

### 关于改动范围

运行时配置**不回写上游源码** —— 所有设置走 `config_local.json` 覆盖层
（见 [`config_local.py`](config_local.py) 的文件头说明），`LLM/*.py` 里的角色文件你可以随便改、
升级不会覆盖。相对上游基线（`84912d5`）：**30 多个上游文件被改、30 多个新文件**，合计一万多行；**识别内核（`core/server/engines/`、
`core/server/merger/`、`core/protocol.py`）的**功能代码一行未改** ——
`merger/`、`protocol.py` 完全没动，`engines/` 只删掉了 8 个确认没人引用的死文件（旧实现、死门面、废弃的 worker 残留，共 787 行）。
每处外壳改动都带 `[补丁]` 注释说明原因。

### 关于「自动学词」读了什么

它用 Windows 无障碍接口（UI Automation）读的，所以**能读到别的窗口里的文字** —— 边界写死在这儿：

- 只在你**刚打完字之后那一小会儿**（默认最多 20 秒）盯着那个输入框，**不是常驻监控**
- 焦点跑到别的窗口就不看；连续跑掉好几次就收工
- 只跟你本机对比，**不联网**（这个产品全程离线，代码里没有任何上传路径）
- 只在**读音对得上**时才认（识别错字就是这个特征）；你补字、删句、重写半句话都不会被记
- 开关在设置面板「热词」页，取消勾选就完全不读；词典里学到的词可以逐条删

同样的接口也用来读「选中的文字」（翻译角色要用）：**不发按键、不碰你的剪贴板**，
读不到才退回模拟 Ctrl+C，**且终端窗口里绝不退回**（那里 Ctrl+C 是 SIGINT，会打断你跑着的程序）。

---

## 从源码运行

```bash
git clone https://github.com/cloud666666666/speak-more speak-more
cd speak-more
uv sync                     # 或 pip install -r requirements-client.txt requirements-server.txt
python start_client.py
```

> 客户端比上游多一个依赖 **comtypes**（读焦点输入框 / 选区用的，走 Windows UI Automation）。
> 没装的话只有「自动学词」和「读选中文字」不生效，其余照常。

还需要两样上游要求的东西（仓库里不带）：

- **llama.cpp 的 DLL**（`ggml.dll` / `ggml-base.dll` / `llama.dll` 加后端 DLL）：
  三个 GGUF 引擎（Qwen3-ASR / Fun-ASR-Nano / 对齐器）**共用同一份**，铺到这一个目录就行：

  ```
  core/server/engines/llama/bin/
  ```

  > 别被各引擎目录下那个 `inference/bin/` 骗了 —— 那是**旧版说明**的产物。
  > 实测（`logs/_probe_engine_llama_bind.py`）：三个引擎的 `inference/__init__.py`
  > 都写了 `from ...llama import llama`，实际绑上的是共享那份；运行时日志里能看到
  > 「初始化 llama.cpp，跳转至：…\core\server\engines\llama\bin」。
  > 往 `inference/bin` 装既不会生效，那份旧绑定还和新版 DLL 对不上
  > （会把 `llama_context_params` 读成 136 字节、真实 160 → `n_threads=0`、`rope_freq_base=nan`）。

  版本与下载地址见该目录里的 `llama.cpp二进制下载到这里.txt`（本仓库用 b10621）。
- **模型**：放到 `models/` 下，结构见 [`core/tools/model_paths.py`](core/tools/model_paths.py)
  （启动预检就是照它核对的）。

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
| 说了话但什么都没上屏 | 多半是**麦克风没声音**了（静音 / 输入音量被拉到 0 / 选错设备）。录满 1.5 秒却一个字都没识别出来时会弹一条「🔇 没听到声音」；也可以去设置 → **测试麦克风**看电平 |
| 启动时弹窗说缺文件 | 按弹窗提示放文件（缺的是模型或 llama.cpp 运行库） |
| 托盘图标不见了 | 用下面的**独立设置模式**改配置；托盘需要控制台窗口，用 pythonw / 重定向输出启动会丢掉它 |
| 想强制用 CPU | `config_local.json` 的 `server` 段写 `{"gpu_auto": false, "onnx_provider": "cpu"}` |
| 说「翻译」没反应 | 单说一个角色词等于没内容 —— 它不会去问模型，而是当普通听写把这两个字打出来 |
| 任务栏麦克风图标一直亮 | 说明采集流一直开着。默认「不用时自动释放麦克风」是开的：安静 `mic_idle_close_seconds`（默认 60）秒后自动关流、图标熄灭，按下快捷键自动重开。想彻底常开（上游行为）就在设置 → 麦克风页关掉这一项 |
| 要排查细节 | `logs/client_latest.log`、`logs/server_latest.log`（托盘 → 设置 → 关于 → 打开日志文件夹） |

**独立设置模式**（不启动客户端、不连服务端，只改配置，保存后下次启动生效）：

```bash
Speak More.exe --settings            # 打包版
python start_client.py --settings    # 源码
```

更多细节见 [`docs/`](docs/)：`常见问题.md`、`热词功能如何使用.md`、`角色功能如何使用.md`、
`文件转录功能如何使用.md`、`识别语言如何配置.md`、`显卡加速与推理后端（Speak More）.md` 等。

---

## 许可

上游是 MIT（作者 HaujetZhao），本仓库的改动同样以 MIT 发布，[`LICENSE`](LICENSE) 保留原作者署名。
上游的完整说明、原理文档与 FAQ 见
[CapsWriter-Offline](https://github.com/HaujetZhao/CapsWriter-Offline)。
