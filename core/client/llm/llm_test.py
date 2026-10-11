# coding: utf-8
"""
LLM 连通性测试

设置向导和设置面板上的「测试连接」按钮用它 —— 填完 key 立刻知道对不对，
而不是等说了一句话才发现没用。

只发一次最小的请求（max_tokens=4），成本可以忽略。
"""

from __future__ import annotations

from typing import Tuple

try:
    from . import logger
except Exception:                                  # pragma: no cover
    import logging
    logger = logging.getLogger('client')


def _friendly_error(err: Exception) -> str:
    """把 SDK 的异常翻译成用户看得懂的中文。"""
    name = type(err).__name__
    text = str(err)
    low = text.lower()

    if 'authenticationerror' in name.lower() or '401' in text or 'invalid api key' in low:
        return 'API Key 无效或已失效，请到对应平台的开放控制台重新生成'
    if 'notfound' in name.lower() or '404' in text or 'model_not_found' in low:
        return '模型名不对：这个账号下找不到该模型，检查拼写或换个模型'
    if 'ratelimit' in name.lower() or '429' in text or 'insufficient' in low or 'balance' in low:
        return '额度不足或被限流：去平台看下余额'
    # 网络类：既要看异常类名（APIConnectionError / APITimeoutError），
    # 也要看消息文本 —— 有些 SDK 会把连接问题包成普通 Exception
    network_hint = ('connection', 'timeout', 'timed out', 'ssl', 'unreachable',
                    'getaddrinfo', 'max retries')
    if (any(h in name.lower() for h in network_hint)
            or any(h in low for h in network_hint)):
        return ('连不上服务端：检查网络。如果该平台在墙外（如 OpenAI），'
                '可能需要开代理')
    if 'permission' in name.lower() or '403' in text:
        return '这个 Key 没有调用该模型的权限'

    # 兜底：原样带上，别把信息吞掉
    return f'{name}: {text[:160]}'


def test_connection(provider: str, api_url: str = '', api_key: str = '',
                    model: str = '', timeout: float = 20.0) -> Tuple[bool, str]:
    """
    真发一次请求验证 provider / api_key / model 这组配置能不能用。

    Returns:
        (是否成功, 给用户看的说明)
    """
    provider = (provider or '').strip()
    if not provider:
        return False, '没选提供商'

    try:
        from .llm_constants import APIConfig
        url = (api_url or '').strip() or APIConfig.DEFAULT_API_URLS.get(provider, '')
    except Exception as e:
        return False, f'读不到提供商默认地址: {e}'

    if not url:
        return False, f'不知道 {provider} 的默认地址，请手动填 API 地址'

    if not (model or '').strip():
        return False, '模型名是空的'

    # ollama 走自己的 SDK，且不需要 key
    if provider == 'ollama':
        try:
            from ollama import Client as OllamaClient
            client = OllamaClient(host=url, timeout=timeout)
            client.list()
            return True, f'连接成功，Ollama 在 {url} 上跑着'
        except Exception as e:
            return False, _friendly_error(e)

    key = (api_key or '').strip()
    if not key:
        try:
            from .llm_constants import APIConfig
            key = APIConfig.DEFAULT_API_KEYS.get(provider, '')
        except Exception:
            key = ''

    # 本机服务（CC Switch / LM Studio / Ollama 等）一般不校验 Key
    # —— 实测 CC Switch 的 /v1/chat/completions 有没有 Authorization 都返回 200。
    # 但 openai SDK 拿到空 api_key 会直接抛 OpenAIError: Missing credentials，
    # 所以给个占位符，别因为"没填 key"把用户挡在门外。
    low = url.lower()
    if not key and any(h in low for h in ('127.0.0.1', 'localhost',
                                          '0.0.0.0', '[::1]')):
        key = 'sk-local'

    if not key:
        return False, '还没填 API Key'

    try:
        from openai import OpenAI
    except Exception as e:
        return False, f'缺少 openai 依赖: {e}'

    try:
        client = OpenAI(base_url=url, api_key=key, timeout=timeout)
        resp = client.chat.completions.create(
            model=model.strip(),
            messages=[{'role': 'user', 'content': 'ping'}],
            max_tokens=4,
        )
        used = getattr(resp, 'model', model)
        logger.info(f'LLM 连通性测试通过: {provider} / {used}')
        return True, f'连接成功，{used} 可用'

    except Exception as e:
        msg = _friendly_error(e)
        logger.info(f'LLM 连通性测试失败: {provider} / {model} -> {msg}')
        return False, msg
