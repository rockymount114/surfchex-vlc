from dataclasses import dataclass
from pathlib import Path
import os
import yaml
import time

BASE_DIR = Path(__file__).resolve().parent.parent

@dataclass
class Config:
    camera_page_url: str
    cameras: dict[str, str]
    camera: str
    camera_cycle_seconds: int
    initial_stream_url: str | None
    vlc_path: str
    headless: bool
    browser_profile_dir: str
    stream_url_contains: str
    stream_url_regex: str | None
    refresh_before_seconds: int
    retry_seconds: int
    monitor_interval_seconds: int
    page_timeout_seconds: int
    capture_timeout_seconds: int
    network_idle_wait_seconds: int
    vlc_network_caching_ms: int
    vlc_extra_args: list[str]
    vlc_player: bool
    log_file: str
    obs_enabled: bool
    obs_host: str
    obs_port: int
    obs_password: str
    obs_source_name: str

    def camera_slugs(self) -> list[str]:
        """Ordered camera slugs to use.

        With ``camera_cycle_seconds > 0`` the app cycles through every camera,
        starting from the configured default.  Otherwise only the default
        camera is used.  Returns an empty list when no ``cameras`` are set
        (legacy single ``camera_page_url`` mode).
        """
        if not self.cameras:
            return []
        slugs = list(self.cameras.keys())
        if self.camera and self.camera in slugs:
            slugs.remove(self.camera)
            slugs.insert(0, self.camera)
        if self.camera_cycle_seconds > 0:
            return slugs
        return [slugs[0]]

    def camera_page_for(self, slug: str) -> str:
        """Page URL for a camera slug (falls back to the legacy page URL)."""
        return self.cameras.get(slug) or self.camera_page_url

def expires():
    '''return a UNIX style timestamp representing 5 minutes from now'''
    return int(time.time()+300)

def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def _as_bool(value, default: bool = False) -> bool:
    """Parse a YAML/string value into a bool (handles quoted 'false' too)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def load_config(path: str | None = None) -> Config:
    config_path = Path(path or BASE_DIR / "config.yaml")

    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
        
    # Process initial_stream_url with placeholder replacement
    raw_initial = data.get("initial_stream_url")
    if raw_initial:
        raw_initial = _expand(raw_initial)
        raw_initial = raw_initial.replace("{expires}", str(expires()))
    else:
        raw_initial = None

    # init obs data
    obs_data = data.get("obs", {})
    
    return Config(
        camera_page_url=_expand(data["camera_page_url"]),
        # cameras: slug -> camera page URL; camera: default slug; cycle seconds
        cameras={
            str(k).strip(): _expand(str(v))
            for k, v in (data.get("cameras") or {}).items()
            if isinstance(v, str) and str(v).strip()
        },
        camera=str(data.get("camera", "")).strip(),
        camera_cycle_seconds=int(data.get("camera_cycle_seconds", 0)),
        initial_stream_url=raw_initial  
        if data.get("initial_stream_url")
        else None,
        vlc_path=_expand(data.get(
            "vlc_path",
            r"C:\Program Files\VideoLAN\VLC\vlc.exe",
        )),
        headless=bool(data.get("headless", True)),
        browser_profile_dir=_expand(data.get(
            "browser_profile_dir",
            str(BASE_DIR / "state" / "chromium-profile"),
        )),
        stream_url_contains=data.get(
            "stream_url_contains",
            "surfchex.com/hls/",
        ),
        stream_url_regex=data.get("stream_url_regex"),
        refresh_before_seconds=int(
            data.get("refresh_before_seconds", 120)
        ),
        retry_seconds=int(data.get("retry_seconds", 10)),
        monitor_interval_seconds=int(
            data.get("monitor_interval_seconds", 5)
        ),
        page_timeout_seconds=int(
            data.get("page_timeout_seconds", 60)
        ),
        capture_timeout_seconds=int(
            data.get("capture_timeout_seconds", 45)
        ),
        network_idle_wait_seconds=int(
            data.get("network_idle_wait_seconds", 5)
        ),
        vlc_network_caching_ms=int(
            data.get("vlc_network_caching_ms", 1500)
        ),
        vlc_extra_args=list(data.get("vlc_extra_args", [])),
        # true = open the local VLC window; false = only send the URL to OBS
        vlc_player=_as_bool(data.get("vlc_player", True)),
        log_file=_expand(data.get(
            "log_file",
            str(BASE_DIR / "logs" / "surfchex-vlc.log"),
        )),
        
        # obs data
        obs_enabled=bool(obs_data.get("enabled", False)),
        obs_host=_expand(obs_data.get("host", "localhost")),
        obs_port=int(obs_data.get("port", 4455)),
        obs_password=_expand(obs_data.get("password", "")),
        obs_source_name=obs_data.get("source_name", "SurfChex Stream"),
    )
    
    




