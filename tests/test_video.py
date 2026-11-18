import struct

import pytest

from webkvm.config import VideoConfig
from webkvm.video import VideoPipeline, av1_is_keyframe, build_cmd, ivf_splitter


def test_default_qsv():
    cfg = VideoConfig()
    pipeline = VideoPipeline(cfg)
    cmd = pipeline._command()
    assert cfg.encoder == "qsv"
    assert cmd[cmd.index("-c:v") + 1] == "av1_qsv"
    assert cmd[-3:] == ["-f", "ivf", "-"]
    assert pipeline._info()["codec"] == "av01.0.08M.08"
    assert f"qsv=qs:hw,child_device={cfg.qsv_device}" in cmd


@pytest.mark.parametrize("encoder,codec", [("vaapi", "h264_vaapi"), ("libx264", "libx264")])
def test_h264_options(encoder, codec):
    cmd = build_cmd(VideoConfig(), "/dev/video0", encoder)
    assert cmd[cmd.index("-c:v") + 1] == codec
    assert cmd[-3:] == ["-f", "h264", "-"]


def test_ivf_fragmentation():
    header = struct.pack("<4sHH4sHHIIII", b"DKIF", 0, 32, b"AV01",
                         1920, 1080, 60, 1, 0, 0)
    key = b"\x12\x00\x0a\x01\x00\x32\x01\x00"
    delta = b"\x12\x00\x32\x01\x20"
    stream = header + struct.pack("<IQ", len(key), 0) + key
    stream += struct.pack("<IQ", len(delta), 1) + delta
    for chunk_size in (1, 7, 32, len(stream)):
        feed = ivf_splitter()
        frames = []
        for pos in range(0, len(stream), chunk_size):
            frames.extend(feed(stream[pos:pos + chunk_size]))
        assert frames == [(key, True), (delta, False)]


def test_truncated_obu():
    assert not av1_is_keyframe(b"\x0a\x80")
    assert not av1_is_keyframe(b"\x0a\x05\x00")


def test_invalid_ivf():
    with pytest.raises(ValueError):
        ivf_splitter()(bytes(32))
