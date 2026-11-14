"""
AstrBot 插件: QQ官方内置ASR语音识别

功能:
- QQ内置ASR语音识别 —— 将QQ平台免费提供的语音转写文本(asr_refer_text)注入消息链

路径:
- 新 AstrBot(PatchedMessage 带 raw_data): 直接从消息原始数据读取,无需缓存
- 旧 AstrBot: 经 botpy parser / webhook 捕获原始 attachments 后缓存提取
- Webhook 模式: HTTP 入口提前捕获

注: 引用消息解析已移除(AstrBot 4.26.x 已原生支持)。

参考实现: https://github.com/NousResearch/hermes-agent (gateway/platforms/qqbot)
"""

from __future__ import annotations

import json
import threading
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register


def _detect_new_adapter() -> bool:
    """检测 AstrBot 适配器是否为新版(PatchedMessage 保留 raw_data)。

    新版适配器源码中存在 _ensure_group_message_create_parser 符号,
    以此作为版本判定依据。旧版则需走 parser 缓存路径获取原始 attachments。
    """
    try:
        from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
            _ensure_group_message_create_parser,
        )
        return callable(_ensure_group_message_create_parser)
    except (ImportError, AttributeError):
        pass
    return False


# ====================================================================
# 全局开关(由 _apply_patches 设置)
# ====================================================================

_APPLY_ASR = True
_HAS_RAW_DATA = False  # AstrBot PatchedMessage 是否有 raw_data

# ====================================================================
# 模块级缓存(旧 AstrBot 的 attachments 捕获)
# ====================================================================

_raw_attachments_cache: dict[str, list] = {}
_raw_attachments_timestamps: dict[str, float] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 300
_CACHE_MAX_SIZE = 500

_patch_applied = False
_patched_parsers: set[str] = set()


def _store_raw_attachments(message_id: str, attachments: list | None) -> None:
    if not message_id or not attachments:
        return
    with _cache_lock:
        _raw_attachments_cache[message_id] = attachments
        _raw_attachments_timestamps[message_id] = time.monotonic()
        _evict_expired_locked()


def _pop_raw_attachments(message_id: str) -> list | None:
    if not message_id:
        return None
    with _cache_lock:
        _raw_attachments_timestamps.pop(message_id, None)
        return _raw_attachments_cache.pop(message_id, None)


def _evict_expired_locked() -> None:
    if len(_raw_attachments_cache) <= _CACHE_MAX_SIZE:
        return
    now = time.monotonic()
    expired = [mid for mid, ts in _raw_attachments_timestamps.items() if now - ts > _CACHE_TTL]
    for mid in expired:
        _raw_attachments_cache.pop(mid, None)
        _raw_attachments_timestamps.pop(mid, None)
    if len(_raw_attachments_cache) > _CACHE_MAX_SIZE:
        _raw_attachments_cache.clear()
        _raw_attachments_timestamps.clear()


# ====================================================================
# QQ 内置 ASR 语音识别
# ====================================================================


def _extract_asr_from_attachments(raw_attachments: list | None) -> list[str]:
    if not raw_attachments or not isinstance(raw_attachments, list):
        return []
    transcripts: list[str] = []
    audio_exts = {".mp3", ".wav", ".ogg", ".m4a", ".amr", ".silk"}
    audio_cts = ("voice", "audio")
    for att in raw_attachments:
        if not isinstance(att, dict):
            continue
        ct = str(att.get("content_type", "") or "").lower()
        fn = str(att.get("filename", "") or att.get("name", "") or "")
        ext = "." + fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
        if not (ct.startswith(audio_cts) or ext in audio_exts):
            continue
        asr_text = str(att.get("asr_refer_text", "") or "").strip()
        if asr_text:
            transcripts.append(asr_text)
    return transcripts


# ====================================================================
# Monkey-Patch 引擎
# ====================================================================


def _apply_patches(context: Context, config: dict | None = None) -> None:
    if config is None:
        config = {"enable_asr": True}
    global _APPLY_ASR, _HAS_RAW_DATA
    _APPLY_ASR = bool(config.get("enable_asr", True))
    _HAS_RAW_DATA = _detect_new_adapter()
    global _patch_applied
    if _patch_applied:
        return
    if not _APPLY_ASR:
        logger.info("[qqofficial_quote] ASR 功能关闭, 跳过全部 patch")
        _patch_applied = True
        return
    # parser patch：仅在旧 AstrBot(无 raw_data)时需要,用于捕获原始 attachments
    if not _HAS_RAW_DATA:
        try:
            _patch_connection_state_parsers()
        except Exception as e:
            logger.warning(f"[qqofficial_quote] parser patch 失败: {e}")
    else:
        logger.info("[qqofficial_quote] 跳过 parser patch (AstrBot 已原生保留 raw_data)")
    # parse patch：总是 patch（ASR 从 raw_data 或缓存注入）
    try:
        _patch_parse_from_qqofficial()
    except Exception as e:
        logger.warning(f"[qqofficial_quote] _parse_from_qqofficial patch 失败: {e}")
    # webhook patch：webhook 模式在 HTTP 入口提前捕获
    try:
        _patch_webhook_handle_callback(context)
    except Exception as e:
        logger.warning(f"[qqofficial_quote] webhook patch 失败: {e}")
    _patch_applied = True
    logger.info(
        f"[qqofficial_quote] patch 已应用 "
        f"(asr={'启用(raw_data)' if _HAS_RAW_DATA else '启用(cache)'})"
    )


def _patch_connection_state_parsers() -> None:
    try:
        from botpy.connection import ConnectionState
    except ImportError:
        logger.warning("[qqofficial_quote] botpy 未安装, 跳过 parser patch")
        return
    event_names = [
        "group_at_message_create", "group_message_create",
        "c2c_message_create", "at_message_create", "direct_message_create",
    ]
    for event_name in event_names:
        parser_name = f"parse_{event_name}"
        orig = getattr(ConnectionState, parser_name, None)
        if orig is None or getattr(orig, "_qq_quote_patched", False):
            continue
        def _make_wrapper(o, n=parser_name):
            def wrapped(self, payload):
                d = payload.get("d", {}) or {}
                mid = d.get("id")
                if mid and _APPLY_ASR and d.get("attachments"):
                    _store_raw_attachments(str(mid), d.get("attachments"))
                return o(self, payload)
            wrapped._qq_quote_patched = True
            return wrapped
        setattr(ConnectionState, parser_name, _make_wrapper(orig))
        _patched_parsers.add(parser_name)


def _patch_parse_from_qqofficial() -> None:
    try:
        from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
            QQOfficialPlatformAdapter,
        )
    except ImportError:
        logger.warning("[qqofficial_quote] 无法导入 QQOfficialPlatformAdapter")
        return
    orig = QQOfficialPlatformAdapter._parse_from_qqofficial
    if getattr(orig, "_qq_quote_patched", False):
        return
    async def patched(message, msg_type, force_group_mention=False, **kwargs):
        abm = await orig(message, msg_type, force_group_mention=force_group_mention, **kwargs)
        if _APPLY_ASR:
            raw_atts = None
            # 新 AstrBot: 直接从 message.raw_data 读取，无需缓存
            if _HAS_RAW_DATA:
                raw_data = getattr(message, "raw_data", None)
                if isinstance(raw_data, dict):
                    raw_atts = raw_data.get("attachments")
            else:
                # 旧 AstrBot: 从缓存获取
                mid = str(getattr(abm, "message_id", "") or "")
                raw_atts = _pop_raw_attachments(mid)

            if raw_atts:
                ts = _extract_asr_from_attachments(raw_atts)
                if ts:
                    for t in ts:
                        abm.message.append(Plain(f"[语音转文字] {t}"))
                    blk = "\n".join(f"[语音转文字] {t}" for t in ts)
                    abm.message_str = f"{abm.message_str}\n{blk}" if abm.message_str else blk
        return abm
    patched._qq_quote_patched = True
    QQOfficialPlatformAdapter._parse_from_qqofficial = staticmethod(patched)
    logger.info("[qqofficial_quote] 已 patch _parse_from_qqofficial")


def _patch_webhook_handle_callback(context: Context) -> None:
    """patch QQOfficialWebhook.handle_callback。
    Webhook 模式下在 HTTP 入口提前捕获原始 payload 的 attachments。
    消息对象仍可能有 raw_data，但为兼容性也同时走缓存路径。
    """
    try:
        from astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_server import (
            QQOfficialWebhook,
        )
    except ImportError:
        return
    orig = QQOfficialWebhook.handle_callback
    if getattr(orig, "_qq_quote_patched", False):
        return
    async def patched_handle(self, request):
        try:
            body = await request.get_data()
            if body:
                msg = json.loads(body.decode("utf-8"))
                d = (msg.get("d") or {}) if isinstance(msg, dict) else {}
                mid = d.get("id") if isinstance(d, dict) else None
                if mid and _APPLY_ASR and d.get("attachments"):
                    _store_raw_attachments(str(mid), d.get("attachments"))
        except Exception:
            pass
        return await orig(self, request)
    patched_handle._qq_quote_patched = True
    QQOfficialWebhook.handle_callback = patched_handle
    logger.info("[qqofficial_quote] 已 patch QQOfficialWebhook.handle_callback")


# ====================================================================
# 插件入口
# ====================================================================


@register(
    "astrbot_plugin_qqofficial_quote",
    "yuweitk",
    "QQ官方内置ASR语音识别",
    "0.5.0",
)
class QQOfficialQuotePlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config if config is not None else AstrBotConfig({
            "enable_asr": True,
        })

    async def initialize(self) -> None:
        logger.info(
            f"[qqofficial_quote] v0.5.0 已加载 "
            f"(asr={'启用' if self.config.get('enable_asr', True) else '关闭'}, "
            f"新版适配器raw_data={'是' if _detect_new_adapter() else '否'})"
        )

    @filter.on_platform_loaded()
    async def _on_platform_loaded(self) -> None:
        try:
            _apply_patches(self.context, dict(self.config))
        except Exception as e:
            logger.error(f"[qqofficial_quote] 应用 patch 失败: {e}", exc_info=True)

    @filter.command("qqquote")
    async def cmd_qqquote(self, event: AstrMessageEvent):
        """查看当前配置: /qqquote"""
        ea = self.config.get("enable_asr", True)
        yield event.plain_result(
            f"【QQ官方ASR插件 v0.5.0 配置】\n\n"
            f"ASR语音转文字:  {'✅ 启用' if ea else '❌ 关闭'}\n"
            f"引用消息解析: 已移除(AstrBot 4.26.x 原生支持)\n\n"
            f"📁 配置文件: _conf_schema.json\n"
            f"   在 WebUI 管理面板直接修改, 保存后重启 AstrBot 生效\n"
            f"   当前: enable_asr={str(ea).lower()}"
        )

    async def terminate(self) -> None:
        global _patch_applied
        _patch_applied = False
        with _cache_lock:
            _raw_attachments_cache.clear()
            _raw_attachments_timestamps.clear()
        logger.info("[qqofficial_quote] 插件已卸载")
