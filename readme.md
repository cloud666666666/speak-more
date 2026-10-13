# Speak More

> 这是 **[CapsWriter-Offline](https://github.com/HaujetZhao/CapsWriter-Offline)**（作者
> [HaujetZhao](https://github.com/HaujetZhao)，MIT）的一个**二次开发分支**。
> 语音识别内核、模型、热词机制、协议全部来自上游，我们只加了一层
> **「装好就能用」的外壳** —— 图形设置面板、首次启动向导、统一的托盘与应用图标、
> 以及按一下键就能改的快捷键。

## 和上游的区别

上游功能很全，但配置全靠手改 `config.py` / `LLM/*.py`。这一版把最常用的那几件事
搬进了图形界面：**运行时的配置从不回写作者的源码** —— 所有设置走
`config_local.json` 覆盖层（见 `config_local.py` 的文件头说明），
`LLM/default.py` 更是一个字节没动。

源码层面只做了必要的最小改动：**识别内核一行未改**（`core/server/engines/`、
`core/server/merger/`、`core/protocol.py` —— 这些地方一个字节都没动）。
其余对上游既有文件的改动集中在客户端外壳（托盘、设置、快捷键、启动器、打包清单），
每处都带 `[补丁]` 注释说明为什么改，累计 22 个文件、`+720/−158` 行。

| | 上游 | 这一版 |
|---|---|---|
| 改配置 | 手改 `config_client.py` / `LLM/default.py` | 托盘 → ⚙️ **设置面板**（常用 / 热词 / 关于） |
| 首次使用 | 自己看文档配 | **首次启动向导**：选麦克风（带 3 秒试录）→ 填 API → 选快捷键 |
| 快捷键 | 手改配置里的键名 | **点一下按钮，按下你想用的键**（鼠标侧键也能收） |
| 托盘 | 客户端 + 服务端**两个图标** | 一个图标；服务端由客户端自动拉起、退出自动收掉、崩了自动重启 |
| 启动 | 依次双击 `start_server.exe`、`start_client.exe` | **只双击 `Speak More.exe` 一个**（服务端不用管，它自动起停） |
| 图标 | 各处的默认图标不统一 | 托盘 / 标题栏 / 任务栏 / 黑窗口统一成 Speak More |
| 识别结果 | 只上屏 | 上屏的同时**自动进剪贴板** |
| 录音时 | 无反馈 | 屏幕下方一条跟着声音跳的**波形浮层** |
| LLM 提示词 | 纠错只能依据热词表 | 重写过，**专有名词直接交给大模型**（自测：改前 4/10、改后 7/7） |
| 开机自启 | 自己弄 | 「关于」页一键开关（带图标的快捷方式，放在「启动」文件夹里） |

> 表里那个「自测：改前 4/10、改后 7/7」是开发时手工评测的结果，仓库里没有附复现
> 脚本 —— 不当成权威数据，欢迎拿自己的语料验证。

**关于热词表**：上游的热词表要用户自己维护，这一版用下来它真正的价值是
**「把大模型不认识的那些词告诉它」**（同事名、项目代号、行业黑话），而不是
替代大模型做替换 —— 公共名词（宇树科技、PyTorch 这类）好的提示词本来就能改对。
所以「热词」页做成了一个**原文编辑框 + 体检提示**，你只需要往里放自己的词。

上游原有的功能**一个都没删**：文件转录、日记归档、UDP 广播、多角色 LLM、
热词正则规则等等都在，只是那些"基本不用动"的配置项收进了代码默认值，
要调可以直接编辑 `config_local.json`。

## 安装

**拿到打包发行包**的话：解压后**只双击 `Speak More.exe` 一个文件**即可。
目录里还有一个 `start_server.exe`，那是服务端本体，由客户端自动拉起、退出时自动收掉、
崩了自动重启 —— 你不需要（也不应该）手动去点它。

**从源码跑**（目前仓库主线是这个形态）：

```bash
git clone https://github.com/cloud666666666/speak-more speak-more
cd speak-more
uv sync              # 或 pip install -r 依赖
python start_client.py
```

还需要两样上游要求的东西（仓库里不带）：

- **llama.cpp 的 DLL**（`ggml.dll` / `ggml-base.dll` / `llama.dll` 加后端 DLL）：
  每个引擎从**自己的** `inference/bin` 加载，中间没有回退路径 ——
  - 用默认引擎（Qwen3-ASR）：铺到 `core/server/engines/qwen_asr_gguf/inference/bin/`
  - 用 Fun-ASR-Nano：铺到 `core/server/engines/fun_asr_gguf/inference/bin/`
  - 要文件转录（需要时间戳对齐）：再铺一份到
    `core/server/engines/force_aligner_gguf/inference/bin/`

  （`core/server/engines/llama/bin/` 是历史遗留目录，现在的引擎已经不用它了）
  版本与下载地址见各目录里的 `llama.cpp二进制下载到这里.txt`（本仓库用 b10621）

- **模型**：放到 `models/` 下，结构见 `config_server.py` 的 `ModelPaths`。
  缺哪个文件，**客户端启动时会直接弹窗告诉你**（缺哪个文件、该放哪、去哪下）

**托盘起不来时**（用 pythonw、重定向输出、或某些启动器启动会丢掉控制台，
而托盘需要它）：用**独立设置模式**改配置 —— 不启动客户端、不连服务端：

```bash
Speak More.exe --settings        # 打包版
python start_client.py --settings  # 源码
```

**自己打包**：

```bash
uv sync --group build
uv run pyinstaller build.spec
```

⚠️ **打包前必须先退出所有正在运行的实例** —— exe 被占用时 PyInstaller 写不进
`dist/`，但它**照样打印"完成"、退出码还是 0**，`dist/` 里留下的是上一次的产物
（这个坑踩过一次，产物日期不对才发现）。

## 许可

上游是 MIT，这一版同样是 MIT，`LICENSE` 保留原作者署名。
本仓库内的改动同样以 MIT 发布。

---

# 以下是上游 CapsWriter-Offline 的原始说明

---

# CapsWriter-Offline

![demo](assets/demo.png)

> **按住 CapsLock 说话，松开就上屏。就这么简单。**

**CapsWriter-Offline** 是一个专为 Windows 打造的**完全离线**语音输入工具。

## ✨ 核心特性

-   **语音输入**：按住 `CapsLock键` 或 `鼠标侧键X2` 说话，松开即输入，超低延迟，默认去除末尾逗句号。支持对讲机模式和单击录音模式。
-   **文件转录**：音视频文件往客户端 exe 一丢，字幕 (`.srt`)、文本 (`.txt`)、时间戳 (`.json`) 统统都有。
-   **数字 ITN**：自动将「十五六个」转为「15~16个」，支持各种复杂数字格式。
-   **热词替换**：在 `hot.txt` 记下偏僻词，通过音素模糊匹配，相似度大于阈值则强制替换。
-   **正则替换**：在 `hot-rule.txt` 用正则或简单等号规则，精准强制替换。
-   **LLM 角色**：预置了润色、小助理等角色，当识别结果的开头匹配任一角色名字时，将交由该角色处理。
-   **托盘菜单**：右键托盘图标即可添加热词、复制结果、清除LLM记忆。
-   **C/S 架构**：服务端与客户端分离，虽然 Win7 老电脑跑不了服务端模型，但最少能用客户端输入。
-   **日记归档**：按日期保存你的每一句语音及其识别结果。
-   **录音保存**：所有语音均保存为本地音频文件，隐私安全，永不丢失。

**CapsWriter-Offline** 的精髓在于：**完全离线**（不受网络限制）、**响应极快**、**高准确率** 且 **高度自定义**。我追求的是一种「如臂使指」的流畅感，让它成为一个专属的一体化输入利器。无需安装，一个U盘就能带走，随插随用，保密电脑也能用。

以下为支持的模型：

| 引擎名 | 准确性 | 速度 | 格式 | 显卡加速 |
|------|-------|------|------|---------|
| Paraformer | ★★★☆☆ | ★★★★★ | ONNX | ❌ |
| SenseVoice-Small | ★★★☆☆ | ★★★★★ | ONNX | ✅ |
| Fun-ASR-Nano | ★★★★☆ | ★★★★☆ | ONNX + GGUF | ✅ |
| Qwen3-ASR | ★★★★★ | ★★★☆☆ | ONNX + GGUF | ✅ |


性能参考（20s 音频转录延迟）：

| 模型 | CPU U9-285H | GPU RTX5050 |
|------|------------|------------|
| Paraformer | 0.6s | - |
| SenseVoice-Small | 0.6s | 0.15s |
| Fun-ASR-Nano | 2.0s | 0.5s |
| Qwen3-ASR-1.7B | 4.0s | 1.0s |

详细功能说明请参考 [`docs/`](docs/) 目录：
- [环境依赖安装说明](docs/环境依赖安装说明.md) — VC++ 运行库、FFmpeg 安装
- [热词功能如何使用](docs/热词功能如何使用.md) — 热词替换、规则替换、自定义短语
- [角色功能如何使用](docs/角色功能如何使用.md) — LLM 角色配置、输出模式、创建新角色
- [识别语言如何配置](docs/识别语言如何配置.md) — 各引擎语言支持范围与配置方法
- [文件转录功能如何使用](docs/文件转录功能如何使用.md) — 拖拽转字幕、时间戳对齐
- [显卡加速的若干问题](docs/显卡加速的若干问题.md) — DirectML、Vulkan 加速配置
- [模型下载的若干问题](docs/模型下载的若干问题.md) — 引擎选择、模型下载、目录结构
- [常见问题](docs/常见问题.md) — FAQ
- [更新日志](docs/CHANGELOG.md) 


## 💻 平台支持

目前**仅能保证在 Windows 10/11 (64位) 下完美运行**。

- **Linux**：暂无环境进行测试和打包，无法保证兼容性。
- **MacOS**：由于底层的 `keyboard` 库已放弃支持 MacOS，且系统限制极多，暂时无法支持。

[LazyTyper](https://lazytyper.com/) 和 [闪电说](https://shandianshuo.cn/) 也是很优秀的作品，都有离线引擎，都支持 Windows Linux 与 MacOS，并都有漂亮的图形化页面，推荐使用。

CapsWriter 的特别之处在于追求：

- 无感输入
- 完全离线，不受网络约束
- 低延迟，尽量做到硬件极限的最快速度
- 高度自定义的热词系统


## 🎬 快速开始

1.  **准备环境**：确保安装了 [VC++ 运行库](https://learn.microsoft.com/zh-cn/cpp/windows/latest-supported-vc-redist)。若要使用文件转录功能，还需安装 [ffmpeg](https://ffmpeg.org/download.html) 并确保其在系统 PATH 中。
2.  **下载解压**：下载 [Latest Release](https://github.com/HaujetZhao/CapsWriter-Offline/releases/latest) 里的软件本体，再到 [Models Release](https://github.com/HaujetZhao/CapsWriter-Offline/releases/tag/models) 下载模型压缩包，将模型解压，放入 `models` 文件夹中对应模型的文件夹里。
3.  **启动服务**：双击 `start_server.exe`，**它会自动最小化到托盘菜单**。
4.  **启动听写**：双击 `start_client.exe`，**它会自动最小化到托盘菜单**。
5.  **开始录音**：按住 `CapsLock键` 或 `鼠标侧键X2` 就可以说话了！


## ⚙️ 个性化配置

所有的设置都在根目录的 `config_server.py` 和 `config_client.py` 里，可直接编辑。


## 🛠️ 常见问题


**Q: 为什么按了没反应？**  
A: 请确认 `start_client.exe` 的黑窗口还在运行。若想在管理员权限运行的程序中输入，也需以管理员权限运行客户端。

**Q: 为什么识别结果没字？**  
A: 到 `年/月/assets` 文件夹中检查录音文件，看是不是没有录到音；听听录音效果，是不是麦克风太差，建议使用桌面 USB 麦克风；检查麦克风权限。

**Q: 想要隐藏黑窗口？**  
A: 点击托盘菜单即可隐藏黑窗口。

**Q: 如何开机启动？**  
A: `Win+R` 输入 `shell:startup` 打开启动文件夹，将服务端、客户端的快捷方式放进去即可。

更多问题请参阅 [docs/常见问题.md](docs/常见问题.md)。


## 🚀 我的其他优质项目推荐

| 项目名称 | 说明 | 体验地址 |
| :--- | :--- | :--- |
| [**IME_Indicator**](https://github.com/HaujetZhao/IME_Indicator) | Windows 输入法中英状态指示器 | [下载即用](https://github.com/HaujetZhao/IME_Indicator/releases/latest/download/IME-Indicator.exe) |
| [**Rust-Tray**](https://github.com/HaujetZhao/Rust-Tray) | 将控制台最小化到托盘图标的工具 | [下载即用](https://github.com/HaujetZhao/Rust-Tray/releases/latest/download/Tray.exe) |
| [**Gallery-Viewer**](https://github.com/HaujetZhao/Gallery-Viewer-HTML) | 网页端图库查看器，纯 HTML 实现 | [点击即用](https://haujetzhao.github.io/Gallery-Viewer-HTML/) |
| [**全景图片查看器**](https://github.com/HaujetZhao/Panorama-Viewer-HTML) | 单个网页实现全景照片、视频查看 | [点击即用](https://haujetzhao.github.io/Panorama-Viewer-HTML/) |
| [**图标生成器**](https://github.com/HaujetZhao/Font-Awesome-Icon-Generator-HTML) | 使用 Font-Awesome 生成网站 Icon | [点击即用](https://haujetzhao.github.io/Font-Awesome-Icon-Generator-HTML/) |
| [**五笔编码反查**](https://github.com/HaujetZhao/wubi86-revert-query) | 86 五笔编码在线反查 | [点击即用](https://haujetzhao.github.io/wubi86-revert-query/) |
| [**快捷键映射图**](https://github.com/HaujetZhao/ShortcutMapper_Chinese) | 可视化、交互式的快捷键映射图 (中文版) | [点击即用](https://haujetzhao.github.io/ShortcutMapper_Chinese/) |


## ❤️ 致谢

本项目基于以下优秀的开源项目：

-   [Sherpa-ONNX](https://github.com/k2-fsa/sherpa-onnx)
-   [FunASR](https://github.com/alibaba-damo-academy/FunASR)

感谢 Google Antigravity、Anthropic Claude、GLM、DeepSeek，如果不是这些编程助手，许多功能（例如基于音素的热词检索算法）我是无力实现的。

特别感谢那些慷慨解囊的捐助者，你们的捐助让我用在了购买这些优质的 AI 编程助手服务，并最终将这些成果反馈到了软件的更新里。


如果觉得好用，欢迎点个 Star 或者打赏支持：


![sponsor](assets/sponsor.jpg)	
