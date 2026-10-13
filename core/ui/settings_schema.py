# coding: utf-8
"""
设置面板的声明式字段表

这一份表被三个地方共享：
  1. 托盘里的 ⚙️ 设置面板（core/ui/settings_dialog.py）
  2. 独立的「CapsWriter设置.exe」（逃生通道，托盘不可用时用）
  3. 安装向导的高级初始配置页

所以**必须只依赖标准库**，不能 import config_client / core.*。

字段的 key 约定：
  - 普通 key（如 'paste'）        -> 写到 config_local.json 的 client 段
  - 'llm.<字段>'（如 'llm.api_key'）-> 写到 llm_roles[<当前选中角色>].<字段>
  - 'mic_device' 等              -> 也是 client 段（补丁新增键，见 config_local.EXTRA_CLIENT_DEFAULTS）

effect 三种取值：
  - 'hot'      保存后立即生效（Config 是类属性，运行时逐次现读）
  - 'restart'  需要重启客户端（启动时一次性快照，见 README 说明）
  - 'server'   需要重启服务端（配置在 worker 子进程里读）
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Sequence, Tuple

# ============================================================
#  可选值
# ============================================================

LANGUAGES: Tuple[Tuple[str, str], ...] = (
    ('自动检测', 'auto'),
    ('中文', 'chinese'),
    ('英文', 'english'),
    ('日文', 'japanese'),
    ('韩文', 'korean'),
    ('粤语', 'cantonese'),
)

TRADITIONAL_LOCALES: Tuple[Tuple[str, str], ...] = (
    ('标准繁体', 'zh-hant'),
    ('台湾繁体', 'zh-tw'),
    ('香港繁体', 'zh-hk'),
)

LOG_LEVELS: Tuple[Tuple[str, str], ...] = (
    ('DEBUG（排查问题用，很啰嗦）', 'DEBUG'),
    ('INFO（推荐）', 'INFO'),
    ('WARNING', 'WARNING'),
    ('ERROR', 'ERROR'),
    ('CRITICAL', 'CRITICAL'),
)

# ---- LLM 连接方式 ----
#
# 界面上**不再暴露"服务商"下拉**。原因：有了用户填的 Base URL 之后，
# provider 就只剩一个作用 —— 决定走哪套 SDK。我们统一用 'openai'，
# 也就是 "OpenAI 兼容接口"，DeepSeek / 智谱 / 月之暗面 / Ollama /
# LM Studio / 各类中转和本地代理全都走这一条路。
#
# 这样用户只需要填三样东西：Base URL、API Key、模型名。
#
# 注意 api_url **必须非空**：llm_client_pool 里 final_url = api_url or
# DEFAULT_API_URLS.get(provider)，留空会退回 OpenAI 官方地址。
# 见 core/client/settings_apply.py 里的兜底。
GENERIC_PROVIDER = 'openai'
DEFAULT_LLM_BASE_URL = 'https://api.deepseek.com/v1'

# 常见地址，写进提示里给用户抄
LLM_BASE_URL_EXAMPLES: Tuple[Tuple[str, str], ...] = (
    ('DeepSeek', 'https://api.deepseek.com/v1'),
    ('智谱 GLM', 'https://open.bigmodel.cn/api/paas/v4'),
    ('月之暗面 Kimi', 'https://api.moonshot.cn/v1'),
    ('本地代理（CC Switch）', 'http://127.0.0.1:15721/v1'),
    ('LM Studio', 'http://127.0.0.1:1234/v1'),
    ('Ollama', 'http://127.0.0.1:11434/v1'),
)

# 各家当前常见的模型名。**只是提示文字，不是下拉选项** ——
# 模型名更新极快（新版本、自建、中转站改名），给固定的下拉框只会让用户
# 以为自己必须从里面挑。这里是纯文本框，用户想填什么填什么。
LLM_MODELS: Tuple[str, ...] = (
    'deepseek-chat',
    'deepseek-reasoner',
    'glm-4-flash',
    'glm-4-plus',
    'moonshot-v1-8k',
    'gpt-4o-mini',
)

OUTPUT_MODES: Tuple[Tuple[str, str], ...] = (
    ('直接打字上屏', 'typing'),
    ('浮动窗显示', 'toast'),
)

# 键盘/鼠标按键名（来自 config_client.py 底部的说明注释）
SHORTCUT_KEYS: Tuple[str, ...] = (
    'caps_lock', 'num_lock', 'scroll_lock',
    'f1', 'f2', 'f3', 'f4', 'f5', 'f6', 'f7', 'f8', 'f9', 'f10', 'f11', 'f12',
    'f13', 'f14', 'f15', 'f16', 'f17', 'f18', 'f19', 'f20', 'f21', 'f22', 'f23', 'f24',
    'ctrl_l', 'ctrl_r', 'shift', 'shift_r', 'alt_l', 'alt_gr', 'cmd', 'cmd_r',
    'space', 'enter', 'tab', 'backspace', 'delete', 'insert',
    'home', 'end', 'page_up', 'page_down', 'esc', 'print_screen', 'pause', 'menu',
    'up', 'down', 'left', 'right',
    'x1', 'x2',
    'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm',
    'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z',
)

SHORTCUT_TYPES: Tuple[Tuple[str, str], ...] = (
    ('键盘', 'keyboard'),
    ('鼠标', 'mouse'),
)


# ============================================================
#  数据结构
# ============================================================

@dataclass(frozen=True)
class Field:
    """一个可配置项"""
    key: str
    label: str
    kind: str                       # bool|int|float|str|choice|multiline|secret|shortcut_list
    default: Any = None
    choices: Sequence[Tuple[str, str]] = ()
    hint: str = ''
    effect: str = 'hot'
    section: str = 'basic'          # basic 直接显示 / advanced 折叠到「高级」
    width: int = 0                  # 输入框宽度，0 = 自动
    stacked: bool = False           # True = 标签单独一行，输入框占满下一行
                                    # （长文本类字段用它，比如 URL 和 Key）


@dataclass(frozen=True)
class Group:
    """一组相关的配置项"""
    id: str
    title: str
    fields: Tuple[Field, ...] = ()
    note: str = ''                  # 组底部的说明文字
    # 面板分层。目标是"开箱即用"：用户打开面板第一眼只该看到
    # 真正需要他决定的东西，其余全部收进高级。
    #   common   -> 「常用」页（LLM 连接、麦克风、快捷键）
    #   advanced -> 「高级」页（有合理默认值、基本不用改的）
    #   about    -> 「关于」页（版本、日志、恢复默认）
    tier: str = 'common'


EFFECT_TEXT = {
    'hot': '✅ 保存即生效',
    'restart': '⚠️ 需重启客户端',
    'server': '⚠️ 需重启服务端',
}


# ============================================================
#  字段表
# ============================================================

GROUPS: Tuple[Group, ...] = (

    Group(
        id='llm',
        title='LLM 润色',
        note=('打开后，每次语音识别结果都会先交给大模型润色一遍'
              '（去口水词、修错别字、补标点），再上屏。\n'
              '只需要一个「OpenAI 兼容」的服务地址 —— DeepSeek、智谱、'
              '本地代理（CC Switch / LM Studio / Ollama）都可以。'),
        fields=(
            Field('llm_enabled', '启用 LLM 润色', 'bool', False, effect='hot',
                  hint='打开后识别结果会先交给大模型润色一遍再上屏'),
            Field('llm.api_url', 'Base URL', 'str', DEFAULT_LLM_BASE_URL,
                  effect='hot', stacked=True,
                  hint='要带 /v1。' + '　'.join(
                      f'{n}：{u}' for n, u in LLM_BASE_URL_EXAMPLES)),
            Field('llm.api_key', 'API Key', 'secret', '', effect='hot',
                  stacked=True,
                  hint='在服务商的开放平台申请。地址指向本机（127.0.0.1）时'
                       '一般不校验，留空会自动补个占位符'),
            # kind='str' 而不是 'choice'：模型名不给下拉框。各家命名花样太多、
            # 版本迭代又快，下拉框只会让用户以为"必须从这几个里选"。
            Field('llm.model', '模型', 'str', 'deepseek-chat', effect='hot',
                  hint='填服务商文档里的模型名，常见的有：'
                       + '、'.join(LLM_MODELS)),
        ),
    ),

    Group(
        id='shortcut',
        title='快捷键',
        note=('每一行是一个触发键，三列分别是：用哪个键、怎么触发、要不要启用。\n'
              '「怎么触发」是每一行各设各的 —— 比如键盘用「点一下」、'
              '鼠标侧键用「按住」，互不影响。列表里两行显示不一样是正常的。\n'
              '  • 按住说话，松开上屏 —— 适合一两句话。短按（不足阈值）不会触发录音\n'
              '  • 点一下开始 / 再点一下结束 —— 不用一直按着，适合长段口述。\n'
              '    注意：这种模式下这个键会被完全占用，不再触发它原本的功能'),
        fields=(
            # 这两项标 hot：core/client/settings_apply.py 的 _rebuild() 会
            # 直接重建 ShortcutManager，保存后立刻生效（正在录音时会拒绝并
            # 提示重启）。之前标成 restart，用户会被白叫去重启一次。
            Field('shortcuts', '快捷键列表', 'shortcut_list', (), effect='hot'),
            Field('threshold', '触发阈值（秒）', 'float', 0.3, effect='hot',
                  hint='按住超过这个时长才开始录音。太短容易误触，太长不跟手'),
        ),
    ),

    Group(
        id='output',
        title='输出行为',
        note='控制识别结果怎么落到你的光标处。',
        fields=(
            Field('paste', '用粘贴方式输出', 'bool', False,
                  hint='关掉是逐字模拟键盘输入；某些程序（如微信）只吃粘贴'),
            Field('restore_clip', '粘贴后恢复剪贴板', 'bool', True,
                  hint='仅在开启「用粘贴方式输出」时有意义'),
            Field('language', '识别语言', 'choice', 'auto', choices=LANGUAGES),
            Field('save_audio', '保存录音文件', 'bool', True,
                  hint='录音按日期归档到「年份/月份/assets」里'),
            Field('trash_punc', '末尾要去掉的标点', 'str', '，。,.',
                  hint='被当正则字符类用，不能包含 - ] ^ \\ 这几个字符'),
            Field('trash_punc_thresh', '短句强制去标点的字数', 'int', 8),
            Field('show_recording_overlay', '录音时显示波形浮层', 'bool', True,
                  hint='屏幕下方会显示一条跟着声音跳动的波形，并提示当前状态'),
            Field('traditional_convert', '转成繁体输出', 'bool', False),
            Field('traditional_locale', '繁体地区', 'choice', 'zh-hant',
                  choices=TRADITIONAL_LOCALES),
            Field('context', '提示词上下文', 'multiline', '',
                  hint='填人名、地名、专业术语等，辅助识别（例如「张三、CapsWriter、FastAPI」）'),
            Field('llm_stop_key', '中断 LLM 输出的键', 'str', 'esc',
                  effect='restart', section='advanced'),
        ),
        tier='advanced',
    ),

    Group(
        id='mic',
        title='麦克风',
        note='改完保存后音频流会自动重启，按住快捷键说一句话试试。',
        fields=(
            Field('mic_device', '输入设备', 'choice', '',
                  hint='按设备名保存（不是编号），换 USB 口也不会错位'),
            Field('mic_seg_duration', '录音分段长度（秒）', 'int', 60,
                  effect='restart', section='advanced'),
            Field('mic_seg_overlap', '录音分段重叠（秒）', 'int', 4,
                  effect='restart', section='advanced'),
        ),
    ),

    Group(
        id='file',
        title='文件转录',
        note='把音视频文件拖到客户端图标上，会生成字幕和文本。',
        fields=(
            Field('file_save_srt', '生成 .srt 字幕', 'bool', True),
            Field('file_save_txt', '生成 .txt 文本', 'bool', True),
            Field('file_save_json', '生成 .json（含时间戳）', 'bool', True),
            Field('file_save_merge', '生成 merge.txt（未切分长文本）', 'bool', False),
            Field('file_seg_duration', '转录分段长度（秒）', 'int', 60,
                  section='advanced'),
        ),
        tier='advanced',
    ),

    Group(
        id='hotword',
        title='热词',
        note=('热词表在安装目录的 hot.txt 里，一行一个，可以用 | 写别名。'
              '改完 3 秒内自动重载，不用重启。'),
        fields=(
            Field('hot', '启用热词替换', 'bool', True),
            Field('hot_thresh', '替换阈值', 'float', 0.85,
                  hint='相似度高于这个值就强制替换。调高更保守'),
            Field('hot_similar', '参考阈值', 'float', 0.6,
                  hint='相似度高于这个值的热词会作为上下文传给 LLM'),
            Field('hot_rule', '启用正则规则替换', 'bool', True,
                  hint='规则写在 hot-rule.txt 里'),
        ),
        tier='advanced',
    ),

    Group(
        id='advanced',
        title='日志与网络',
        note='这些几乎不用改。日志级别调成 DEBUG 会在 logs 目录里写很多内容。',
        fields=(
            Field('log_level', '日志级别', 'choice', 'DEBUG', choices=LOG_LEVELS,
                  hint='排查问题时调成 DEBUG；平时 INFO 就够了'),
            Field('enable_tray', '启用托盘图标', 'bool', True, effect='restart',
                  hint='关掉之后就没法从托盘打开这个面板了，慎重'),
            Field('addr', '服务端地址', 'str', '127.0.0.1', effect='restart'),
            Field('port', '服务端端口', 'str', '6016', effect='restart'),
            Field('udp_broadcast', 'UDP 广播识别结果', 'bool', False),
            Field('udp_control', 'UDP 远程控制录音', 'bool', False,
                  effect='restart'),
        ),
        tier='advanced',
    ),

    # 「关于」页没有字段，内容由 core/ui/settings_dialog.py 自己画：
    # 版本信息、日志路径、打开目录、恢复默认。
    # 「记录」页只有这一个字段 —— 列表本身由 core/ui/record_page.py 自己画
    # （历史记录是数据、不是配置项，声明式字段表表达不了）
    Group(
        id='records',
        title='保留策略',
        tier='records',
        fields=(
            Field('history_keep', '历史保留时长', kind='choice', default='forever',
                  effect='restart',
                  choices=(('永久保留', 'forever'), ('1 年', '365'), ('30 天', '30'),
                           ('7 天', '7'), ('24 小时', '1'), ('不保留任何记录', 'never')),
                  hint='超过这个时长的记录（连同原始录音）会在**下次启动时**自动清理；'
                       '听写结果与 LLM 对话记录都算在内。'),
        ),
        note='清理是真删，删了找不回来。默认「永久保留」——'
             '这一版不做云端同步，所有记录只在你自己的机器上。',
    ),
    Group(id='about', title='关于', tier='about'),
)

# 面板页签：常用 / 热词 / 关于。
#
# 「高级」那页（输出行为 / 文件转录 / 热词 / 日志与网络，共 26 项）用户反馈
# "里边的功能有点多余"，整个从面板上撤掉了。**下面的 Group 定义都还在**，
# 只是不再被渲染 —— 那些配置项照常生效（默认值在 config_client.py 里），
# 想调可以直接编辑根目录的 config_local.json；要调回界面，把
# ('advanced', '高级') 加回 TIERS 就行，一行的事。
#
# 「热词」页没有字段表，内容由 core/ui/settings_dialog.py 自己画
# （自动学到的词要能逐条增删改，声明式字段表表达不了）。
TIERS: Tuple[Tuple[str, str], ...] = (
    ('common', '常用'),
    ('hotwords', '热词'),
    ('records', '记录'),
    ('about', '关于'),
)


def groups_in_tier(tier: str) -> Tuple[Group, ...]:
    return tuple(g for g in GROUPS if g.tier == tier and g.fields)


# ============================================================
#  查询工具
# ============================================================

_ALL_FIELDS: Tuple[Field, ...] = tuple(
    f for g in GROUPS for f in g.fields
)

_BY_KEY = {f.key: f for f in _ALL_FIELDS}


def get_field(key: str) -> Field:
    return _BY_KEY[key]


def find_field(key: str) -> Optional[Field]:
    """安全版 get_field：表里没有就返回 None 而不是抛 KeyError。"""
    return _BY_KEY.get(key)


def all_fields() -> Tuple[Field, ...]:
    return _ALL_FIELDS


def is_role_field(key: str) -> bool:
    """是不是「当前选中角色」的字段（key 以 llm. 开头）"""
    return key.startswith('llm.')


def role_attr(key: str) -> str:
    """把 'llm.api_key' 拆成 'api_key'"""
    return key[4:]


def effect_of(key: str) -> str:
    f = _BY_KEY.get(key)
    return f.effect if f else 'hot'


def base_url_hint() -> str:
    """给界面用的"常见地址"提示文字。"""
    return '　'.join(f'{name}：{url}' for name, url in LLM_BASE_URL_EXAMPLES)


def is_placeholder_secret(value: Any) -> bool:
    """
    作者在 `LLM/*.py` 里留的是**脱敏过的假 key**（形如 sk-4c756****36fb）。
    原样显示出来会让人以为已经配好了，所以界面上当空处理。
    """
    return '*' in str(value or '')


PLACEHOLDER_LOCAL_KEY = 'sk-local'


def is_local_url(url: str) -> bool:
    """
    地址是不是指向本机。

    本机服务（CC Switch / LM Studio / Ollama / one-api 之类）基本都不校验
    API Key，但 **openai SDK 拿到空 api_key 会直接抛
    `OpenAIError: Missing credentials`** —— 所以这类情况要补一个占位符，
    否则用户会卡在"端口都对、地址也对，就是报错"上。
    """
    low = str(url or '').strip().lower()
    return any(h in low for h in ('127.0.0.1', 'localhost', '0.0.0.0', '[::1]'))
