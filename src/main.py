import asyncio
import logging
import signal
from pathlib import Path

from config import load_config
from browser import StreamBrowser
from vlc import VLCPlayer

# Try to import OBSUpdater; if missing, disable OBS integration.
try:
    from obs_updater import OBSUpdater
except ImportError:
    OBSUpdater = None
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

        # Use the initial URL if provided (may be None)
        current_url = config.initial_stream_url

        # If we have an initial URL and OBS is active, update OBS immediately.
        if current_url and obs_updater:
            log.info("Initial URL provided; sending to OBS.")
            await obs_updater.update_stream_url(current_url)

        # Main monitoring loop
        while not stop_event.is_set():
            try:
                # If no URL, fetch one from the camera page
                if not current_url:
                    log.info("No stream URL available; opening camera page.")
                    current_url = await browser.get_fresh_stream_url()

                # If still no URL, wait and retry
                if not current_url:
                    log.warning(
                        "No .m3u8 request captured. Retrying in %s seconds.",
                        config.retry_seconds,
                    )
                    await asyncio.sleep(config.retry_seconds)
                    continue

                log.info("Using stream URL: %s", current_url)

                # Update OBS with the new URL (with debug log)
                if obs_updater:
                    log.info("Sending updated URL to OBS source '%s'", config.obs_source_name)
                    await obs_updater.update_stream_url(current_url)

                # Launch VLC with the stream URL
                player.start(current_url)

                # Monitor VLC process and URL expiration
                reason = await player.monitor(
                    current_url=current_url,
                    stop_event=stop_event,
                    refresh_before_seconds=config.refresh_before_seconds,
                    check_interval=config.monitor_interval_seconds,
                )

                if stop_event.is_set():
                    break

                # VLC exited or URL expired – recover
                log.info("VLC monitor requested recovery: %s", reason)
                player.stop()

                # Wait before trying to get a fresh URL
                await asyncio.sleep(config.retry_seconds)

                # Fetch a new signed URL from the camera page
                current_url = await browser.get_fresh_stream_url()

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