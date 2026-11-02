# WebKVM

用 USB 擷取卡（MS2130 / MS2131）加 CH340‑CH9329 連接線，透過瀏覽器控制另一台電腦。

```
被控電腦 ── HDMI ──▶ 擷取卡 (YUYV) ──USB3──▶ 本機 ffmpeg (H.264) ──WebSocket──▶ 瀏覽器 (WebCodecs)
被控電腦 ◀─ USB HID ── CH9329 ◀─ UART ─ CH340 ◀── USB ── 本機 ◀──WebSocket── 瀏覽器（鍵盤/滑鼠）
```

## 需求

- Linux、Python ≥ 3.11、`ffmpeg`、`v4l2-ctl`（v4l-utils）、`openssl`
- Python 套件：`aiohttp`、`pyserial`（`pacman -S python-aiohttp python-pyserial` 或 `uv sync`）
- 使用者需在 `video`（擷取卡）與 `uucp`（Arch）/ `dialout`（Debian 系）群組
- 有 Intel/AMD GPU 時使用 VAAPI 硬體編碼，否則自動改用 libx264

### 擷取卡

只使用 **YUYV 4:2:2 未壓縮** 擷取，避開擷取卡內建 MJPEG 的壓縮失真；因此需要能在 USB3 下輸出 YUYV 1080p60 的
MS2130 / MS2131。MS2109（USB2）的 YUYV 頻寬不足，不支援。

## 執行

```bash
cd ~/webkvm
python3 -m webkvm            # 或 uv run -m webkvm
```

第一次啟動會在終端機印出隨機產生的登入密碼，之後可改：

```bash
python3 -m webkvm passwd
```

開啟 `https://<本機 IP>:8443`。憑證是自簽的，第一次需在瀏覽器中接受警告。
WebCodecs 只在 HTTPS（或 localhost）下可用，所以請不要關掉 TLS，除非前面另有 HTTPS 反向代理。

設定檔：複製 `config.example.toml` 為 `config.toml`。密碼雜湊、憑證與 WebUI 中調整的影像設定都存在 `data/`。

## 使用

- **點一下畫面** 開始擷取鍵盤與滑鼠；離開畫面（失去焦點）時會自動放開所有按鍵。
- **絕對滑鼠**：本機游標在哪，被控端游標就在哪。BIOS 或部分遊戲不支援絕對座標時改用 **相對滑鼠**（Pointer Lock，按 Esc 解除）。
- **全螢幕** 時會鎖定鍵盤（Chromium），Esc、Alt+Tab、Win 鍵也會送到被控端；長按 Esc 離開全螢幕。
- **貼上文字** 以 US 鍵盤配置逐字輸入，只支援 ASCII。
- 工具列可切換擷取裝置、YUYV 解析度/更新率與位元率，會立即套用並保存。

## 設計說明

- **影像**：ffmpeg 從 V4L2 讀 YUYV → VAAPI `h264_vaapi`（或 libx264 ultrafast/zerolatency）→ Annex‑B 輸出到 stdout。
  伺服器以 AUD 切出每個影格，加上 9 位元組標頭（flags + 時間戳）後經 WebSocket 送出；瀏覽器用 `VideoDecoder` 解碼後畫到 canvas。
  沒有觀看者 5 秒後停止 ffmpeg，釋放擷取卡。客戶端跟不上時丟棄到下一個關鍵影格（預設每秒一個）。
- **HID**：CH9329 協定（`57 AB addr cmd len data sum`），所有序列埠 I/O 在單一執行緒上；
  滑鼠移動會合併成最新一筆，避免序列埠塞車。每秒查詢一次晶片狀態（被控端 USB 連線、Num/Caps/Scroll 燈號）。
- **安全**：scrypt 雜湊密碼、記憶體內 session（重啟伺服器需重新登入）、Cookie `HttpOnly; Secure; SameSite=Strict`、
  WebSocket 與 POST 檢查 Origin、同一 IP 5 分鐘內失敗 5 次即鎖定。
