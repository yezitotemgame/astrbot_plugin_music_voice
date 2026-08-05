# 网易云音乐语音条插件 (astrbot_plugin_music_voice) v1.2.0

> 网易云点歌 → 下载 MP3 → 转 SILK 语音条 → 通过 NapCat 发送 QQ 语音条

## 当前状态

- **扫码登录**：已废弃（服务器 IP 112.22.138.152 被网易云风控 `8821` 拒绝）
- **Cookie 方案**：当前可行解（管理员 `/设置cookie <Cookie>`）
- **Playwright 临时方案**：服务器真实 Chromium 浏览器可绕过 `8821` 风控（已测试成功）

## 功能

- `/搜歌 <关键词>` — 搜索歌曲并缓存结果
- `/点歌 <序号/歌名>` — 从缓存选歌或直接搜索第一首，发送语音条
- `/设置cookie <Cookie>` — 管理员设置网易云 Cookie（解锁 VIP 歌曲）

## 安装依赖

```bash
pip install requests pysilk
# 可选：Playwright 临时方案（真实浏览器绕风控）
# 使用技能目录 anti-bot-scraper-1.0.0 下的 node_modules/playwright
```

系统要求：`ffmpeg` 在 PATH 中。

## 配置（`astrbot_plugin_music_voice_config.json`）

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| cookie | 网易云 Cookie（含 `MUSIC_U`，用于 VIP 歌曲） | `""` |
| search_limit | 搜索结果数量 | `10` |
| cache_timeout | 搜索缓存时间（秒） | `300` |
| quality | 音质等级（low/medium/standard/high/extreme） | `standard` |

> ⚠️ 配置文件含 Cookie，不应提交到公共仓库。已加入 `.gitignore`。

## Cookie 获取方法

1. 电脑浏览器登录 `music.163.com`
2. `F12` → `Application` → `Cookies` → `https://music.163.com`
3. 找到 `Name = MUSIC_U` 的那一行，复制 `Value` 整串
4. 管理员执行：`/设置cookie MUSIC_U=复制的值`

脚本 `temp_playwright_login.js` 可自动轮询扫码登录并提取 Cookie（已测试成功，保存到配置）。

## 版本历史

- `v1.2.0` — 删除扫码/轮询/登录状态代码；新增 `/设置cookie`；支持 Cookie 取歌；同步配置文件
- `v1.1.x` — 旧扫码方案（已废弃，受 `8821` 风控拦截）

## 仓库

本地已初始化 `git` 仓库（`git init` 完成，`.gitignore` 已配置排除配置文件）。
实际推送到 GitHub 需要仓库 URL + 认证（如 `git push https://token@github.com/user/repo.git`），
当前无真实远程地址，无法自动完成推送。
