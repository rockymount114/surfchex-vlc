import asyncio
import logging
from typing import Optional
import obswebsocket
from obswebsocket import requests as obs_requests


class OBSUpdater:
    def __init__(self, config):
        self.config = config
        self.log = logging.getLogger("surfchex.obs")
        self.ws: Optional[obswebsocket.obsws] = None

    async def connect(self):
        if not self.config.obs_enabled:
            return
        self.ws = obswebsocket.obsws(
            self.config.obs_host,
            self.config.obs_port,
            self.config.obs_password
        )
        try:
            # Run the blocking connect() in a thread
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.ws.connect)
            self.log.info("Connected to OBS WebSocket")
        except Exception as e:
            self.log.error("Failed to connect to OBS: %s", e)
            self.ws = None

    async def update_stream_url(self, url: str):
        if not self.ws or not self.config.obs_enabled:
            return
        try:
            loop = asyncio.get_running_loop()
            # Get current settings (blocking)
            response = await loop.run_in_executor(
                None,
                lambda: self.ws.call(
                    obs_requests.GetInputSettings(
                        inputName=self.config.obs_source_name
                    )
                )
            )
            settings = response.datain.get("inputSettings", {})
            settings["url"] = url
            # Apply updated settings (blocking)
            await loop.run_in_executor(
                None,
                lambda: self.ws.call(
                    obs_requests.SetInputSettings(
                        inputName=self.config.obs_source_name,
                        inputSettings=settings,
                        overlay=True
                    )
                )
            )
            self.log.info("Updated OBS source '%s' with new URL", self.config.obs_source_name)
        except Exception as e:
            self.log.error("Failed to update OBS source: %s", e)

    async def disconnect(self):
        if self.ws:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.ws.disconnect)
                self.log.info("Disconnected from OBS WebSocket")
            except Exception as e:
                self.log.warning("Error during OBS disconnect: %s", e)