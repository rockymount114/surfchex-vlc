import asyncio
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qs, urlparse

from config import Config


def stream_expiration_epoch(url: str) -> Optional[int]:
    """
    Read the common ?e=<unix timestamp> value used by signed URLs.
    Returns None if the URL has no usable e parameter.
    """
    try:
        query = parse_qs(urlparse(url).query)
        values = query.get("e")

        if not values:
            return None

        return int(values[0])
    except (ValueError, TypeError):
        return None


class VLCPlayer:
    def __init__(self, config: Config):
        self.config = config
        self.log = logging.getLogger("surfchex.vlc")
        self.process: Optional[subprocess.Popen] = None

    def start(self, stream_url: str) -> None:
        self.stop()

        if not os.path.exists(self.config.vlc_path):
            raise FileNotFoundError(
                f"VLC executable not found: {self.config.vlc_path}"
            )

        args = [
            self.config.vlc_path,
            "--intf", "qt",
            "--network-caching", str(self.config.vlc_network_caching_ms),
            "--no-video-title-show",
            "--no-sout-keep",
        ]

        args.extend(self.config.vlc_extra_args)
        args.append(stream_url)

        self.log.info("Starting VLC.")

        self.process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            if os.name == "nt"
            else 0,
        )

        self.log.info("VLC PID=%s", self.process.pid)

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    async def monitor(
        self,
        current_url: str,
        stop_event: asyncio.Event,
        refresh_before_seconds: int,
        check_interval: int,
    ) -> str:
        expiration = stream_expiration_epoch(current_url)

        if expiration:
            expiry_text = datetime.fromtimestamp(
                expiration,
                tz=timezone.utc,
            ).isoformat()

            self.log.info(
                "Signed URL expiration: %s UTC",
                expiry_text,
            )

        while not stop_event.is_set():

            if not self.is_running():
                return "vlc-exited"

            if expiration:
                now = int(time.time())
                remaining = expiration - now

                if remaining <= refresh_before_seconds:
                    self.log.info(
                        "Signed URL expires in %s seconds; refreshing.",
                        max(0, remaining),
                    )
                    return "url-expiring"

            await asyncio.sleep(check_interval)

        return "shutdown"

    def stop(self) -> None:
        if not self.process:
            return

        if self.process.poll() is None:
            self.log.info("Stopping VLC PID=%s", self.process.pid)

            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.log.warning("VLC did not exit; killing process.")
                self.process.kill()
                self.process.wait(timeout=5)
            except Exception:
                self.log.exception("Error stopping VLC.")

        self.process = None
