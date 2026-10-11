# coding: utf-8
"""
设置面板的「保存后立即生效」逻辑

职责边界（重要）：
    本文件**是唯一需要认识 app / Config 的地方之一**（另一个是 settings_handler.py）。
    settings_dialog.py / settings_schema.py / config_local.py 都完全不依赖作者代码。
    作者升级时要改的就是这两个文件。

生效分三级（这是本项目最容易让人误判的地方）：
  A. 每次调用现读 —— setattr 完就生效，什么都不用做。
     paste / language / context / trash_punc* / llm_enabled / file_save_* ...
  B. 导入时快照 —— log_level。core/logger.py:63-71 的 Logger.setup 支持重入更新，
     所以再调一次 setup_logger 就热了。
  C. 构造时快照 —— 必须重建对象：
     shortcuts/threshold（app.py:77 一次性构造）、hot_thresh/hot_similar
     （app.py:59-63 传进 HotwordManager，再进 PhonemeCorrector）、mic_device
     （stream.py 启动时读）。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional

from core.ui import settings_schema as S

try:
    from core.client import logger
except Exception:                                  # pragma: no cover
    import logging
    logger = logging.getLogger('client')


@dataclass
class ApplyReport:
    """保存结果。给用户看的都是中文短句。"""
    applied: List[str] = dc_field(default_factory=list)   # ✅ 已立即生效
    restart: List[str] = dc_field(default_factory=list)   # ⚠️ 需重启
    errors: List[str] = dc_field(default_factory=list)    # ❌ 出错

    def is_empty(self) -> bool:
        return not (self.applied or self.restart or self.errors)


# ============================================================
#  对外入口
# ============================================================

def _sanitize_roles(roles: Dict[str, Any],
                    before_roles: Optional[Dict[str, Any]] = None) -> None:
    """
    Base URL 兜底。

    现在界面上不再暴露"服务商"下拉，所有角色都走 OpenAI 兼容接口，
    **Base URL 是唯一的连接信息**。它一旦为空，
    core/client/llm/llm_client_pool.py 会退回到 provider 的默认地址
    （对默认角色来说是 DeepSeek 官方），报一个让人摸不着头脑的错。

    只处理**用户这次真的动过**的角色：像「翻译」「小助理」那种用
    LM Studio（有内置默认地址 http://127.0.0.1:1234/v1）的角色，
    我们不该去动它，否则等于把人家指向了 DeepSeek。
    """
    try:
        from core.ui import settings_schema as S
    except Exception:
        return

    before_roles = before_roles or {}

    for name, vals in roles.items():
        if not isinstance(vals, dict) or not vals:
            continue

        before_vals = before_roles.get(name) or {}
        if before_vals and not any(before_vals.get(k) != v for k, v in vals.items()):
            continue                     # 这一项没改动，跳过

        url = str(vals.get('api_url') or '').strip()
        if not url:
            url = S.DEFAULT_LLM_BASE_URL
            vals['api_url'] = url
            logger.info(f'角色「{name}」没填 Base URL，已自动补为 {url}')

        # 本机服务一般不校验 Key，但 openai SDK 拿到空 api_key 会直接抛
        # OpenAIError: Missing credentials —— 补个占位符，
        # 免得用户卡在"地址都对、就是报错"上。
        if not str(vals.get('api_key') or '').strip() and S.is_local_url(url):
            vals['api_key'] = S.PLACEHOLDER_LOCAL_KEY
            logger.info(f'角色「{name}」指向本机服务但没填 Key，'
                        f'已补占位符 {S.PLACEHOLDER_LOCAL_KEY}')


def _apply_hotwords(app: Any, hot_in: Any, report: ApplyReport) -> bool:
    """
    落盘「热词」页的改动，并让它立刻生效。

    `hot_in` 是 `{'manual': '<hot.txt 原文>'}`。为 None 或没有 'manual'
    表示对话框没建那一页 -> **一个字都不动**（否则会把用户的热词清空）。
    """
    if not isinstance(hot_in, dict):
        return False

    manual = hot_in.get('manual')
    if not isinstance(manual, str):
        return False

    try:
        from core.client.hotword import hot_file
    except Exception as e:
        report.errors.append(f'热词模块不可用: {e}')
        return False

    try:
        base = app.hotword.files.get('hot')
    except Exception:
        base = None

    if not hot_file.write_hot(manual, base):
        report.errors.append('保存热词表失败')
        return False

    logger.info(f'热词表已由设置面板改写（{len(manual)} 字符）')

    # 立刻生效。文件监视器也会在 3 秒内重载，但用户刚点了保存就该马上能用。
    try:
        app.hotword.load_all()
        report.applied.append('热词表')
    except Exception as e:
        report.errors.append(f'热词重载失败: {e}')

    return True


def apply_result(app: Any, data: Dict[str, Any],
                 before: Optional[Dict[str, Any]] = None) -> ApplyReport:
    """
    把设置面板返回的结果落盘 + 生效。

    Args:
        app:    CapsWriterClient 实例
        data:   DialogResult.data —— {'client': {...}, 'llm_roles': {...}, 'reset': True}
        before: 打开面板时的快照，用来判断"哪些项真的被改了"

    Returns:
        ApplyReport
    """
    report = ApplyReport()

    # 先记一笔收到的内容。排查"设置怎么不生效"时，
    # 一眼就能看出到底是没保存、还是被当成「恢复默认」了。
    logger.info(
        f'设置提交：client={len(data.get("client") or {})} 项，'
        f'llm_roles={len(data.get("llm_roles") or {})} 个角色，'
        f'reset={bool(data.get("reset"))}，'
        f'wizard_done={bool(data.get("wizard_done"))}')

    try:
        import config_local
    except Exception as e:
        report.errors.append(f'配置层不可用: {e}')
        return report

    # ---------- 恢复默认 ----------
    if data.get('reset'):
        try:
            config_local.reset_overrides()
            report.restart.append('所有设置已恢复默认')
            logger.info('设置面板：已恢复默认（config_local.json 已改名备份）')
        except Exception as e:
            report.errors.append(f'恢复默认失败: {e}')
        return report

    client_in: Dict[str, Any] = dict(data.get('client') or {})
    roles_in: Dict[str, Any] = dict(data.get('llm_roles') or {})

    # 总开关要同时驱动「默认」角色的 enabled。
    # core/client/llm/llm_role_detector.py 里，只有 role_config.enabled 为 True
    # 的角色才会被选中 —— 只开总开关、不开角色，表现就是"看着开了但没效果"，
    # 属于最难排查的那类问题（用户会以为是模型或 key 的问题）。
    if 'llm_enabled' in client_in:
        enabled = bool(client_in['llm_enabled'])
        roles_in.setdefault('默认', {})['enabled'] = enabled

    # Base URL 兜底，必须在落盘之前
    _sanitize_roles(roles_in, (before or {}).get('llm_roles'))

    # ---------- 1) 先落盘 ----------
    patch: Dict[str, Any] = {}
    if client_in or roles_in:
        patch['client'] = client_in
        patch['llm_roles'] = roles_in
        try:
            config_local.update_overrides(patch)
        except Exception as e:
            report.errors.append(f'写入 config_local.json 失败: {e}')
            return report

    # ---------- 2) 刷内存（B 类 / A 类在这里就生效了） ----------
    try:
        from config_client import ClientConfig as Config
        config_local.apply_client_overrides(Config)
    except Exception as e:
        report.errors.append(f'套用配置失败: {e}')
        return report

    # ---------- 2.5) 热词表 ----------
    #
    # ⚠️ 必须放在下面"算出哪些真的变了"那个提前 return **之前**。
    # 热词不属于 client 也不属于 llm_roles，用户只改了热词的话上面两个
    # diff 都是空的，会走到 `if not changed_client and not changed_roles:
    # return` —— 也就是"改了热词却什么都没发生"，而且日志上看着一切正常。
    _apply_hotwords(app, data.get('hotwords'), report)

    # ---------- 3) 算出哪些真的变了 ----------
    before_client = dict((before or {}).get('client') or {})
    before_roles = dict((before or {}).get('llm_roles') or {})
    changed_client = {k for k, v in client_in.items() if before_client.get(k) != v}
    changed_roles = {
        name for name, vals in roles_in.items()
        if any(before_roles.get(name, {}).get(k) != v for k, v in vals.items())
    }

    if not changed_client and not changed_roles:
        # 别在这里就说"没有改动" —— 热词那份可能在 2.5) 已经生效了。
        if not report.applied:
            report.applied.append('没有改动')
        return report

    # ---------- 4) C 类：重建对象 ----------
    _rebuild(app, changed_client, report)

    # ---------- 5) LLM ----------
    if changed_roles or 'llm_enabled' in changed_client:
        try:
            app.llm.reload_roles()      # 内置 client_pool.clear()
            report.applied.append('LLM 配置')
        except Exception as e:
            report.errors.append(f'LLM 配置生效失败: {e}')
            logger.error(f'LLM 配置生效失败: {e}', exc_info=True)

    # ---------- 6) 剩下的按 schema 声明分类 ----------
    handled = {'shortcuts', 'threshold', 'mic_device',
               'hot_thresh', 'hot_similar', 'log_level', 'llm_enabled'}
    for key in sorted(changed_client - handled):
        if key not in {f.key for f in S.all_fields()}:
            continue
        field = S.find_field(key)
        if field is None:
            continue
        if field.effect == 'restart':
            report.restart.append(field.label)
        elif field.effect == 'server':
            report.restart.append(f'{field.label}（服务端）')
        else:
            report.applied.append(field.label)

    return report


def _rebuild(app: Any, changed: set, report: ApplyReport) -> None:
    """重建「构造时快照」的那些组件。每一项都独立 try/except，互不牵连。"""

    # ---- 快捷键：ShortcutManager 的线程池 stop 后就废了，只能 new 一个 ----
    if changed & {'shortcuts', 'threshold'}:
        if getattr(app.state, 'recording', False):
            report.restart.append('快捷键（正在录音，请稍后重启）')
        else:
            try:
                from config_client import ClientConfig as Config
                from core.client.shortcut.shortcut_manager import ShortcutManager
                from core.client.shortcut.shortcut_config import Shortcut

                app.shortcut.stop()
                app.shortcut = ShortcutManager(
                    app, [Shortcut(**sc) for sc in Config.shortcuts])
                app.shortcut.start()
                # UDPController 在 app.py:78 拿到的是旧 ShortcutManager 的引用，
                # 不换的话 UDP 远程控制会去操作已经停掉的任务表
                if getattr(app, 'udp', None) is not None:
                    app.udp.manager = app.shortcut
                report.applied.append('快捷键')
            except Exception as e:
                report.restart.append('快捷键（重建失败，需重启客户端）')
                logger.error(f'重建快捷键失败: {e}', exc_info=True)

    # ---- 麦克风：重新打开音频流即可 ----
    if 'mic_device' in changed:
        if getattr(app.state, 'recording', False):
            report.restart.append('麦克风（正在录音，请稍后重启）')
        else:
            try:
                app.stream.reopen()
                report.applied.append('麦克风设备')
            except Exception as e:
                report.restart.append('麦克风设备（重开音频流失败，需重启客户端）')
                logger.error(f'重开音频流失败: {e}', exc_info=True)

    # ---- 热词阈值 ----
    if changed & {'hot_thresh', 'hot_similar'}:
        try:
            from config_client import ClientConfig as Config
            corrector = app.hotword.phoneme_corrector
            corrector.threshold = Config.hot_thresh
            corrector.similar_threshold = Config.hot_similar
            # 必须走 load_all()：PhonemeCorrector 内部的 fast_rag 粗筛阈值
            # 只在 update_hotwords() 里重建（hot_phoneme.py:97 的
            # search_threshold = min(threshold, similar_threshold) - 0.1）。
            # 只 setattr 两个阈值不改 fast_rag.threshold，
            # 会出现"粗筛把本该命中的热词提前筛掉"的隐性失效。
            app.hotword.load_all()
            report.applied.append('热词阈值')
        except Exception as e:
            report.restart.append('热词阈值（重建失败，需重启客户端）')
            logger.error(f'重建热词阈值失败: {e}', exc_info=True)

    # ---- 日志级别：Logger.setup 支持重入更新（core/logger.py:63-71） ----
    if 'log_level' in changed:
        try:
            from config_client import ClientConfig as Config
            from core.logger import setup_logger
            setup_logger('client', level=Config.log_level)
            report.applied.append('日志级别')
        except Exception as e:
            report.restart.append('日志级别（热更新失败，需重启客户端）')
            logger.error(f'更新日志级别失败: {e}', exc_info=True)


# ============================================================
#  给用户看的汇总
# ============================================================

def summarize(report: ApplyReport) -> str:
    """拼一句能给 toast 显示的短文案。"""
    if report.errors:
        return '设置保存失败：' + '；'.join(report.errors[:2])

    parts: List[str] = []
    if report.applied:
        parts.append('已生效：' + '、'.join(dict.fromkeys(report.applied)))
    if report.restart:
        parts.append('需重启：' + '、'.join(dict.fromkeys(report.restart)))
    return '\n'.join(parts) if parts else '没有改动'
