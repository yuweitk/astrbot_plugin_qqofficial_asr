# astrbot_plugin_qqofficial_asr

> 为 AstrBot QQ 官方适配器补全 **QQ内置ASR语音转文字** 能力（v0.5.0 起为纯 ASR 插件, v0.5.1 清理残留 quote 命名）

## 功能

| 功能 | 说明 |
|------|------|
| QQ内置ASR | QQ平台免费提供的语音转文字(`asr_refer_text`),零配置自动识别 |

## 背景

QQ 平台对语音消息免费提供腾讯ASR转写结果(`asr_refer_text`)。
但旧版 `botpy` 的 `_Attachments.__slots__` 丢弃了这个字段,导致语音消息内容无法感知。

> 参考实现: [hermes-agent](https://github.com/NousResearch/hermes-agent/tree/main/gateway/platforms/qqbot)

## 工作原理

本插件通过 **monkey-patch** 技术,在不修改 AstrBot 源码的前提下工作:

```
QQ 推送消息 payload
    |
    +-- [新 AstrBot] PatchedMessage 原生保留 raw_data
    |       -> patched _parse_from_qqofficial 直接从 raw_data 读 attachments
    |
    +-- [旧 AstrBot] patched botpy ConnectionState parser 捕获原始 attachments -> 缓存
    |       -> patched _parse_from_qqofficial 从缓存提取
    |
    +-- [Webhook]   patched handle_callback 在 HTTP 入口捕获 -> 缓存
    |
    v
提取 asr_refer_text -> 注入消息链 [语音转文字] ...
```

### Patch 列表

| 序号 | 目标 | 作用 |
|------|------|------|
| 1 | `botpy.connection.ConnectionState` parser | (旧AstrBot) 捕获原始 `attachments`(含ASR) |
| 2 | `QQOfficialWebhook.handle_callback` | Webhook 模式同样捕获 |
| 3 | `QQOfficialPlatformAdapter._parse_from_qqofficial` | 提取 ASR 文本注入消息链(`**kwargs` 兼容新版 `bot` 参数) |

## 变更记录

- **v0.5.1**: 清理残留的 `qqquote` 指令与 quote 旧命名(类名/日志/注册名/patch标记), 纯 ASR
- **v0.5.0**: 移除引用消息解析功能(AstrBot 4.26.x 已原生支持); `_parse_from_qqofficial` patch 改用 `**kwargs` 透传,兼容新版 `bot` 仅关键字参数(修复普通群聊消息 TypeError)
- **v0.4.0**: 引用消息 + ASR 双功能,配置开关

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/yuweitk/astrbot_plugin_qqofficial_asr.git
```

## ⚠️ 重要：必须重启AstrBot！

本插件通过 `on_platform_loaded` 钩子应用 monkey-patch,该钩子**仅在 AstrBot 启动时平台加载完毕后触发**。

- ❌ **仅重载插件**不会生效
- ❌ **先装插件后启用平台**不会重新触发
- ✅ **必须完全重启 AstrBot** 才能应用 patch

重启后检查日志,应看到:
```
[qqofficial_asr] 插件已加载,等待平台就绪后自动应用 patch...
[qqofficial_asr] patch 已应用 (asr=启用(raw_data))
```

## 配置

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `enable_asr` | `true` | 启用QQ内置ASR语音转文字 |
