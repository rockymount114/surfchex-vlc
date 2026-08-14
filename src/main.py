import asyncio
import logging
import signal
from pathlib import Path

from browser import StreamBrowser
from config import load_config
from vlc import VLCPlayer


def setup_logging(log_file: str) -> None:
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
    config = load_config()
    setup_logging(config.log_file)

    log = logging.getLogger("surfchex")
    stop_event = asyncio.Event()

    def request_stop(*_args):
        log.info("Shutdown requested.")
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    browser = StreamBrowser(config)
    player = VLCPlayer(config)

    try:
        await browser.start()

        # Optional initial URL. The application will still refresh it
        # from the configured SurfChex camera page when VLC fails or
        # the signed URL approaches expiration.
        current_url = config.initial_stream_url

        while not stop_event.is_set():
            try:
                if not current_url:
                    log.info("No stream URL available; opening camera page.")
                    current_url = await browser.get_fresh_stream_url()

                if not current_url:
                    log.warning(
                        "No .m3u8 request was captured. Retrying in %s seconds.",
                        config.retry_seconds,
                    )
                    await asyncio.sleep(config.retry_seconds)
                    continue

                log.info("Using stream URL: %s", current_url)

                player.start(current_url)

                # Monitor VLC and the signed URL expiration.
                reason = await player.monitor(
                    current_url=current_url,
                    stop_event=stop_event,
                    refresh_before_seconds=config.refresh_before_seconds,
                    check_interval=config.monitor_interval_seconds,
                )

                if stop_event.is_set():
                    break

                log.info("VLC monitor requested recovery: %s", reason)
                player.stop()

                await asyncio.sleep(config.retry_seconds)

                # Get a fresh signed URL from the normal web page.
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
        log.info("Stopping application.")
        player.stop()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
