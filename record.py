"""
錄影工具：錄下整個遊戲畫面（含對手和球桌），給「追蹤球」模式分析球的飛行用。
可以和 bot.py 同時開：bot.py 負責打球，這個只負責錄。

用法：
  python record.py            第一次會先框選整個遊戲畫面（存到 config.json 的 track_region）
  python record.py --select   重新框選
  按 F2 開始錄 2 秒，錄完自動存到 debug_shots/record_<時間>/，可以重複按
  在這個視窗按 Ctrl+C 結束
"""
import argparse
import ctypes
import threading
import time
from pathlib import Path

import cv2
import dxcam
import keyboard

from bot import load_config, save_config

RECORD_SECONDS = 2.0


def select_region(cam, cfg):
    print("切到遊戲畫面，按 F2 截圖後框選「整個遊戲畫面」（要包含對手和整張球桌）")
    keyboard.wait("f2")
    frame = None
    while frame is None:
        frame = cam.grab(region=(0, 0, cam.width, cam.height))
    win = "Select WHOLE game area (incl. opponent), then ENTER"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    x, y, w, h = cv2.selectROI(win, frame, showCrosshair=False)
    cv2.destroyWindow(win)
    if w == 0 or h == 0:
        raise SystemExit("沒有框選")
    cfg["track_region"] = [int(x), int(y), int(x + w), int(y + h)]
    save_config(cfg)
    print(f"已儲存錄影範圍 {cfg['track_region']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--select", action="store_true", help="重新框選錄影範圍")
    args = ap.parse_args()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()

    cfg = load_config()
    cam = dxcam.create(output_idx=cfg["output_idx"], output_color="BGR")
    if args.select or not cfg.get("track_region"):
        select_region(cam, cfg)

    shots_dir = Path(__file__).with_name("debug_shots")
    go = threading.Event()
    keyboard.add_hotkey("f2", go.set)
    print(f"就緒：按 F2 錄 {RECORD_SECONDS:.0f} 秒（球速快的時候按），Ctrl+C 結束")
    try:
        while True:
            go.wait()
            go.clear()
            print("● 錄影中…")
            cam.start(region=tuple(cfg["track_region"]), target_fps=144)
            frames = []
            t0 = time.perf_counter()
            while (now := time.perf_counter()) - t0 < RECORD_SECONDS:
                frames.append((now - t0, cam.get_latest_frame().copy()))
            cam.stop()
            fps = len(frames) / RECORD_SECONDS
            folder = shots_dir / ("record_" + time.strftime("%H%M%S"))
            folder.mkdir(parents=True, exist_ok=True)
            for i, (t, f) in enumerate(frames):
                # cv2.imwrite 不支援中文路徑，改用 imencode + tofile
                cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tofile(
                    str(folder / f"{i:03d}_{t * 1000:05.0f}ms.jpg"))
            print(f"■ 已存 {len(frames)} 張（{fps:.0f} FPS）到 debug_shots/{folder.name}，可再按 F2")
            go.clear()  # 存檔期間誤按的不算
    except KeyboardInterrupt:
        pass
    finally:
        cam.stop()


if __name__ == "__main__":
    main()
