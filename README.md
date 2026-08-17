# 网易云音乐语音条插件 (astrbot_plugin_music_voice) v2.1.0

> 网易云点歌 → 下载 MP3 → 转 SILK 语音条 → 通过 NapCat 发送 QQ 语音条
> VIP 歌曲自动二维码登录解锁，无需手动设置 Cookie

## 功能

- `/搜歌 <关键词>` — 搜索歌曲并缓存结果
- `/点歌 <序号/歌名>` — 从缓存选歌或直接搜索第一首，发送语音条
- VIP 歌曲自动触发二维码登录：截图发群 → 用户用网易云APP扫码 → 自动获取Cookie → 继续播放

## VIP 歌曲解锁流程

1. 用户点播 VIP 歌曲 → 获取不到播放链接
2. 插件自动用 Playwright（无头浏览器）打开网易云登录页面
3. 截取二维码图片发送到群里
4. 用户用网易云音乐 APP 扫码登录
5. 插件自动检测登录成功 → 提取 Cookie 并保存到配置
6. 自动关闭临时浏览器 → 重新获取播放链接 → 继续播放
7. Cookie 保存后后续点歌自动使用，失效时会再次弹二维码

## 安装依赖

```bash
pip install requests pysilk playwright
python -m playwright install chromium
```

系统要求：`ffmpeg` 在 PATH 中。

## 配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| cookie | 网易云 Cookie（自动扫码获取，也可手动填） | `""` |
| search_limit | 搜索结果数量 | `10` |
| cache_timeout | 搜索缓存时间（秒） | `300` |
| quality | 音质等级（low/medium/standard/high/extreme） | `standard` |

## 版本历史

- `v2.1.0` — 去掉 `/设置cookie` 命令；VIP 歌曲自动 Playwright 二维码登录截图发群，扫码后自动获取 Cookie 继续播放
- `v1.2.0` — 手动 `/设置cookie` 方案
- `v1.1.x` — 旧扫码方案（已废弃）
