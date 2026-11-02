import asyncio
import contextlib
import json
import logging
import pathlib
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from . import auth, config
from .audio import AudioPipeline
from .ch9329 import CH9329
from .pipeline import FFmpegPipeline
from .video import VideoPipeline, list_devices, list_modes

log = logging.getLogger(__name__)

STATIC = pathlib.Path(__file__).parent / "static"
COOKIE = "webkvm_session"
PUBLIC_PATHS = ("/login",)
NO_CACHE = {"Cache-Control": "no-store"}

K_CFG = web.AppKey("cfg", config.Config)
K_DATA = web.AppKey("data_dir", pathlib.Path)
K_PWHASH = web.AppKey("pw_hash", str)
K_SECURE = web.AppKey("secure", bool)
K_SESSIONS = web.AppKey("sessions", auth.Sessions)
K_LIMITER = web.AppKey("limiter", auth.LoginLimiter)
K_HID = web.AppKey("hid", CH9329)
K_VIDEO = web.AppKey("video", VideoPipeline)
K_AUDIO = web.AppKey("audio", AudioPipeline)
K_HID_WS = web.AppKey("hid_ws", set)


def _same_origin(request):
    origin = request.headers.get("Origin")
    return origin is None or urlsplit(origin).netloc == request.host


@web.middleware
async def auth_middleware(request, handler):
    path = request.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await handler(request)
    if not request.app[K_SESSIONS].valid(request.cookies.get(COOKIE)):
        if path.startswith(("/api/", "/ws/")):
            raise web.HTTPUnauthorized()
        raise web.HTTPFound("/login")
    # 擋掉跨站 WebSocket 與跨站 POST
    if (request.method != "GET" or path.startswith("/ws/")) and not _same_origin(request):
        raise web.HTTPForbidden()
    return await handler(request)


# ---------- 頁面與登入 ----------

async def index(request):
    return web.FileResponse(STATIC / "index.html", headers=NO_CACHE)


async def login_page(request):
    return web.FileResponse(STATIC / "login.html", headers=NO_CACHE)


async def login(request):
    app = request.app
    ip = request.remote or "?"
    if not _same_origin(request):
        raise web.HTTPForbidden()
    if app[K_LIMITER].blocked(ip):
        raise web.HTTPFound("/login?e=locked")
    form = await request.post()
    pw = str(form.get("password", ""))
    ok = await asyncio.get_running_loop().run_in_executor(
        None, auth.verify_password, pw, app[K_PWHASH])
    if not ok:
        app[K_LIMITER].fail(ip)
        log.warning("登入失敗: %s", ip)
        raise web.HTTPFound("/login?e=bad")
    app[K_LIMITER].reset(ip)
    log.info("登入成功: %s", ip)
    resp = web.HTTPFound("/")
    resp.set_cookie(COOKIE, app[K_SESSIONS].create(), httponly=True, secure=app[K_SECURE],
                    samesite="Strict", max_age=int(app[K_SESSIONS].ttl))
    raise resp


async def logout(request):
    request.app[K_SESSIONS].revoke(request.cookies.get(COOKIE))
    resp = web.HTTPFound("/login")
    resp.del_cookie(COOKIE)
    raise resp


# ---------- API ----------

async def api_state(request):
    video = request.app[K_VIDEO]
    desc = video.describe()
    loop = asyncio.get_running_loop()
    devices = await loop.run_in_executor(None, list_devices)
    modes = await loop.run_in_executor(None, list_modes, desc["resolved"])
    return web.json_response({"video": desc, "devices": devices, "modes": modes,
                              "audio": request.app[K_AUDIO].describe(),
                              "hid": request.app[K_HID].status()}, headers=NO_CACHE)


async def api_video(request):
    app = request.app
    video = app[K_VIDEO]
    try:
        body = await request.json()
        device = str(body.get("device", video.cfg.device))
        width, height, fps = int(body["width"]), int(body["height"]), int(body["fps"])
        bitrate = max(500, min(50000, int(body.get("bitrate", video.cfg.bitrate))))
    except (ValueError, KeyError, TypeError):
        raise web.HTTPBadRequest(text="參數錯誤")

    loop = asyncio.get_running_loop()
    if device != "auto" and device not in {d["path"] for d in await loop.run_in_executor(None, list_devices)}:
        raise web.HTTPBadRequest(text="未知的擷取裝置")
    modes = await loop.run_in_executor(None, list_modes, device if device != "auto" else video.describe()["resolved"])
    if modes and not any(m["width"] == width and m["height"] == height and fps in m["fps"] for m in modes):
        raise web.HTTPBadRequest(text="裝置不支援此 YUYV 解析度/更新率")

    c = video.cfg
    c.device, c.width, c.height, c.fps, c.bitrate = device, width, height, fps, bitrate
    config.save_state(app[K_CFG], app[K_DATA])
    video.restart()
    return web.json_response(video.describe())


# ---------- WebSocket ----------

async def ws_video(request):
    return await _stream_ws(request, request.app[K_VIDEO])


async def ws_audio(request):
    return await _stream_ws(request, request.app[K_AUDIO])


async def _stream_ws(request, pipeline: FFmpegPipeline):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    client = pipeline.subscribe()

    async def pump():
        while True:
            item = await client.queue.get()
            if isinstance(item, str):
                await ws.send_str(item)
            else:
                await ws.send_bytes(item)

    sender = asyncio.create_task(pump())
    try:
        async for _ in ws:
            pass
    finally:
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError, ConnectionError):
            await sender
        pipeline.unsubscribe(client)
    return ws


def _handle_hid(hid, m):
    t = m["t"]
    if t == "kd":
        hid.key(m["c"], True)
    elif t == "ku":
        hid.key(m["c"], False)
    elif t == "ma":
        hid.mouse_abs(m["x"], m["y"], m.get("b", 0))
    elif t == "mr":
        hid.mouse_rel(m["dx"], m["dy"], m.get("b", 0))
    elif t == "mw":
        hid.mouse_wheel(m["d"])
    elif t == "combo":
        hid.combo(m["c"])
    elif t == "txt":
        return {"t": "typed", "skipped": hid.type_text(m["s"])}
    elif t == "release":
        hid.release_all()


async def ws_hid(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    hid = request.app[K_HID]
    clients = request.app[K_HID_WS]
    clients.add(ws)
    await ws.send_json({"t": "status", **hid.status()})
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                reply = _handle_hid(hid, json.loads(msg.data))
            except (ValueError, KeyError, TypeError) as e:
                log.debug("無效的 HID 訊息 %r: %s", msg.data[:200], e)
                continue
            if reply:
                await ws.send_json(reply)
    finally:
        clients.discard(ws)
        hid.release_all()
    return ws


async def _status_broadcaster(app):
    last = None
    while True:
        await asyncio.sleep(0.5)
        st = app[K_HID].status()
        if st != last:
            last = st
            for ws in list(app[K_HID_WS]):
                with contextlib.suppress(ConnectionError, RuntimeError):
                    await ws.send_json({"t": "status", **st})


async def _lifecycle(app):
    app[K_HID].start()
    task = asyncio.create_task(_status_broadcaster(app))
    yield
    task.cancel()
    await app[K_VIDEO].shutdown()
    await app[K_AUDIO].shutdown()
    await asyncio.get_running_loop().run_in_executor(None, app[K_HID].close)


def create_app(cfg, data_dir, pw_hash, secure):
    app = web.Application(middlewares=[auth_middleware])
    app[K_CFG] = cfg
    app[K_DATA] = data_dir
    app[K_PWHASH] = pw_hash
    app[K_SECURE] = secure
    app[K_SESSIONS] = auth.Sessions(cfg.server.session_hours)
    app[K_LIMITER] = auth.LoginLimiter()
    app[K_HID] = CH9329(cfg.hid.port, cfg.hid.baud, cfg.hid.address)
    app[K_VIDEO] = VideoPipeline(cfg.video)
    app[K_AUDIO] = AudioPipeline(cfg.audio)
    app[K_HID_WS] = set()
    app.cleanup_ctx.append(_lifecycle)
    app.add_routes([
        web.get("/", index),
        web.get("/login", login_page),
        web.post("/login", login),
        web.post("/logout", logout),
        web.get("/api/state", api_state),
        web.post("/api/video", api_video),
        web.get("/ws/video", ws_video),
        web.get("/ws/audio", ws_audio),
        web.get("/ws/hid", ws_hid),
        web.static("/static", STATIC),
    ])
    return app


def run(cfg, data_dir):
    pw_hash, new_pw = auth.load_or_init_password(data_dir)
    if new_pw:
        print(f"\n  已產生初始登入密碼: {new_pw}\n  可用 `python -m webkvm passwd` 修改。\n", flush=True)
    ssl_ctx = auth.ssl_context(data_dir, cfg.server.cert, cfg.server.key) if cfg.server.tls else None
    config.load_state(cfg, data_dir)
    app = create_app(cfg, data_dir, pw_hash, secure=ssl_ctx is not None)
    scheme = "https" if ssl_ctx else "http"
    log.info("WebKVM 監聽 %s://%s:%d", scheme, cfg.server.host, cfg.server.port)
    web.run_app(app, host=cfg.server.host, port=cfg.server.port, ssl_context=ssl_ctx,
                print=None, access_log=None)
