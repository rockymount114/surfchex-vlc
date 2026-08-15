import asyncio
import logging
import signal
import time
from pathlib import Path
from typing import Optional

from config import load_config
from browser import StreamBrowser
from vlc import VLCPlayer

# Try to import OBSUpdater; if missing, disable OBS integration.
try:
    from obs_updater import (
        OBSUpdater,
        LOCATION_SOURCE,
        WEATHER_SOURCE,
        TIDE_SOURCE,
    )
except ImportError:
    OBSUpdater = None
    LOCATION_SOURCE = WEATHER_SOURCE = TIDE_SOURCE = None
    logging.warning("OBSUpdater could not be imported. OBS integration disabled.")


def setup_logging(log_file: str) -> None:
    """Create log directory and configure logging."""
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


async def _fetch_fresh_url(
    browser: StreamBrowser,
    config,
    camera_slugs: list[str],
    cycle_index: int,
) -> tuple[Optional[str], int]:
    """Fetch a fresh signed URL for the camera at ``cycle_index``.

    When rotating, cameras that produce no stream are skipped so a dead
    camera cannot stall the rotation.  Returns the URL and the updated
    ``cycle_index``.
    """
    attempts = len(camera_slugs) if camera_slugs else 1
    for _ in range(attempts):
        page_url = (
            config.camera_page_for(camera_slugs[cycle_index])
            if camera_slugs
            else config.camera_page_url
        )
        url = await browser.get_fresh_stream_url(page_url=page_url)
        if url:
            return url, cycle_index
        if len(camera_slugs) > 1:
            logging.getLogger("surfchex").warning(
                "Camera '%s' produced no stream; trying the next camera.",
                camera_slugs[cycle_index],
            )
            cycle_index = (cycle_index + 1) % len(camera_slugs)
    return None, cycle_index


def _clean_text(value) -> Optional[str]:
    """Return stripped value, or None for empty / '--' placeholder values."""
    if not value:
        return None
    text = str(value).strip()
    if not text or "--" in text:
        return None
    return text


def _format_weather(w: dict) -> str:
    """One-line weather summary for the OBS text overlay."""
    parts = []
    title = _clean_text(w.get("title"))
    if title:
        parts.append(title)
    temp = []
    if _clean_text(w.get("temp")):
        temp.append(w["temp"].strip())
    if _clean_text(w.get("feels")):
        temp.append("feels %s°" % w["feels"].strip())
    if temp:
        parts.append(" ".join(temp))
    if _clean_text(w.get("windDir")) or _clean_text(w.get("wind")):
        parts.append(
            "Wind %s %s mph"
            % (w.get("windDir", "").strip(), w.get("wind", "").strip())
        )
    if _clean_text(w.get("gusts")):
        parts.append("Gusts %s mph" % w["gusts"].strip())
    if _clean_text(w.get("humidity")):
        parts.append("Humidity %s" % w["humidity"].strip())
    if _clean_text(w.get("pressure")):
        parts.append("Pressure %s" % w["pressure"].strip())
    if _clean_text(w.get("dew")):
        parts.append("Dew %s" % w["dew"].strip())
    if _clean_text(w.get("rain")):
        parts.append("Rain %s" % w["rain"].strip())
    return " | ".join(parts) or "Weather unavailable"


def _format_tide(t: dict) -> str:
    """One-line tide summary for the OBS text overlay."""
    parts = []
    if _clean_text(t.get("now")):
        parts.append(t["now"].strip())
    if _clean_text(t.get("nextEvent")):
        nxt = t["nextEvent"].strip()
        if _clean_text(t.get("nextHeight")):
            nxt = "%s (%s)" % (nxt, t["nextHeight"].strip())
        parts.append(nxt)
    if _clean_text(t.get("station")):
        parts.append(t["station"].strip())
    if _clean_text(t.get("stationMeta")):
        parts.append(t["stationMeta"].strip())
    return " | ".join(parts) or "Tide unavailable"


async def main() -> None:
    # Load config and set up logging
    config = load_config()
    setup_logging(config.log_file)
    log = logging.getLogger("surfchex")

    # Shutdown event
    stop_event = asyncio.Event()

    def request_stop(*_args):
        log.info("Shutdown requested.")
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    browser = StreamBrowser(config)
    player = VLCPlayer(config)

    # Initialise OBS updater (optional)
    obs_updater = None
    if config.obs_enabled and OBSUpdater is not None:
        try:
            obs_updater = OBSUpdater(config)
            await obs_updater.connect()
            if obs_updater.ws is not None:
                log.info("OBS WebSocket connected successfully.")
            else:
                log.warning("OBS WebSocket connection failed (ws is None).")
                obs_updater = None
        except Exception as e:
            log.warning("Failed to connect to OBS WebSocket: %s. OBS updates disabled.", e)
            obs_updater = None
    elif config.obs_enabled:
        log.warning("OBS integration enabled in config, but OBSUpdater is not available.")

    try:
        # Start the browser (Playwright)
        await browser.start()

        # Camera selection: ordered list of slugs (default camera first when
        # cycling), and the current position in that list.
        camera_slugs = config.camera_slugs()
        if camera_slugs and config.camera and config.camera not in config.cameras:
            log.warning(
                "Configured camera '%s' is not in the cameras list; using '%s'.",
                config.camera,
                camera_slugs[0],
            )
        if len(camera_slugs) > 1:
            log.info(
                "Camera rotation enabled: cycling %d cameras every %s seconds "
                "(starting at '%s').",
                len(camera_slugs),
                config.camera_cycle_seconds,
                camera_slugs[0],
            )
        elif camera_slugs:
            log.info("Using camera '%s'.", camera_slugs[0])
        else:
            log.info("No 'cameras' configured; using legacy camera_page_url.")

        cycle_index = 0

        # Scraped site info cache: slug -> {"date": "YYYY-MM-DD", "info": {...}}.
        # Camera name / weather / tide are only re-scraped once per day per camera.
        site_info_cache: dict[str, dict] = {}

        async def camera_info(slug: str) -> dict:
            """Camera name + weather + tide for a slug (cached once per day)."""
            today = time.strftime("%Y-%m-%d")
            cached = site_info_cache.get(slug)
            if cached and cached.get("date") == today:
                return cached["info"]
            log.info("Scraping camera info for '%s'...", slug)
            info = await browser.scrape_camera_info() or {}
            if not info.get("name"):
                info["name"] = slug
            site_info_cache[slug] = {"date": today, "info": info}
            return info

        async def update_overlays(slug: str) -> None:
            """Push camera name / weather / tide to the OBS text sources.

            Runs on every camera change / URL refresh so the overlays always
            match the source currently shown in OBS.  Each overlay can be
            switched off in config.yaml (camera_location / camera_weather /
            camera_tide).
            """
            if not obs_updater:
                return
            info = await camera_info(slug)
            name = info.get("name") or slug
            weather = _format_weather(info.get("weather") or {})
            tide = _format_tide(info.get("tide") or {})
            log.info(
                "Overlay for '%s': name='%s' weather='%s' tide='%s'",
                slug, name, weather, tide,
            )
            if config.obs_camera_location:
                await obs_updater.update_text_source(LOCATION_SOURCE, name)
            if config.obs_camera_weather:
                await obs_updater.update_text_source(WEATHER_SOURCE, weather)
            if config.obs_camera_tide:
                await obs_updater.update_text_source(TIDE_SOURCE, tide)

        # Use the initial URL if provided (may be None)
        current_url = config.initial_stream_url

        # If we have an initial URL and OBS is active, update OBS immediately.
        if current_url and obs_updater:
            log.info("Initial URL provided; sending to OBS.")
            await obs_updater.update_stream_url(current_url)

        # Main monitoring loop
        # Estimate of how long a fresh-URL fetch takes; used to plan the
        # refresh so it always completes before the signed URL expires.
        last_fetch_seconds = 30.0
        while not stop_event.is_set():
            try:
                # If no URL, fetch one from the camera page
                if not current_url:
                    log.info("No stream URL available; opening camera page.")
                    fetch_start = time.monotonic()
                    current_url, cycle_index = await _fetch_fresh_url(
                        browser, config, camera_slugs, cycle_index
                    )
                    last_fetch_seconds = time.monotonic() - fetch_start
                    log.info("Fresh URL fetch took %.1f seconds.", last_fetch_seconds)

                # If still no URL, wait and retry
                if not current_url:
                    log.warning(
                        "No .m3u8 request captured. Retrying in %s seconds.",
                        config.retry_seconds,
                    )
                    await asyncio.sleep(config.retry_seconds)
                    continue

                log.info("Using stream URL: %s", current_url)
                if camera_slugs:
                    log.info("Camera: %s", camera_slugs[cycle_index])

                # Update OBS with the new URL (with debug log)
                if obs_updater:
                    log.info("Sending updated URL to OBS source '%s'", config.obs_source_name)
                    await obs_updater.update_stream_url(current_url)

                # Update the OBS text overlays for the active camera
                # (camera name / weather / tide, scraped at most once a day).
                if camera_slugs:
                    await update_overlays(camera_slugs[cycle_index])

                # Launch VLC with the stream URL (no-op when vlc_player=false)
                player.start(current_url)

                # Refresh early enough that OBS never runs out of a valid URL:
                # at least the configured margin, and at least twice the time
                # the last fetch took, plus 30 s of slack.
                refresh_before = max(
                    config.refresh_before_seconds,
                    int(last_fetch_seconds) * 2 + 30,
                )

                # Monitor VLC process and URL expiration (and camera rotation)
                reason = await player.monitor(
                    current_url=current_url,
                    stop_event=stop_event,
                    refresh_before_seconds=refresh_before,
                    check_interval=config.monitor_interval_seconds,
                    rotate_after_seconds=(
                        config.camera_cycle_seconds
                        if len(camera_slugs) > 1
                        else None
                    ),
                )

                if stop_event.is_set():
                    break

                if reason == "rotate":
                    # Time to move to the next camera.
                    cycle_index = (cycle_index + 1) % len(camera_slugs)
                    log.info(
                        "Switching to camera '%s' (%s).",
                        camera_slugs[cycle_index],
                        config.camera_page_for(camera_slugs[cycle_index]),
                    )
                    fetch_start = time.monotonic()
                    fresh_url, cycle_index = await _fetch_fresh_url(
                        browser, config, camera_slugs, cycle_index
                    )
                    last_fetch_seconds = time.monotonic() - fetch_start
                    log.info("Fresh URL fetch took %.1f seconds.", last_fetch_seconds)
                    if fresh_url:
                        current_url = fresh_url
                        continue  # update OBS + re-monitor in the next iteration

                if reason == "url-expiring":
                    # The signed URL is about to expire: fetch a fresh one
                    # immediately (no retry sleep) and push it to OBS so
                    # playback continues without stopping.
                    log.info("Signed URL about to expire; fetching a fresh URL now.")
                    fetch_start = time.monotonic()
                    fresh_url, cycle_index = await _fetch_fresh_url(
                        browser, config, camera_slugs, cycle_index
                    )
                    last_fetch_seconds = time.monotonic() - fetch_start
                    log.info("Fresh URL fetch took %.1f seconds.", last_fetch_seconds)
                    if fresh_url:
                        current_url = fresh_url
                        continue  # update OBS + re-monitor in the next iteration

                # VLC exited or the refresh failed – full recovery
                log.info("VLC monitor requested recovery: %s", reason)
                player.stop()

                # Wait before trying to get a fresh URL
                await asyncio.sleep(config.retry_seconds)

                # Fetch a new signed URL from the camera page
                fetch_start = time.monotonic()
                current_url, cycle_index = await _fetch_fresh_url(
                    browser, config, camera_slugs, cycle_index
                )
                last_fetch_seconds = time.monotonic() - fetch_start
                log.info("Fresh URL fetch took %.1f seconds.", last_fetch_seconds)

            except Exception:
                log.exception(
                    "Main loop error. Retrying in %s seconds.",
                    config.retry_seconds,
                )
                player.stop()
                await asyncio.sleep(config.retry_seconds)
                current_url = None

    finally:
        # Clean up
        log.info("Stopping application.")
        player.stop()
        await browser.close()
        if obs_updater:
            await obs_updater.disconnect()


if __name__ == "__main__":
    asyncio.run(main())