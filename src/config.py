from dataclasses import dataclass
from pathlib import Path
import os
import yaml


BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    camera_page_url: str
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
    log_file: str


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def load_config(path: str | None = None) -> Config:
    config_path = Path(path or BASE_DIR / "config.yaml")

    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    return Config(
        camera_page_url=_expand(data["camera_page_url"]),
        initial_stream_url=_expand(data["initial_stream_url"])
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
            data.get("refresh_before_seconds", 60)
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
        log_file=_expand(data.get(
            "log_file",
            str(BASE_DIR / "logs" / "surfchex-vlc.log"),
        )),
    )
