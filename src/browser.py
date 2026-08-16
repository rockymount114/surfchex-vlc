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
    # HLS/fMP4 media segment URLs: blocked so the camera page never actually
    # downloads/decodes the stream once (or before) we have the .m3u8 URL.
    _SEGMENT_RE = re.compile(r"\.(ts|m4s|mp4)(\?|$)", re.IGNORECASE)

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
                "--disable-gpu",   # keep the GPU free for OBS (no video decode needed)
                "--mute-audio",    # no need to decode/mix audio at all
            ],
        )

        self.page = (
            self.context.pages[0]
            if self.context.pages
            else await self.context.new_page()
        )

        self.page.on("request", self._on_request)

        # Block media-segment downloads so the page never actually streams
        # video (we only need the .m3u8 URL).  The route persists across
        # navigations, so it is installed once here.
        try:
            await self.page.route(
                self._SEGMENT_RE,
                lambda route: route.abort(),
            )
            self.log.info(
                "Media-segment requests blocked; the camera page stays idle "
                "after the .m3u8 URL is captured."
            )
        except Exception as e:
            self.log.debug("Could not install segment blocker: %s", e)

        self.log.info(
            "Playwright started. headless=%s",
            self.config.headless,
        )

    async def park_page(self) -> None:
        """Unload the camera page (about:blank) to stop its video decoding and
        JS timers between refreshes.

        The URL has already been captured and OBS/VLC is playing it, so the
        page does not need to keep running.  The next ``get_fresh_stream_url``
        navigation reloads the camera page normally.
        """
        if not self.page:
            return
        try:
            await self.page.goto(
                "about:blank",
                wait_until="domcontentloaded",
                timeout=5000,
            )
            self.log.info("Camera page parked (about:blank) to reduce CPU/GPU usage.")
        except Exception as e:
            self.log.warning("Could not park camera page: %s", e)

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

    async def scrape_camera_info(self) -> dict:
        """Extract the camera name, weather and tide from the loaded page.

        Values are raw DOM text; missing widgets become None.  Waits until the
        weather/tide widgets are filled in by the page's JS (they start with
        "--" placeholders), so the scrape does not catch the loading state.
        """
        # Weather: wait until the temperature shows a real value (not "--").
        try:
            await self.page.wait_for_function(
                """() => {
                    const el = document.querySelector('#wc-temp');
                    return el && el.textContent.trim() && !el.textContent.includes('--');
                }""",
                timeout=15000,
            )
        except Exception:
            pass  # widget missing/hidden -> evaluate returns None
        # Tide: wait until the "now" line is populated.
        try:
            await self.page.wait_for_function(
                """() => {
                    const el = document.querySelector('#tide-now');
                    return el && el.textContent.trim().length > 0;
                }""",
                timeout=10000,
            )
        except Exception:
            pass

        try:
            return await self.page.evaluate(
                """() => {
                    const txt = (sel) => {
                        const el = document.querySelector(sel);
                        return el ? el.textContent.trim() : null;
                    };
                    const active = document.querySelector('#camera-list .cam-btn.active');
                    return {
                        name: active ? (active.textContent || '').trim() : null,
                        weather: {
                            title: txt('.wc-title'),
                            temp: txt('#wc-temp'),
                            feels: txt('#wc-feels'),
                            windDir: txt('#wc-wind-dir-txt'),
                            wind: txt('#wc-wind-val'),
                            gusts: txt('#wc-gusts'),
                            humidity: txt('#wcx-hum'),
                            pressure: txt('#wcx-press'),
                            dew: txt('#wcx-dew'),
                            rain: txt('#wcx-rain'),
                        },
                        tide: {
                            station: txt('#tide-station-name'),
                            stationMeta: txt('#tide-station-meta'),
                            now: txt('#tide-now'),
                            nextEvent: txt('#tide-next-event'),
                            nextHeight: txt('#tide-next-height'),
                        }
                    };
                }"""
            )
        except Exception as e:
            self.log.warning("Could not scrape camera info: %s", e)
            return {}

    async def refresh_and_get_stream_url(self, page_url: Optional[str] = None) -> Optional[str]:
        if not self.page:
            raise RuntimeError("Playwright browser is not started.")

        await self.page.reload(
            wait_until="domcontentloaded",
            timeout=self.config.page_timeout_seconds * 1000,
        )

        return await self.get_fresh_stream_url(page_url=page_url)
