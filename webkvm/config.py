import dataclasses
import json
import logging
import tomllib
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8443
    tls: bool = True
    cert: str = ""      # 留空則自動產生自簽憑證
    key: str = ""
    session_hours: float = 12


@dataclass
class VideoConfig:
    device: str = "auto"    # auto 或 /dev/v4l/by-id/... 路徑
    width: int = 1920
    height: int = 1080
    fps: int = 60
    encoder: str = "auto"   # auto | vaapi | libx264
    bitrate: int = 8000     # kbps
    gop: float = 1.0        # 關鍵影格間隔（秒），也是新觀看者最長的等待時間
    vaapi_device: str = "/dev/dri/renderD128"


@dataclass
class HidConfig:
    port: str = "auto"      # auto 或序列埠路徑
    baud: int = 115200
    address: int = 0


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    video: VideoConfig = field(default_factory=VideoConfig)
    hid: HidConfig = field(default_factory=HidConfig)


def _fill(obj, values, section):
    names = {f.name for f in dataclasses.fields(obj)}
    for k, v in values.items():
        if k in names:
            setattr(obj, k, v)
        else:
            log.warning("設定檔 [%s] 中未知的選項: %s", section, k)


def load(path):
    cfg = Config()
    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        for section in ("server", "video", "hid"):
            _fill(getattr(cfg, section), raw.get(section, {}), section)
    return cfg


# WebUI 中調整的影像設定存在 state.json，重啟後沿用
VIDEO_STATE_KEYS = ("device", "width", "height", "fps", "bitrate")


def load_state(cfg, data_dir):
    p = data_dir / "state.json"
    try:
        state = json.loads(p.read_text())
    except (OSError, ValueError):
        return
    for k in VIDEO_STATE_KEYS:
        if k in state.get("video", {}):
            setattr(cfg.video, k, state["video"][k])


def save_state(cfg, data_dir):
    state = {"video": {k: getattr(cfg.video, k) for k in VIDEO_STATE_KEYS}}
    (data_dir / "state.json").write_text(json.dumps(state, indent=2))
