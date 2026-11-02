"""擷取卡 HDMI 音訊 (ALSA) → ffmpeg Opus → 以 Opus 封包為單位分送。"""
import logging
import re

from .pipeline import FFmpegPipeline, PipelineError

log = logging.getLogger(__name__)


def find_alsa_device():
    """在 /proc/asound/cards 找 MACROSILICON 擷取卡，回傳 hw:CARD=<id>,DEV=0。"""
    try:
        with open("/proc/asound/cards") as f:
            text = f.read()
    except OSError:
        return None
    # 每張卡兩行：" 1 [Video          ]: USB-Audio - USB3 Video" + "  MACROSILICON USB3 Video at ..."
    for m in re.finditer(r"^\s*\d+ \[(\S+)\s*\]: (.*)\n\s+(.*)$", text, re.MULTILINE):
        if "MACROSILICON" in (m.group(2) + m.group(3)).upper():
            return f"hw:CARD={m.group(1)},DEV=0"
    return None


def build_cmd(cfg, device):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-fflags", "nobuffer"]
    if device.startswith("pulse:"):
        cmd += ["-f", "pulse", "-i", device.removeprefix("pulse:")]
    else:
        cmd += ["-f", "alsa", "-ch_layout", "stereo", "-sample_rate", "48000", "-i", device]
    cmd += ["-ac", "2", "-ar", "48000", "-c:a", "libopus", "-b:a", f"{int(cfg.bitrate)}k",
            "-application", "lowdelay", "-frame_duration", "20",
            "-page_duration", "20000", "-flush_packets", "1", "-f", "ogg", "-"]
    return cmd


def ogg_opus_splitter():
    """從 Ogg 串流取出 Opus 封包（略過 OpusHead / OpusTags 標頭）。"""
    buf = bytearray()
    pkt = bytearray()

    def feed(chunk):
        buf.extend(chunk)
        out = []
        while len(buf) >= 27:
            if buf[:4] != b"OggS":
                i = buf.find(b"OggS", 1)
                if i < 0:
                    del buf[:-3]
                    break
                del buf[:i]
                continue
            hlen = 27 + buf[26]
            if len(buf) < hlen:
                break
            segs = buf[27:hlen]
            total = hlen + sum(segs)
            if len(buf) < total:
                break
            pos = hlen
            for s in segs:
                pkt.extend(buf[pos:pos + s])
                pos += s
                if s < 255:  # lacing 值小於 255 代表封包結束，否則延續到下一段
                    p = bytes(pkt)
                    pkt.clear()
                    if not p.startswith((b"OpusHead", b"OpusTags")):
                        out.append((p, True))
            del buf[:total]
        return out

    return feed


class AudioPipeline(FFmpegPipeline):
    name = "audio"

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.device = None

    def describe(self):
        return {"enabled": self.cfg.enabled, "device": self.cfg.device,
                "resolved": self.device, "bitrate": self.cfg.bitrate,
                "running": self.running, "error": self.last_error}

    def _command(self):
        if not self.cfg.enabled:
            raise PipelineError("音訊已在設定檔中停用")
        self.device = find_alsa_device() if self.cfg.device == "auto" else self.cfg.device
        if not self.device:
            raise PipelineError("找不到擷取卡的音訊裝置")
        return build_cmd(self.cfg, self.device)

    def _info(self):
        return {"device": self.device, "sampleRate": 48000, "channels": 2, "codec": "opus"}

    def _parser(self):
        return ogg_opus_splitter()
