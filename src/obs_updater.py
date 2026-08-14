import asyncio
import logging
from typing import Optional
import obswebsocket
from obswebsocket import requests as obs_requests


class OBSUpdater:
    """Keeps a VLC Video Source (or Media Source) in OBS playing the current URL.

    IMPORTANT: obs-websocket-py does *not* raise when OBS rejects a request.
    A failed request only sets ``request.status = False`` (e.g. when a source
    or scene item does not exist).  The original code treated "no exception"
    as success, so a missing 'Fishing Pier' source was logged as "already
    exists" and was never actually created.  Every call here checks
    ``status`` so "not found" is handled as such.
    """

    VLC_KIND = "vlc_source"
    MEDIA_KIND = "ffmpeg_source"

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
            self.config.obs_password,
        )
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.ws.connect)
            self.log.info("Connected to OBS WebSocket")
        except Exception as e:
            self.log.error("Failed to connect to OBS: %s", e)
            self.ws = None

    async def _call(self, request):
        """Run an OBS request and raise if OBS rejected it (status=False)."""
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, lambda: self.ws.call(request))
        if not response.status:
            raise RuntimeError(
                "OBS rejected request '%s' (the source/scene may not exist)."
                % response.name
            )
        return response

    async def _get_current_scene(self) -> Optional[str]:
        try:
            response = await self._call(obs_requests.GetCurrentProgramScene())
            return response.datain.get("sceneName")
        except Exception as e:
            self.log.error("Could not get current program scene: %s", e)
            return None

    async def _get_source_kind(self) -> Optional[str]:
        """Return the input kind of the configured source, or None if it does not exist."""
        try:
            response = await self._call(
                obs_requests.GetInputSettings(inputName=self.config.obs_source_name)
            )
            return response.datain.get("inputKind")
        except Exception:
            return None

    async def _create_vlc_source(self, url: str, scene_name: Optional[str]) -> bool:
        """Create a VLC Video Source (optionally inside the current scene)."""
        try:
            kwargs = {
                "inputName": self.config.obs_source_name,
                "inputKind": self.VLC_KIND,
                "inputSettings": {"playlist": [{"value": url}]},
                "sceneItemEnabled": True,
            }
            if scene_name:
                kwargs["sceneName"] = scene_name

            response = await self._call(obs_requests.CreateInput(**kwargs))
            self.log.info(
                "Created VLC Video Source '%s'%s (scene item ID=%s).",
                self.config.obs_source_name,
                " in scene '%s'" % scene_name if scene_name else "",
                response.datain.get("sceneItemId"),
            )
            return True
        except Exception as e:
            self.log.error(
                "Failed to create VLC Video Source '%s': %s",
                self.config.obs_source_name,
                e,
            )
            return False

    async def _ensure_source(self, url: str, scene_name: Optional[str]):
        """Ensure the source exists; create it when missing.

        Returns ``(exists, kind, created)`` where ``kind`` is the OBS input
        kind and ``created`` is True when this call created the source.
        """
        kind = await self._get_source_kind()
        if kind is not None:
            self.log.info(
                "Source '%s' already exists (kind=%s).",
                self.config.obs_source_name,
                kind,
            )
            return True, kind, False

        self.log.info(
            "Source '%s' not found in OBS. Creating a new VLC Video Source.",
            self.config.obs_source_name,
        )
        if not await self._create_vlc_source(url, scene_name):
            return False, None, False
        return True, self.VLC_KIND, True

    async def _fit_item_to_canvas(self, scene_name: str, item_id: int) -> None:
        """Center a newly added scene item and scale it to fit the canvas."""
        try:
            video = await self._call(obs_requests.GetVideoSettings())
            width = float(video.datain.get("baseWidth") or 0)
            height = float(video.datain.get("baseHeight") or 0)
            if width <= 0 or height <= 0:
                return
            transform = {
                "positionX": 0.0,
                "positionY": 0.0,
                "rotation": 0.0,
                "boundsType": "OBS_BOUNDS_SCALE_INNER",
                "boundsWidth": width,
                "boundsHeight": height,
                "boundsAlignment": 4,  # OBS_ALIGN_CENTER
            }
            await self._call(
                obs_requests.SetSceneItemTransform(
                    sceneName=scene_name,
                    sceneItemId=item_id,
                    sceneItemTransform=transform,
                )
            )
            self.log.info(
                "Fitted source '%s' to canvas (%.0fx%.0f).",
                self.config.obs_source_name,
                width,
                height,
            )
        except Exception as e:
            self.log.warning("Could not fit source to canvas: %s", e)

    async def _ensure_source_in_scene(self, scene_name: Optional[str]) -> Optional[int]:
        """Ensure the source is in the scene and visible.

        Returns the scene item ID, or None if it could not be ensured.
        """
        if not scene_name:
            return None

        # Already in the scene -> just make sure it is visible.
        try:
            response = await self._call(
                obs_requests.GetSceneItemId(
                    sceneName=scene_name,
                    sourceName=self.config.obs_source_name,
                )
            )
            item_id = response.datain.get("sceneItemId")
            await self._call(
                obs_requests.SetSceneItemEnabled(
                    sceneName=scene_name,
                    sceneItemId=item_id,
                    sceneItemEnabled=True,
                )
            )
            self.log.info(
                "Source '%s' is in scene '%s' (ID=%s); visibility ensured.",
                self.config.obs_source_name,
                scene_name,
                item_id,
            )
            return item_id
        except Exception:
            pass  # not in the scene -> add it below

        try:
            response = await self._call(
                obs_requests.CreateSceneItem(
                    sceneName=scene_name,
                    sourceName=self.config.obs_source_name,
                    sceneItemEnabled=True,
                )
            )
            item_id = response.datain.get("sceneItemId")
            self.log.info(
                "Added source '%s' to scene '%s' (new ID=%s).",
                self.config.obs_source_name,
                scene_name,
                item_id,
            )
            return item_id
        except Exception as e:
            self.log.error(
                "Failed to add source '%s' to scene '%s': %s",
                self.config.obs_source_name,
                scene_name,
                e,
            )
            return None

    def _settings_for(self, kind: str, url: str) -> Optional[dict]:
        """Build the input settings for the URL, depending on the source kind."""
        if kind == self.VLC_KIND:
            return {"playlist": [{"value": url}]}
        if kind == self.MEDIA_KIND:
            return {"input": url, "is_local_file": False}
        return None

    async def update_stream_url(self, url: str) -> None:
        if not self.ws or not self.config.obs_enabled:
            return
        try:
            scene_name = await self._get_current_scene()

            exists, kind, created = await self._ensure_source(url, scene_name)
            if not exists or not kind:
                self.log.error(
                    "Cannot update OBS source '%s': it does not exist and could not be created.",
                    self.config.obs_source_name,
                )
                return

            item_id = await self._ensure_source_in_scene(scene_name)

            # Newly created sources have a default top-left, 1:1 transform;
            # fit them to the canvas so they are immediately visible.
            if created and item_id is not None and scene_name:
                await self._fit_item_to_canvas(scene_name, item_id)

            settings = self._settings_for(kind, url)
            if settings is None:
                self.log.error(
                    "Source '%s' has kind '%s', which this app cannot drive. "
                    "Delete it in OBS (or rename it) and let the app recreate it "
                    "as a VLC Video Source ('vlc_source').",
                    self.config.obs_source_name,
                    kind,
                )
                return

            await self._call(
                obs_requests.SetInputSettings(
                    inputName=self.config.obs_source_name,
                    inputSettings=settings,
                    overlay=True,
                )
            )
            self.log.info(
                "Updated %s source '%s' with new URL: %s",
                kind,
                self.config.obs_source_name,
                url,
            )

            await self._call(
                obs_requests.TriggerMediaInputAction(
                    inputName=self.config.obs_source_name,
                    mediaAction="OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART",
                )
            )
            self.log.info(
                "Restarted playback for source '%s'.",
                self.config.obs_source_name,
            )

            # Diagnostic: give the source a moment to open the stream, then
            # report the resulting media state.
            await asyncio.sleep(2)
            try:
                status = await self._call(
                    obs_requests.GetMediaInputStatus(
                        inputName=self.config.obs_source_name
                    )
                )
                state = status.datain.get("mediaState")
                self.log.info(
                    "Media state for source '%s': %s",
                    self.config.obs_source_name,
                    state,
                )
                if state == "OBS_MEDIA_STATE_ERROR":
                    self.log.warning(
                        "Source '%s' reports an error. Check that OBS's "
                        "VLC plugin can open the URL.",
                        self.config.obs_source_name,
                    )
            except Exception as e:
                self.log.warning("Could not read media state: %s", e)

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
