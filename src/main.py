import asyncio
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

from config import load_config
from browser import StreamBrowser
from vlc import VLCPlayer, stream_expiration_epoch

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


async def _camera_info(browser, config, slug: str, cache: dict, log) -> dict:
    """Camera name + weather + tide for a slug, scraped at most once per day.

    ``cache`` maps slug -> {"date": "YYYY-MM-DD", "info": {...}}.
    """
    today = time.strftime("%Y-%m-%d")
    cached = cache.get(slug)
    if cached and cached.get("date") == today:
        return cached["info"]
    log.info("Scraping camera info for '%s'...", slug)
    info = await browser.scrape_camera_info() or {}
    if not info.get("name"):
        info["name"] = slug
    cache[slug] = {"date": today, "info": info}
    return info


def _is_obs_running() -> bool:
    """Return True if an OBS Studio process is running (Windows)."""
    if os.name != "nt":
        return False
    for image in ("obs64.exe", "obs.exe"):
        try:
            result = subprocess.run(
                ["tasklist", "/NH", "/FI", "IMAGENAME eq " + image],
                capture_output=True,
                timeout=10,
            )
            if image.encode() in result.stdout.lower():
                return True
        except Exception:
            return False
    return False


def _ask_start_obs() -> bool:
    """Ask the user whether to start OBS.  Returns True to start it."""
    while True:
        try:
            answer = input("OBS is not running. Start OBS now? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if answer in ("", "y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")


def _start_obs(config) -> bool:
    """Launch OBS Studio.  Returns True when the process was started.

    OBS must run with its own directory as the working directory, otherwise
    it fails with 'Failed to find locale/en-US.ini' (it locates its data
    files relative to the launch directory).
    """
    path = config.obs_path
    if not path or not os.path.exists(path):
        logging.getLogger("surfchex").warning(
            "OBS executable not found at '%s' — cannot start it.", path,
        )
        return False
    try:
        subprocess.Popen([path], cwd=os.path.dirname(path))
        logging.getLogger("surfchex").info("Started OBS: %s", path)
        return True
    except Exception as e:
        logging.getLogger("surfchex").error("Failed to start OBS: %s", e)
        return False


async def _wait_for_port(host: str, port: int, timeout: float = 60.0) -> bool:
    """Wait until a TCP connection to host:port succeeds (OBS starting up)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return True
        except OSError:
            await asyncio.sleep(2)
    return False


async def _connect_obs(config, log):
    """Connect to OBS; if it is not running, ask the user whether to start it.

    Returns the connected OBSUpdater, or None to continue without OBS.
    """
    updater = OBSUpdater(config)
    try:
        await updater.connect()
    except Exception as e:
        log.warning("Failed to connect to OBS WebSocket: %s. OBS updates disabled.", e)
        return None
    if updater.ws is not None:
        log.info("OBS WebSocket connected successfully.")
        return updater

    # OBS is not reachable over WebSocket.
    if _is_obs_running():
        log.warning(
            "OBS appears to be running, but its WebSocket server is not reachable "
            "on %s:%s. Is the WebSocket server enabled? Continuing without OBS.",
            config.obs_host, config.obs_port,
        )
        return None

    if not _ask_start_obs():
        log.info("Continuing without OBS integration.")
        return None

    if not _start_obs(config):
        log.warning("OBS could not be started. Continuing without OBS integration.")
        return None

    log.info("Waiting for OBS to start (up to 60 s)...")
    if not await _wait_for_port(config.obs_host, config.obs_port, timeout=60):
        log.warning("OBS did not start in time. Continuing without OBS integration.")
        return None

    # The port is up; give the WebSocket server a moment, then connect.
    for _ in range(5):
        await updater.connect()
        if updater.ws is not None:
            break
        await asyncio.sleep(1)
    if updater.ws is None:
        log.warning("Could not connect to OBS WebSocket. Continuing without OBS integration.")
        return None
    log.info("OBS WebSocket connected successfully.")
    return updater


def _format_weather(w: dict) -> str:
    """One-line weather summary for the OBS text overlay.

    All values go through ``_clean_text`` first, so missing or "--"
    placeholder values can never crash the formatting.
    """
    parts = []

    title = _clean_text(w.get("title"))
    if title:
        parts.append(title)

    temp_parts = []
    temp = _clean_text(w.get("temp"))
    if temp:
        temp_parts.append(temp)
    feels = _clean_text(w.get("feels"))
    if feels:
        temp_parts.append("feels %s°" % feels)
    if temp_parts:
        parts.append(" ".join(temp_parts))

    wind_dir = _clean_text(w.get("windDir"))
    wind_val = _clean_text(w.get("wind"))
    if wind_dir or wind_val:
        parts.append("Wind %s %s mph" % (wind_dir or "", wind_val or ""))

    gusts = _clean_text(w.get("gusts"))
    if gusts:
        parts.append("Gusts %s mph" % gusts)

    humidity = _clean_text(w.get("humidity"))
    if humidity:
        parts.append("Humidity %s" % humidity)

    pressure = _clean_text(w.get("pressure"))
    if pressure:
        parts.append("Pressure %s" % pressure)

    dew = _clean_text(w.get("dew"))
    if dew:
        parts.append("Dew %s" % dew)

    rain = _clean_text(w.get("rain"))
    if rain:
        parts.append("Rain %s" % rain)

    return " | ".join(parts) or "Weather unavailable"


def _format_tide(t: dict) -> str:
    """One-line tide summary for the OBS text overlay."""
    parts = []

    now = _clean_text(t.get("now"))
    if now:
        parts.append(now)

    next_event = _clean_text(t.get("nextEvent"))
    if next_event:
        nxt = next_event
        next_height = _clean_text(t.get("nextHeight"))
        if next_height:
            nxt = "%s (%s)" % (nxt, next_height)
        parts.append(nxt)

    station = _clean_text(t.get("station"))
    if station:
        parts.append(station)

    station_meta = _clean_text(t.get("stationMeta"))
    if station_meta:
        parts.append(station_meta)

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

    # Initialise OBS updater (optional).  If OBS is not running, the user is
    # asked whether to start it; the app continues either way.
    obs_updater = None
    if config.obs_enabled and OBSUpdater is not None:
        obs_updater = await _connect_obs(config, log)
    elif config.obs_enabled:
        log.warning("OBS integration enabled in config, but OBSUpdater is not available.")

    # Optional: force the OBS canvas resolution (e.g. 1080p) to cut GPU load.
    if obs_updater and config.obs_canvas_width > 0 and config.obs_canvas_height > 0:
        await obs_updater.set_canvas_resolution(
            config.obs_canvas_width, config.obs_canvas_height
        )

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
            return await _camera_info(browser, config, slug, site_info_cache, log)

        async def update_overlays(slug: str) -> None:
            """Push camera name / weather / tide to the OBS text sources.

            Runs on every camera change / URL refresh so the overlays always
            match the source currently shown in OBS.  Each overlay can be
            switched off in config.yaml (camera_location / camera_weather /
            camera_tide).  A failure in one source never stops the others.
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
            overlays = []
            if config.obs_camera_location:
                overlays.append((LOCATION_SOURCE, name))
            if config.obs_camera_weather:
                overlays.append((WEATHER_SOURCE, weather))
            if config.obs_camera_tide:
                overlays.append((TIDE_SOURCE, tide))

            for source_name, text in overlays:
                try:
                    await obs_updater.update_text_source(source_name, text)
                except Exception as e:
                    log.error("Overlay update failed for '%s': %s", source_name, e)

        # --- Prefetch the NEXT camera while the current one plays, so camera
        # switches are instant (the URL + info are already fetched and only
        # pushed to OBS at the rotation tick). ---
        prefetch_task: Optional[asyncio.Task] = None
        prefetched: dict = {}

        async def prefetch_camera(slug: str) -> None:
            """Fetch URL + info for a camera in the background; park the page
            afterwards so the browser stays idle until the next switch."""
            nonlocal prefetched
            try:
                url, _ = await _fetch_fresh_url(browser, config, [slug], 0)
                if not url:
                    log.warning("Prefetch: camera '%s' produced no stream.", slug)
                    return
                await camera_info(slug)  # scrapes now, cached for the day
                await browser.park_page()
                prefetched = {"slug": slug, "url": url}
                log.info("Prefetched camera '%s' (ready to switch).", slug)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Prefetch failed for camera '%s': %s", slug, e)

        def start_prefetch(slug: str) -> None:
            nonlocal prefetch_task, prefetched
            if prefetch_task and not prefetch_task.done():
                prefetch_task.cancel()
            prefetched = {}
            prefetch_task = asyncio.create_task(prefetch_camera(slug))

        def cancel_prefetch() -> None:
            nonlocal prefetch_task
            if prefetch_task and not prefetch_task.done():
                prefetch_task.cancel()

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
                    cancel_prefetch()
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

                # The URL is captured and OBS/VLC is playing it — unload the
                # camera page so its video/JS stop using CPU/GPU until the
                # next refresh (about:blank is reloaded by the next fetch).
                if config.park_page:
                    await browser.park_page()

                # Launch VLC with the stream URL (no-op when vlc_player=false)
                player.start(current_url)

                # Background-prefetch the NEXT camera so the next switch is
                # instant (URL + info ready before the rotation tick).
                if len(camera_slugs) > 1:
                    start_prefetch(
                        camera_slugs[(cycle_index + 1) % len(camera_slugs)]
                    )

                # Refresh early enough that OBS never runs out of a valid URL:
                # at least the configured margin, and at least twice the time
                # the last fetch took, plus 30 s of slack.  The margin is also
                # capped at ~25% of the URL's lifetime so a very short-lived
                # URL can never cause an immediate re-fetch loop.
                refresh_before = max(
                    config.refresh_before_seconds,
                    int(last_fetch_seconds) * 2 + 30,
                )
                url_lifetime = stream_expiration_epoch(current_url)
                if url_lifetime:
                    refresh_before = min(
                        refresh_before,
                        max(20, url_lifetime // 4),
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

                # The prefetch served its purpose (or it is moot now) — stop it
                # before acting on the monitor result.
                cancel_prefetch()

                if stop_event.is_set():
                    break

                if reason == "rotate":
                    # Time to move to the next camera.  Use the prefetched URL
                    # when it is ready; otherwise fall back to a live fetch.
                    cycle_index = (cycle_index + 1) % len(camera_slugs)
                    next_slug = camera_slugs[cycle_index]
                    log.info(
                        "Switching to camera '%s' (%s).",
                        next_slug,
                        config.camera_page_for(next_slug),
                    )
                    if prefetched.get("slug") == next_slug:
                        current_url = prefetched["url"]
                        prefetched = {}
                        continue  # update OBS + re-monitor in the next iteration

                    log.info("Prefetched URL not ready; fetching now.")
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
                cancel_prefetch()
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