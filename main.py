# -*- coding: utf-8 -*-
"""
网易云点歌 → 语音条插件 v2.1.0
搜索网易云音乐，下载音频，用 ffmpeg 转成 amr/silk 格式，通过 NapCat 发送语音条。
遇到 VIP 歌曲时自动用 Playwright 打开网易云二维码登录页截图发给用户，
扫码登录后自动获取 Cookie 并继续播放，无需手动设置。

命令：
  /搜歌 <关键词>    搜索歌曲
  /搜歌手 <关键词>   搜索歌手
  /点歌 <序号/歌名>  点播歌曲（发语音条）
  /点歌帮助          帮助

依赖：ffmpeg, pysilk(可选), playwright
"""
import asyncio
import os
import sys
import time
import tempfile
import subprocess
from typing import Dict, List, Optional

import requests

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.all import Star, Context, AstrBotConfig, logger, Plain

try:
    from astrbot.api.message_components import Record, Image
except ImportError:
    from astrbot.core.message.components import Record, Image

try:
    import pysilk
    import io as _io
    HAS_SILK = True
except ImportError:
    HAS_SILK = False

# 确保能 import 同目录下的 qr_login
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from qr_login import qr_login, poll_cookie, cleanup_login

VERSION = "v2.1.0"
TAG = "[音乐语音条]"

# 音质等级映射：silk采样率 / bitrate
QUALITY_MAP = {
    "low":      (16000, 16000),
    "medium":   (24000, 24000),
    "standard": (24000, 50000),
    "high":     (24000, 80000),
    "extreme":  (24000, 100000),
}

# 取歌音质 → 网易云level
LEVEL_MAP = {
    "low": "standard",
    "medium": "standard",
    "standard": "standard",
    "high": "exhigh",
    "extreme": "lossless",
}

# 网易云 API
SEARCH_URL = "https://music.163.com/api/search/get"
SONG_URL_API = "https://music.163.com/api/song/enhance/player/url/v1"
ARTIST_TOP_SONG_URL = "https://music.163.com/api/artist/top/song"

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
        self._qr_logining = False
        logger.info(f"{TAG} v{VERSION} loaded, ffmpeg={FFMPEG}")

    # ============ 缓存 ============

    def _cache_key(self, event: AstrMessageEvent) -> str:
        gid = getattr(event.message_obj, "group_id", None) or "private"
        return f"{gid}:{event.get_sender_id()}"

    def _get_cache(self, event: AstrMessageEvent):
        """返回 (results, cache_type) 或 (None, None)"""
        key = self._cache_key(event)
        cached = self._cache.get(key)
        if cached is None:
            return None, None
        results, ts, cache_type = cached
        if time.time() - ts < self.config.get("cache_timeout", 300):
            return results, cache_type
        self._cache.pop(key, None)
        return None, None

    def _set_cache(self, event: AstrMessageEvent, results, cache_type="song"):
        self._cache[self._cache_key(event)] = (results, time.time(), cache_type)

    # ============ 网易云 API ============

    @staticmethod
    def _parse_song(s: dict) -> dict:
        """从网易云 API 响应中提取歌曲信息"""
        artists = "/".join(
            a.get("name", "") for a in (s.get("ar") or s.get("artists") or [])
            if isinstance(a, dict) and a.get("name")
        )
        album = (s.get("al") or s.get("album") or {}).get("name", "")
        return {
            "id": s.get("id"),
            "name": s.get("name", "?"),
            "artists": artists or "?",
            "album": album,
            "duration": s.get("dt") or s.get("duration", 0),
        }

    def _search_sync(self, keyword: str, limit: int) -> List[dict]:
        params = {"s": keyword, "type": 1, "limit": limit, "offset": 0}
        resp = requests.get(SEARCH_URL, params=params, headers=HEADERS, timeout=10)
        raw = resp.json()
        if raw.get("code") != 200:
            return []
        return [self._parse_song(s) for s in raw.get("result", {}).get("songs", [])]

    def _search_artists_sync(self, keyword: str, limit: int = 10) -> List[dict]:
        """搜索歌手"""
        params = {"s": keyword, "type": 100, "limit": limit, "offset": 0}
        try:
            resp = requests.get(SEARCH_URL, params=params, headers=HEADERS, timeout=10)
            raw = resp.json()
            if raw.get("code") != 200:
                return []
            result = []
            for a in raw.get("result", {}).get("artists", []):
                result.append({
                    "id": a.get("id"),
                    "name": a.get("name", "?"),
                    "alias": " / ".join(a.get("alias", []) or []),
                    "music_size": a.get("musicSize", 0),
                    "album_size": a.get("albumSize", 0),
                })
            return result
        except Exception as e:
            logger.error(f"{TAG} search_artists err: {e}")
            return []

    def _get_artist_songs_sync(self, artist_id: int, limit: int = 10) -> List[dict]:
        """获取歌手热门歌曲"""
        params = {"id": artist_id}
        try:
            resp = requests.get(ARTIST_TOP_SONG_URL, params=params, headers=HEADERS, timeout=10)
            raw = resp.json()
            if raw.get("code") != 200:
                return self._search_sync(f"artist:{artist_id}", limit)
            return [self._parse_song(s) for s in raw.get("songs", [])[:limit]]
        except Exception as e:
            logger.error(f"{TAG} get_artist_songs err: {e}")
            return []

    def _get_url_sync(self, song_id: int) -> Optional[str]:
        cookie = (self.config.get("cookie") or "").strip()
        headers = dict(HEADERS)
        if cookie:
            headers["Cookie"] = cookie
        level = LEVEL_MAP.get(self.config.get("quality", "standard"), "standard")
        params = {"ids": f"[{song_id}]", "level": level, "encodeType": "aac"}
        try:
            resp = requests.get(SONG_URL_API, params=params, headers=headers, timeout=10)
            data = resp.json().get("data", [])
            if data and data[0].get("url"):
                return data[0]["url"]
        except Exception as e:
            logger.error(f"{TAG} get_url err: {e}")
        return None

    def _download_sync(self, url: str) -> Optional[bytes]:
        try:
            return requests.get(url, headers=HEADERS, timeout=30).content
        except Exception as e:
            logger.error(f"{TAG} download err: {e}")
            return None

    # ============ 音频转码 ============

    def _to_voice_sync(self, data: bytes, song_id: int) -> Optional[str]:
        """把音频数据转成语音格式(silk优先,否则amr)，返回文件路径"""
        src = os.path.join(self._tmpdir, f"{song_id}_src.mp3")
        dst = os.path.join(self._tmpdir, f"{song_id}.silk" if HAS_SILK else f"{song_id}.amr")
        try:
            with open(src, "wb") as f:
                f.write(data)

            # 探测下载内容的实际格式
            probe = subprocess.run([
                FFMPEG, "-i", src
            ], capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            probe_err = probe.stderr.decode('utf-8', 'replace')[:800]
            logger.info(f"{TAG} probe_audio: size={len(data)}, probe_err_head={probe_err[:300]}")

            if HAS_SILK:
                qual = self.config.get("quality", "standard")
                sample_rate, bit_rate = QUALITY_MAP.get(qual, (24000, 50000))
                pcm_path = os.path.join(self._tmpdir, f"{song_id}_pcm.raw")
                p1 = subprocess.run([
                    FFMPEG, "-y", "-i", src,
                    "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", pcm_path
                ], capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
                if p1.returncode != 0 or not os.path.isfile(pcm_path):
                    err_msg = p1.stderr.decode('utf-8', 'replace')[:500]
                    logger.error(f"{TAG} ffmpeg pcm err: {err_msg}")
                    return None
                with open(pcm_path, "rb") as f:
                    pcm_data = f.read()
                os.remove(pcm_path)
                logger.info(f"{TAG} pcm: {len(pcm_data)} bytes, sr={sample_rate}, br={bit_rate}")

                output_io = _io.BytesIO()
                try:
                    pysilk.encode(_io.BytesIO(pcm_data), output_io, sample_rate, bit_rate, tencent=True)
                except Exception as e:
                    logger.error(f"{TAG} pysilk encode exception: {e}")
                    return None
                silk_data = output_io.getvalue()
                if not silk_data:
                    logger.error(f"{TAG} silk encode empty, pcm_size={len(pcm_data)}, sr={sample_rate}, br={bit_rate}")
                    return None
                with open(dst, "wb") as f:
                    f.write(silk_data)
                logger.info(f"{TAG} silk: {len(silk_data)} bytes")
                return dst
            else:
                p = subprocess.run([
                    FFMPEG, "-y", "-i", src,
                    "-ar", "8000", "-ac", "1",
                    "-c:a", "libopencore_amrnb", "-b:a", "12.2k",
                    dst
                ], capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
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

    # ============ 格式化辅助 ============

    @staticmethod
    def _fmt_songs(songs: List[dict], header: str) -> str:
        """格式化歌曲列表为消息文本"""
        lines = [f"{header} {len(songs)} 首：\n"]
        for i, s in enumerate(songs, 1):
            dur = MusicVoicePlugin._fmt(s.get("duration", 0))
            lines.append(f"  {i}. {s['name']} - {s['artists']}")
            lines.append(f"     {s['album']}  {dur}")
        lines.append("\n输入 /点歌 <序号> 发语音条")
        return "\n".join(lines)

    # ============ 二维码登录 ============

    async def _notify_qr_login(self, event: AstrMessageEvent, song_name: str) -> Optional[dict]:
        await event.send(event.plain_result(
            f"「{song_name}」是VIP歌曲，正在打开网易云登录页面...\n"
            "请用网易云音乐APP扫一扫登录，登录后将自动继续播放"
        ))
        login_state = await qr_login(timeout=180)
        if not login_state:
            await event.send(event.plain_result("登录页面打开失败，请稍后重试"))
            return None
        qr_path = login_state["qr_path"]
        if os.path.isfile(qr_path):
            try:
                await event.send(event.chain_result([
                    Plain("截取网易云登录页面如下，请用APP扫一扫👇"),
                    Image.fromFileSystem(qr_path)
                ]))
            except Exception as e:
                logger.error(f"{TAG} 发送二维码图失败: {e}")
                await event.send(event.plain_result(f"登录页面截图失败，请查看: {qr_path}"))
        else:
            await event.send(event.plain_result("登录页面截图失败"))
            await cleanup_login(login_state)
            return None
        return login_state

    async def _wait_qr_login_and_save(self, event: AstrMessageEvent, login_state: dict) -> Optional[str]:
        try:
            cookie = await poll_cookie(login_state, timeout=180)
            if cookie:
                self.config["cookie"] = cookie
                self.config.save_config()
                logger.info(f"{TAG} Cookie已通过扫码获取并保存")
                await event.send(event.plain_result(
                    "✅ 扫码登录成功！Cookie已自动保存，正在继续播放..."
                ))
            else:
                await event.send(event.plain_result("⏰ 二维码登录超时，请重新点歌再试"))
        except Exception as e:
            logger.error(f"{TAG} 扫码等待失败: {e}")
            await event.send(event.plain_result("扫码等待异常，请重试"))
            cookie = None
        finally:
            await cleanup_login(login_state)
            self._qr_logining = False
        return cookie

    # ============ 命令 ============

    @filter.command("点歌帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "🎵 音乐语音条 " + VERSION + "\n"
            "━━━━━━━━━━━━━━━\n"
            "🔍 /搜歌 <关键词>    搜索歌曲\n"
            "   例: /搜歌 晴天\n\n"
            "🎤 /搜歌手 <关键词>  搜索歌手\n"
            "   例: /搜歌手 周杰伦\n\n"
            "🎵 /点歌 <序号>      发语音条\n"
            "   搜歌后: /点歌 1 → 播第1首歌\n"
            "   搜歌手后: /点歌 1 → 列出该歌手热门歌曲\n"
            "   /点歌 <歌名> → 直接搜并播第一首\n\n"
            "🔑 VIP歌曲会自动打开网易云登录页面截图\n"
            "   用网易云APP扫码登录后自动解锁\n\n"
            "ℹ /点歌帮助         帮助"
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
        try:
            songs = await asyncio.get_event_loop().run_in_executor(
                None, self._search_sync, keyword, self.config.get("search_limit", 10)
            )
        except Exception as e:
            yield event.plain_result(f"搜索失败: {e}")
            return
        if not songs:
            yield event.plain_result(f"未找到「{keyword}」")
            return
        self._set_cache(event, songs, "song")
        yield event.plain_result(self._fmt_songs(songs, f"🔍 「{keyword}」找到"))

    @filter.command("搜歌手")
    async def cmd_search_artist(self, event: AstrMessageEvent):
        keyword = event.message_str.strip()
        for prefix in ("/搜歌手", "搜歌手"):
            if keyword.startswith(prefix):
                keyword = keyword[len(prefix):].strip()
                break
        if not keyword:
            yield event.plain_result("请提供歌手名，例: /搜歌手 周杰伦")
            return
        try:
            artists = await asyncio.get_event_loop().run_in_executor(
                None, self._search_artists_sync, keyword, self.config.get("search_limit", 10)
            )
        except Exception as e:
            yield event.plain_result(f"搜索失败: {e}")
            return
        if not artists:
            yield event.plain_result(f"未找到歌手「{keyword}」")
            return
        self._set_cache(event, artists, "artist")
        lines = [f"🎤 「{keyword}」找到 {len(artists)} 位歌手：\n"]
        for i, a in enumerate(artists, 1):
            alias = f" ({a['alias']})" if a.get("alias") else ""
            lines.append(f"  {i}. {a['name']}{alias}")
            lines.append(f"     {a.get('music_size', 0)}首单曲 · {a.get('album_size', 0)}张专辑")
        lines.append("\n输入 /点歌 <序号> → 列出该歌手热门歌曲")
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
            cached, cache_type = self._get_cache(event)
            if not cached:
                yield event.plain_result("先 /搜歌 或 /搜歌手，或 /点歌 <歌名> 直接播")
                return

            # 歌手缓存 → 列出热门歌曲
            if cache_type == "artist":
                if idx < 0 or idx >= len(cached):
                    yield event.plain_result(f"序号超出 (1-{len(cached)})")
                    return
                artist = cached[idx]
                yield event.plain_result(f"正在获取「{artist['name']}」的热门歌曲...")
                try:
                    songs = await asyncio.get_event_loop().run_in_executor(
                        None, self._get_artist_songs_sync, artist["id"],
                        self.config.get("search_limit", 10)
                    )
                except Exception as e:
                    yield event.plain_result(f"获取歌手歌曲失败: {e}")
                    return
                if not songs:
                    yield event.plain_result(f"「{artist['name']}」暂无热门歌曲")
                    return
                self._set_cache(event, songs, "song")
                yield event.plain_result(self._fmt_songs(songs, f"🎤 「{artist['name']}」热门歌曲"))
                return

            # 歌曲缓存 → 直接播
            songs = cached
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

        loop = asyncio.get_event_loop()
        song = songs[idx]
        sid = song["id"]
        name = song["name"]
        artists = song["artists"]

        yield event.plain_result(f"正在获取「{name} - {artists}」...")

        url = await loop.run_in_executor(None, self._get_url_sync, sid)

        # VIP歌曲 → 二维码登录
        if not url:
            cookie = (self.config.get("cookie") or "").strip()
            if cookie:
                yield event.plain_result(
                    f"「{name}」获取失败，Cookie可能过期或失效\n"
                    "正在重新弹出二维码登录..."
                )
            else:
                yield event.plain_result(f"「{name}」可能是VIP歌曲，需要登录网易云解锁")

            if self._qr_logining:
                yield event.plain_result("已在等待其他用户扫码登录中，请稍等...")
                return

            self._qr_logining = True
            try:
                login_state = await self._notify_qr_login(event, name)
                if not login_state:
                    return
                new_cookie = await self._wait_qr_login_and_save(event, login_state)
                if new_cookie:
                    url = await loop.run_in_executor(None, self._get_url_sync, sid)
                else:
                    return
            except Exception as e:
                logger.error(f"{TAG} 二维码登录流程异常: {e}")
                yield event.plain_result("登录流程异常，请重试")
                self._qr_logining = False
                return

        if not url:
            yield event.plain_result(f"「{name}」仍无法获取播放链接")
            return

        data = await loop.run_in_executor(None, self._download_sync, url)
        if not data:
            yield event.plain_result("下载失败")
            return

        voice_path = await loop.run_in_executor(None, self._to_voice_sync, data, sid)
        if not voice_path:
            yield event.plain_result("音频转换失败")
            return

        try:
            yield event.chain_result([Record.fromFileSystem(voice_path)])
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
