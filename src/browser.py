import asyncio
import logging
import re
from typing import Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Request,
)

from config import Config


class StreamBrowser:
    def __init__(self, config: Config):
        self.config = config
        self.log = logging.getLogger("surfchex.browser")
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    async def start(self) -> None:
        self.playwright = await async_playwright().start()

        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=self.config.browser_profile_dir,
            headless=self.config.headless,
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=False,
            args=[
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
            ],
        )

        self.page = (
            self.context.pages[0]
            if self.context.pages
            else await self.context.new_page()
        )

        self.page.on("request", self._on_request)

        self.log.info(
            "Playwright started. headless=%s",
            self.config.headless,
        )

    async def close(self) -> None:
        try:
            if self.context:
                await self.context.close()
        finally:
            if self.playwright:
                await self.playwright.stop()

    def _on_request(self, request: Request) -> None:
        # Request listener is intentionally lightweight.
        # Actual capture is done by get_fresh_stream_url().
        pass

    def _matches(self, url: str) -> bool:
        if self.config.stream_url_regex:
            if re.search(self.config.stream_url_regex, url, re.IGNORECASE):
                return True

        if self.config.stream_url_contains.lower() not in url.lower():
            return False

        return ".m3u8" in url.lower()

    async def get_fresh_stream_url(self, page_url: Optional[str] = None) -> Optional[str]:
        if not self.page:
            raise RuntimeError("Playwright browser is not started.")

        # Default to the legacy single-camera page when none is given.
        page_url = page_url or self.config.camera_page_url

        captured = asyncio.Future()

        def on_request(request: Request) -> None:
            url = request.url

            if not self._matches(url):
                return

            if not captured.done():
                self.log.info("Captured HLS request: %s", url)
                captured.set_result(url)

        self.page.on("request", on_request)

        try:
            self.log.info("Loading camera page: %s", page_url)

            try:
                await self.page.goto(
                    page_url,
                    wait_until="domcontentloaded",
                    timeout=self.config.page_timeout_seconds * 1000,
                )
            except Exception as exc:
                # A page may keep loading video resources indefinitely.
                # Continue waiting for the HLS request.
                self.log.warning("Page navigation warning: %s", exc)

            try:
                url = await asyncio.wait_for(
                    captured,
                    timeout=self.config.capture_timeout_seconds,
                )
                return url
            except asyncio.TimeoutError:
                self.log.warning(
                    "Timed out after %s seconds waiting for .m3u8.",
                    self.config.capture_timeout_seconds,
                )

                # Some players begin after a delayed JavaScript action.
                await asyncio.sleep(self.config.network_idle_wait_seconds)

                if not captured.done():
                    return None

                return captured.result()

        finally:
            self.page.remove_listener("request", on_request)

    async def refresh_and_get_stream_url(self, page_url: Optional[str] = None) -> Optional[str]:
        if not self.page:
            raise RuntimeError("Playwright browser is not started.")

        await self.page.reload(
            wait_until="domcontentloaded",
            timeout=self.config.page_timeout_seconds * 1000,
        )

        return await self.get_fresh_stream_url(page_url=page_url)
