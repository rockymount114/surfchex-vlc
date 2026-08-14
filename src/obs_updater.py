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
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.ws.connect)
            self.log.info("Connected to OBS WebSocket")
        except Exception as e:
            self.log.error("Failed to connect to OBS: %s", e)
            self.ws = None

    async def _call(self, request):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.ws.call(request))

    async def _ensure_source_exists(self):
        """Create a VLC Video Source if it doesn't exist."""
        try:
            await self._call(
                obs_requests.GetInputSettings(
                    inputName=self.config.obs_source_name
                )
            )
            self.log.info("Source '%s' already exists.", self.config.obs_source_name)
            return True
        except Exception:
            self.log.info("Source '%s' not found. Creating new VLC Video Source.", self.config.obs_source_name)
            try:
                await self._call(
                    obs_requests.CreateInput(
                        inputName=self.config.obs_source_name,
                        inputKind="vlc_source",
                        inputSettings={"playlist": []},
                        sceneItemEnabled=True
                    )
                )
                self.log.info("Created VLC Video Source '%s'.", self.config.obs_source_name)
                return True
            except Exception as e:
                self.log.error("Failed to create VLC source: %s", e)
                return False

    async def _ensure_source_in_scene(self):
        """Ensure the source is added to the current program scene and visible."""
        try:
            # Get current program scene
            scene_response = await self._call(obs_requests.GetCurrentProgramScene())
            scene_name = scene_response.datain.get("sceneName")
            if not scene_name:
                self.log.warning("Could not get current program scene name.")
                return False

            # Check if source is already in the scene
            try:
                item_response = await self._call(
                    obs_requests.GetSceneItemId(
                        sceneName=scene_name,
                        sourceName=self.config.obs_source_name
                    )
                )
                item_id = item_response.datain.get("sceneItemId")
                self.log.info("Source '%s' already in scene '%s' (ID=%s).",
                              self.config.obs_source_name, scene_name, item_id)
                # Ensure it is enabled (visible)
                await self._call(
                    obs_requests.SetSceneItemEnabled(
                        sceneName=scene_name,
                        sceneItemId=item_id,
                        enabled=True
                    )
                )
                # Optionally bring to front (optional)
                # await self._call(obs_requests.SetSceneItemIndex(...))
                return True
            except Exception:
                # Source not in scene – add it
                self.log.info("Adding source '%s' to scene '%s'.", self.config.obs_source_name, scene_name)
                add_response = await self._call(
                    obs_requests.CreateSceneItem(
                        sceneName=scene_name,
                        sourceName=self.config.obs_source_name,
                        sceneItemEnabled=True  # visible
                    )
                )
                new_item_id = add_response.datain.get("sceneItemId")
                self.log.info("Added source '%s' to scene '%s' (new ID=%s).",
                              self.config.obs_source_name, scene_name, new_item_id)
                return True
        except Exception as e:
            self.log.error("Error ensuring source in scene: %s", e)
            return False

    async def update_stream_url(self, url: str):
        if not self.ws or not self.config.obs_enabled:
            return
        try:
            # Ensure the source exists
            if not await self._ensure_source_exists():
                return

            # Ensure it is added to current scene and visible
            if not await self._ensure_source_in_scene():
                self.log.warning("Source may not be visible; continuing anyway.")

            # Prepare the playlist with the new URL
            playlist = [{"value": url}]
            settings = {
                "playlist": playlist,
                "playlist_behavior": "always",
                "loop": False,
                "shuffle": False,
                "vlc_player": "default",
            }

            await self._call(
                obs_requests.SetInputSettings(
                    inputName=self.config.obs_source_name,
                    inputSettings=settings,
                    overlay=True
                )
            )
            self.log.info("Updated VLC source '%s' with new URL: %s", self.config.obs_source_name, url)

            # Restart playback
            await self._call(
                obs_requests.TriggerMediaInputAction(
                    inputName=self.config.obs_source_name,
                    mediaAction="OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"
                )
            )
            self.log.info("Restarted playback for VLC source '%s'.", self.config.obs_source_name)

        except Exception as e:
            self.log.error("Failed to update VLC source: %s", e)

    async def disconnect(self):
        if self.ws:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.ws.disconnect)
                self.log.info("Disconnected from OBS WebSocket")
            except Exception as e:
                self.log.warning("Error during OBS disconnect: %s", e)