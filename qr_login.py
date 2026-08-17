# -*- coding: utf-8 -*-
"""
网易云二维码登录模块

用 Playwright 打开网易云登录页面，截取二维码发给用户扫码，
然后轮询检测 MUSIC_U cookie 出现，返回完整 cookie 字符串。
扫码成功后自动关闭浏览器。
"""
import asyncio
import time
import os
import tempfile
from typing import Optional

from playwright.async_api import async_playwright

from astrbot.api.all import logger

TAG = "[音乐语音条]"

LOGIN_URL = "https://music.163.com/login"
POLL_INTERVAL = 2
MAX_WAIT = 180


async def qr_login(timeout: int = MAX_WAIT) -> Optional[dict]:
    """
    打开网易云二维码登录页面，返回:
      {
        "qr_path": "/tmp/xxx/qr.png",
        "_pw": playwright, "_browser": browser, "_page": page, "_context": context,
      }
    若超时或失败，返回 None。
    """
    tmpdir = os.path.join(tempfile.gettempdir(), "music_voice_qr")
    os.makedirs(tmpdir, exist_ok=True)
    qr_path = os.path.join(tmpdir, f"qr_{int(time.time())}.png")

    pw = await async_playwright().start()
    browser = None

    try:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
        )
        page = await context.new_page()

        await page.goto(LOGIN_URL, timeout=30000, wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # 确认登录iframe存在（二维码在iframe里）
        login_frame = None
        for f in page.frames:
            if "music.163.com/login" in f.url and f != page.main_frame:
                login_frame = f
                break

        if not login_frame:
            logger.error(f"{TAG} 找不到登录iframe")
            await browser.close()
            await pw.stop()
            return None

        # 确认二维码存在
        canvas = await login_frame.query_selector_all("canvas")
        if not canvas:
            logger.error(f"{TAG} 找不到二维码canvas")
            await browser.close()
            await pw.stop()
            return None

        await page.screenshot(path=qr_path, full_page=False)
        logger.info(f"{TAG} 登录页面截图: {qr_path}")

        return {
            "qr_path": qr_path,
            "_pw": pw,
            "_browser": browser,
            "_page": page,
            "_context": context,
        }

    except Exception as e:
        logger.error(f"{TAG} qr_login 启动失败: {e}")
        if browser:
            await browser.close()
        await pw.stop()
        return None


async def poll_cookie(login_state: dict, timeout: int = MAX_WAIT) -> Optional[str]:
    """轮询检查扫码登录状态，成功返回完整 cookie 字符串。"""
    browser = login_state["_browser"]
    context = login_state["_context"]

    start = time.time()
    try:
        while time.time() - start < timeout:
            await asyncio.sleep(POLL_INTERVAL)
            cookies = await context.cookies()
            if any(c.get("name") == "MUSIC_U" and c.get("value") for c in cookies):
                cookie_str = "; ".join(
                    f"{c['name']}={c['value']}" for c in cookies
                    if c.get("name") and c.get("value") and "music.163.com" in c.get("domain", "")
                )
                logger.info(f"{TAG} 扫码登录成功! cookie长度={len(cookie_str)}")
                return cookie_str
        logger.warning(f"{TAG} 扫码等待超时({timeout}s)")
        return None
    except Exception as e:
        logger.error(f"{TAG} poll_cookie 异常: {e}")
        return None


async def cleanup_login(login_state: dict):
    """清理登录用的浏览器资源"""
    if not login_state:
        return
    pw = login_state.get("_pw")
    browser = login_state.get("_browser")
    try:
        if browser:
            await browser.close()
        if pw:
            await pw.stop()
    except Exception as e:
        logger.error(f"{TAG} cleanup_login: {e}")

    qr_path = login_state.get("qr_path")
    if qr_path and os.path.isfile(qr_path):
        try:
            os.remove(qr_path)
        except Exception:
            pass
