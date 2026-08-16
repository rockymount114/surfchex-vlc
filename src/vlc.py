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
        self.enabled = config.vlc_player

    def start(self, stream_url: str) -> None:
        self.stop()

        if not self.enabled:
            self.log.info(
                "VLC player disabled (vlc_player=false); skipping local VLC "
                "and only updating the OBS source."
            )
            return

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
        rotate_after_seconds: Optional[float] = None,
    ) -> str:
        """Watch until VLC exits, the URL is about to expire, a camera rotation
        is due, or shutdown is requested.

        Returns one of: 'vlc-exited', 'url-expiring', 'rotate', 'shutdown'.
        """
        expiration = stream_expiration_epoch(current_url)
        rotate_deadline = (
            time.monotonic() + rotate_after_seconds
            if rotate_after_seconds
            else None
        )

        if expiration:
            expiry_text = datetime.fromtimestamp(
                expiration,
                tz=timezone.utc,
            ).isoformat()

            lifetime = expiration - int(time.time())
            self.log.info(
                "Signed URL valid for %s seconds (~%.1f min), expires %s UTC.",
                max(0, lifetime),
                max(0, lifetime) / 60.0,
                expiry_text,
            )

        if not self.enabled:
            self.log.info(
                "VLC player disabled; monitoring only the signed URL expiration "
                "(no VLC process to watch)."
            )

        while not stop_event.is_set():

            if self.enabled and not self.is_running():
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

            if rotate_deadline is not None and time.monotonic() >= rotate_deadline:
                self.log.info("Camera rotation interval reached.")
                return "rotate"

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
