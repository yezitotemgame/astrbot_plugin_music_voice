# -*- coding: utf-8 -*-
"""
网易云点歌 → 语音条插件
搜索网易云音乐，下载音频，用 ffmpeg 转成 amr 格式，通过 NapCat 发送语音条。
遇到 VIP 歌曲时提示发送网易云 Cookie 解锁。

命令：
  /搜歌 <关键词>    搜索歌曲
  /点歌 <序号>       点播搜索结果中的歌曲（发语音条）
  /设置cookie <cookie>  保存网易云Cookie（解锁VIP歌曲，仅管理员）
  /点歌帮助          帮助

依赖：ffmpeg, pysilk(可选)
"""
import asyncio
import os
import time
import tempfile
import subprocess
from typing import Dict, List, Optional

import requests

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.all import Star, Context, AstrBotConfig, logger, Plain

try:
    from astrbot.api.message_components import Record
except ImportError:
    from astrbot.core.message.components import Record

try:
    import pysilk
    import io as _io
    HAS_SILK = True
except ImportError:
    HAS_SILK = False

VERSION = "v1.2.0"
TAG = "[音乐语音条]"

# 音质等级映射：silk采样率 / bitrate
QUALITY_MAP = {
    "low":      (16000, 16000),   # 1档 低品质
    "medium":   (24000, 24000),   # 2档 中等品质
    "standard": (24000, 50000),   # 3档 标准品质(默认)
    "high":     (24000, 80000),   # 4档 高品质
    "extreme":  (24000, 100000),  # 5档 极致品质
}

# 取歌音质 → 网易云level
LEVEL_MAP = {
    "low": "standard",
    "medium": "standard",
    "standard": "standard",
    "high": "exhigh",
    "extreme": "lossless",
}

# 网易云搜索 API
SEARCH_URL = "https://music.163.com/api/search/get"
# 网易云歌曲链接 API（新版v1明文接口）
SONG_URL_API = "https://music.163.com/api/song/enhance/player/url/v1"

# HTTP 头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://music.163.com/",
}


def _find_ffmpeg():
    for name in ("ffmpeg", "ffmpeg.exe"):
        for p in os.environ.get("PATH", "").split(os.pathsep):
            fp = os.path.join(p.strip(), name)
            if os.path.isfile(fp):
                return fp
    for base in [
        r"C:\Users\Administrator\AppData\Local\Microsoft\WinGet\Links",
        os.environ.get("LOCALAPPDATA", ""),
    ]:
        fp = os.path.join(base, "ffmpeg.exe")
        if os.path.isfile(fp):
            return fp
    return "ffmpeg"


FFMPEG = _find_ffmpeg()


class MusicVoicePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._cache: Dict[str, tuple] = {}
        self._tmpdir = os.path.join(tempfile.gettempdir(), "music_voice")
        os.makedirs(self._tmpdir, exist_ok=True)
        logger.info(f"{TAG} v{VERSION} loaded, ffmpeg={FFMPEG}")

    def _cache_key(self, event: AstrMessageEvent) -> str:
        gid = getattr(event.message_obj, "group_id", None) or "private"
        return f"{gid}:{event.get_sender_id()}"

    def _get_cache(self, event: AstrMessageEvent) -> Optional[List[Dict]]:
        key = self._cache_key(event)
        cached = self._cache.get(key)
        if cached is None:
            return None
        songs, ts = cached
        if time.time() - ts < self.config.get("cache_timeout", 300):
            return songs
        self._cache.pop(key, None)
        return None

    def _set_cache(self, event: AstrMessageEvent, songs):
        self._cache[self._cache_key(event)] = (songs, time.time())

    def _search_sync(self, keyword: str, limit: int) -> List[Dict]:
        params = {"s": keyword, "type": 1, "limit": limit, "offset": 0}
        resp = requests.get(SEARCH_URL, params=params, headers=HEADERS, timeout=10)
        raw = resp.json()
        if raw.get("code") != 200:
            return []
        songs = raw.get("result", {}).get("songs", [])
        result = []
        for s in songs:
            artists = "/".join(
                a.get("name", "") for a in (s.get("ar") or s.get("artists") or [])
                if isinstance(a, dict) and a.get("name")
            )
            album = (s.get("al") or s.get("album") or {}).get("name", "")
            result.append({
                "id": s.get("id"),
                "name": s.get("name", "?"),
                "artists": artists or "?",
                "album": album,
                "duration": s.get("dt") or s.get("duration", 0),
            })
        return result

    def _get_url_sync(self, song_id: int) -> Optional[str]:
        cookie = (self.config.get("cookie") or "").strip()
        headers = dict(HEADERS)
        if cookie:
            headers["Cookie"] = cookie
        qual = self.config.get("quality", "standard")
        level = LEVEL_MAP.get(qual, "standard")
        params = {"ids": f"[{song_id}]", "level": level, "encodeType": "aac"}
        try:
            resp = requests.get(SONG_URL_API, params=params, headers=headers, timeout=10)
            raw = resp.json()
            data = raw.get("data", [])
            if data and data[0].get("url"):
                return data[0]["url"]
        except Exception as e:
            logger.error(f"{TAG} get_url err: {e}")
        return None

    def _download_sync(self, url: str) -> Optional[bytes]:
        try:
            r = requests.get(url, headers=HEADERS, timeout=30, stream=True)
            chunks = []
            for chunk in r.iter_content(8192):
                chunks.append(chunk)
            return b"".join(chunks)
        except Exception as e:
            logger.error(f"{TAG} download err: {e}")
            return None

    def _to_voice_sync(self, data: bytes, song_id: int) -> Optional[str]:
        """把音频数据转成语音格式(silk优先,否则amr)，返回文件路径"""
        src = os.path.join(self._tmpdir, f"{song_id}_src.mp3")
        dst = os.path.join(self._tmpdir, f"{song_id}.silk" if HAS_SILK else f"{song_id}.amr")
        try:
            with open(src, "wb") as f:
                f.write(data)

            if HAS_SILK:
                # mp3 → PCM → silk (按配置音质)
                qual = self.config.get("quality", "standard")
                sample_rate, bit_rate = QUALITY_MAP.get(qual, (24000, 50000))
                pcm_path = os.path.join(self._tmpdir, f"{song_id}_pcm.raw")
                p1 = subprocess.run([
                    FFMPEG, "-y", "-i", src,
                    "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", pcm_path
                ], capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
                if p1.returncode != 0 or not os.path.isfile(pcm_path):
                    logger.error(f"{TAG} ffmpeg pcm err: {p1.stderr.decode('utf-8','replace')[:200]}")
                    return None
                with open(pcm_path, "rb") as f:
                    pcm_data = f.read()
                os.remove(pcm_path)

                input_io = _io.BytesIO(pcm_data)
                output_io = _io.BytesIO()
                pysilk.encode(input_io, output_io, sample_rate, bit_rate, tencent=True)
                silk_data = output_io.getvalue()
                if not silk_data:
                    logger.error(f"{TAG} silk encode empty")
                    return None
                with open(dst, "wb") as f:
                    f.write(silk_data)
                logger.info(f"{TAG} silk: {len(silk_data)} bytes")
                return dst
            else:
                # 回退 amr
                cmd = [
                    FFMPEG, "-y", "-i", src,
                    "-ar", "8000", "-ac", "1",
                    "-c:a", "libopencore_amrnb",
                    "-b:a", "12.2k",
                    dst
                ]
                p = subprocess.run(cmd, capture_output=True, timeout=30,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                if p.returncode != 0 or not os.path.isfile(dst):
                    logger.error(f"{TAG} ffmpeg amr err: {p.stderr.decode('utf-8','replace')[:200]}")
                    return None
                return dst
        except Exception as e:
            logger.error(f"{TAG} to_voice err: {e}")
            return None
        finally:
            try:
                os.remove(src)
            except Exception:
                pass

    def _cleanup(self):
        try:
            now = time.time()
            for f in os.listdir(self._tmpdir):
                fp = os.path.join(self._tmpdir, f)
                if os.path.isfile(fp) and now - os.path.getmtime(fp) > 3600:
                    os.remove(fp)
        except Exception:
            pass

    # ============ Cookie 设置 ============

    @filter.command("设置cookie")
    async def cmd_set_cookie(self, event: AstrMessageEvent):
        """保存网易云Cookie，解锁VIP歌曲。仅管理员可操作。"""
        if not event.is_admin():
            yield event.plain_result("只有群主/管理员才能设置Cookie")
            return
        text = event.message_str.strip()
        for prefix in ("/设置cookie", "设置cookie"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        text = text.strip()
        if not text:
            yield event.plain_result(
                "🔑 /设置cookie 用法说明\n"
                "━━━━━━━━━━━━━━━\n"
                "📌 获取Cookie（电脑浏览器）:\n"
                "1. 打开 music.163.com 并登录账号\n"
                "2. 按 F12 打开开发者工具\n"
                "3. 点上方「Application」(应用) 标签\n"
                "4. 左侧展开 Cookies → 点 https://music.163.com\n"
                "5. 在表格里找 Name 为 MUSIC_U 的那一行\n"
                "6. 双击 Value 列复制整串值（很长一串）\n"
                "━━━━━━━━━━━━━━━\n"
                "📨 然后发送:\n"
                "/设置cookie MUSIC_U=复制的整串值\n"
                "（可连其他项一起，如 MUSIC_U=xxx; MUSIC_A=yyy）\n"
                "⚠️ 必须有 MUSIC_U 这一项，否则等于没登录"
            )
            return
        if "MUSIC_U" not in text and "=" not in text:
            yield event.plain_result("这看起来不像Cookie（需包含MUSIC_U或=），请检查后重发")
            return
        self.config["cookie"] = text
        self.config.save_config()
        logger.info(f"{TAG} cookie updated by admin")
        yield event.plain_result("✅ Cookie 已保存！现在可以点播VIP歌曲了")

    # ============ 点歌 ============

    @filter.command("点歌帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "🎵 音乐语音条 " + VERSION + "\n"
            "━━━━━━━━━━━━━━━\n"
            "🔍 /搜歌 <关键词>   搜索歌曲\n"
            "   例: /搜歌 晴天\n\n"
            "🎤 /点歌 <序号>     发语音条\n"
            "   例: /点歌 1\n\n"
            "🔑 /设置cookie <Cookie>  解锁VIP歌曲(仅管理员)\n\n"
            "ℹ /点歌帮助        帮助"
        )

    @filter.command("搜歌")
    async def cmd_search(self, event: AstrMessageEvent):
        keyword = event.message_str.strip()
        for prefix in ("/搜歌", "搜歌"):
            if keyword.startswith(prefix):
                keyword = keyword[len(prefix):].strip()
                break
        if not keyword:
            yield event.plain_result("请提供关键词，例: /搜歌 晴天")
            return
        limit = self.config.get("search_limit", 10)
        try:
            songs = await asyncio.get_event_loop().run_in_executor(
                None, self._search_sync, keyword, limit
            )
        except Exception as e:
            yield event.plain_result(f"搜索失败: {e}")
            return
        if not songs:
            yield event.plain_result(f"未找到「{keyword}」")
            return
        self._set_cache(event, songs)
        lines = [f"🔍 「{keyword}」找到 {len(songs)} 首：\n"]
        for i, s in enumerate(songs, 1):
            dur = self._fmt(s.get("duration", 0))
            lines.append(f"  {i}. {s['name']} - {s['artists']}")
            lines.append(f"     {s['album']}  {dur}")
        lines.append(f"\n输入 /点歌 <序号> 发语音条")
        yield event.plain_result("\n".join(lines))

    @filter.command("点歌")
    async def cmd_play(self, event: AstrMessageEvent):
        text = event.message_str.strip()
        for prefix in ("/点歌", "点歌"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        if not text:
            yield event.plain_result("用法:\n/点歌 晴天 - 直接搜索并播第一首\n/点歌 1 - 从搜索结果选第1首")
            return

        if text[0].isdigit():
            try:
                idx = int(text.split()[0]) - 1
            except ValueError:
                yield event.plain_result("序号要数字，例: /点歌 1")
                return
            songs = self._get_cache(event)
            if not songs:
                yield event.plain_result("先 /搜歌 搜索，或 /点歌 <歌名> 直接播")
                return
            if idx < 0 or idx >= len(songs):
                yield event.plain_result(f"序号超出 (1-{len(songs)})")
                return
        else:
            yield event.plain_result(f"正在搜索「{text}」...")
            try:
                songs = await asyncio.get_event_loop().run_in_executor(
                    None, self._search_sync, text, 1
                )
            except Exception as e:
                yield event.plain_result(f"搜索失败: {e}")
                return
            if not songs:
                yield event.plain_result(f"未找到「{text}」")
                return
            self._set_cache(event, songs)
            idx = 0
        song = songs[idx]
        sid = song["id"]
        name = song["name"]
        artists = song["artists"]
        yield event.plain_result(f"正在获取「{name} - {artists}」...")
        try:
            url = await asyncio.get_event_loop().run_in_executor(
                None, self._get_url_sync, sid
            )
        except Exception as e:
            yield event.plain_result(f"获取链接失败: {e}")
            return
        if not url:
            cookie = (self.config.get("cookie") or "").strip()
            if cookie:
                yield event.plain_result(
                    f"「{name}」获取失败，Cookie可能过期或失效\n"
                    "请管理员重新发送 /设置cookie <新Cookie>"
                )
            else:
                yield event.plain_result(
                    f"「{name}」可能是VIP歌曲，需要网易云Cookie才能播放\n"
                    "━━━━━━━━━━━━━━━\n"
                    "📌 获取Cookie（电脑浏览器）:\n"
                    "1. 打开 music.163.com 并登录账号\n"
                    "2. 按 F12 打开开发者工具\n"
                    "3. 点上方「Application」(应用) 标签\n"
                    "4. 左侧找到 Cookies → 点 https://music.163.com\n"
                    "5. 表格里找 Name 为 MUSIC_U 的那一行\n"
                    "6. 复制它 Value 的整串值（很长一串）\n"
                    "━━━━━━━━━━━━━━━\n"
                    "📨 发给管理员执行:\n"
                    "/设置cookie MUSIC_U=刚复制的整串值\n"
                    "（可连其他项一起发，如 MUSIC_U=xxx; MUSIC_A=yyy）\n"
                    "⚠️ MUSIC_U 是登录凭证必须要有；Cookie会过期，失效需重新获取"
                )
            return
        try:
            data = await asyncio.get_event_loop().run_in_executor(
                None, self._download_sync, url
            )
        except Exception as e:
            yield event.plain_result(f"下载失败: {e}")
            return
        if not data:
            yield event.plain_result("下载失败")
            return
        amr_path = await asyncio.get_event_loop().run_in_executor(
            None, self._to_voice_sync, data, sid
        )
        if not amr_path:
            yield event.plain_result("音频转换失败")
            return
        try:
            yield event.chain_result([Record.fromFileSystem(amr_path)])
        except Exception as e:
            yield event.plain_result(f"发送失败: {e}")
        finally:
            self._cleanup()

    async def terminate(self):
        logger.info(f"{TAG} unloading...")

    @staticmethod
    def _fmt(ms: int) -> str:
        if ms <= 0:
            return "00:00"
        s = ms // 1000
        return f"{s // 60:02d}:{s % 60:02d}"
