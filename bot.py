"""
MATDRIVE AI 挑戰模式 自動揮拍（僅限 AI 對戰 / 練習使用）

畫面判讀（依實際截圖量測）：
  - 擊球前球員旁出現光圈：外圈逐漸縮小，中間有一個灰色內圈（大小會變），兩者同圓心
  - 外圈藍色 = 反手、紅色 = 正手、半藍半紅 = 「Middle · either side」
  - 滑動起點的左右位置決定反手 / 正手（底部 Backhand ↔ Forehand 黃條跟著游標）
  - 手動出手時機約在 外圈半徑 ≈ 內圈 × 1.33

流程：
  截圖(dxcam) → 用藍/紅兩色找出外圈 → 從圓心往外找比周圍亮的灰色內圈，每幀量內圈半徑
  → 依顏色決定左右起點，按住往下拖引拍
  → 追蹤「外圈 / 內圈」比值的縮小速度，提前 lead_ms 往上滑揮拍並放開

熱鍵：
  F6  校準時擷取畫面（等光圈出現再按，或先按遊戲的 Pause）
  F7  測試揮拍一次（確認遊戲吃得到滑鼠拖曳）
  F8  開始 / 暫停
  F9  重新校準
  F10 結束
"""
import argparse
import ctypes
import ctypes.wintypes
import json
import queue
import threading
import time
import warnings
from collections import deque
from pathlib import Path

import cv2
import dxcam
import keyboard
import numpy as np

CONFIG_PATH = Path(__file__).with_name("config.json")

DEFAULT_CONFIG = {
    # 外圈顏色（OpenCV HSV，H 0~179），由截圖量得
    "blue_hsv": [106, 168, 248],
    "red_hsv": [172, 150, 240],
    # HSV 容差。藍圈(S≈168)和藍色桌面(S≈209)色相只差約 4，S 要收緊
    "hsv_tol": [4, 30, 70],
    # 紅圈有一段偏粉白(S≈128)，其餘 S≈166；紅色球拍 S≈218 要排除
    # 紅圈半透明，疊在紫色背景上色相會偏到 H≈167，所以 H 放寬到 164~180
    "red_tol": [8, 40, 70],
    # 上面的顏色找不到光圈時，改用寬鬆顏色並去掉球桌再找一次
    # （近鏡頭模式：光圈疊在紫色背景 H≈112、疊在黑衣服 V≈160，又貼著球桌）
    "ring_fallback": True,
    # 圓內紅橘色比例超過這個就當作是自己的紅色球拍（球拍約 1.0，疊上 PERFECT/BLOCK 字時降到約 0.45；
    # 真光圈 0.1 以下，光圈套在球拍上時約 0.2）
    "max_warm_fill": 0.3,
    "blue_hsv_alt": [109, 160, 200],
    "hsv_tol_alt": [7, 50, 105],
    "red_tol_alt": [10, 60, 110],
    # 出手時機：外圈 / 內圈 比值降到此值時揮拍（截圖量得手動約 1.33）
    # 揮太早就調小，揮太晚就調大
    "match_scale": 1.3,
    # 正手動作比反手慢，要更早出手（比值更大）。正手太慢就調大、太快就調小
    "match_scale_forehand": 1.4,
    # drive（全部正手拉球）專用的出手比值，和 block 分開調。太早就調小、太晚就調大
    "match_scale_drive": 1.45,
    # 提前量（毫秒），補償截圖 + 辨識延遲。太大會讓預估觸發的揮拍偏早
    "lead_ms": 8,
    # 比值大於 match_scale × 此倍率 才開始引拍，避免誤判
    "pull_trigger": 1.05,
    # 揮拍方式：tap（出手瞬間點一下，沒有引拍時間差）/ swipe（按住往下拖引拍，再往上滑）
    # / drive（全部正手拉球：一直保持正手引拍，出手往上推完立刻再引拍）
    "swing_mode": "tap",
    # drive 模式揮完放開後，隔多久再按下引拍（毫秒）。揮拍動畫還沒做完時按下會被遊戲忽略，
    # 之後一直按著也不會再引拍，所以要等動畫結束
    "repull_gap_ms": 250,
    # drive 模式左鍵放開超過這麼久（毫秒）就自動按下引拍：點完遊戲的開始後、或按住被打斷時補回來
    "auto_pull_ms": 400,
    # 自動發球：我方球員拍子右側出現上下浮動的白色桌球圖示 = 輪到我發球
    "auto_serve": True,
    # 白點要在差不多同一位置連續出現這麼久（毫秒）才發球；比賽中的真球長得一樣但飛很快，靠這個排除
    "serve_stable_ms": 500,
    # 圖示浮動範圍（像素），超過就當作是在飛的真球
    "serve_max_drift": 15,
    # 白點周圍要有這個比例是紫色背景；球員的白手套周圍是黑色輪廓，會被排除
    "serve_bg_ratio": 0.9,
    # 只在畫面這個高度比例以下找（我方半場），避開上方看板的白字
    "serve_y_min": 0.45,
    # 發球時引拍後停多久再揮（毫秒）
    "serve_hold_ms": 300,
    # 計時條小遊戲（NET CORD 等）：白色細棒左右移動，進到中間橘色區時自動點擊
    "auto_timing_bar": True,
    # 預估白棒位置時多提前多少（毫秒），補償截圖、辨識和點擊延遲。點太晚就調大、太早就調小
    "bar_lead_ms": 30,
    # 白棒預估位置落在橘色區中間這個比例內才點（0.6 = 中間 60%）
    "bar_window": 0.6,
    # tap 模式按住多久才放開（毫秒）
    "tap_hold_ms": 30,
    # 點擊 / 拖曳起點（遊戲區寬高比例）：反手在左、正手在右
    "backhand_x": 0.3,
    "forehand_x": 0.7,
    "swipe_y": 0.55,
    # 半藍半紅時用哪一手：forehand / backhand
    "middle_side": "forehand",
    # 單一顏色佔比超過此值才算純藍或純紅，否則視為 middle
    "side_ratio": 0.75,
    # 引拍：往下拖（遊戲區高度比例）、花費毫秒
    "pull_ratio": 0.15,
    "pull_ms": 60,
    # 揮拍：往上滑（遊戲區高度比例）、花費毫秒（越短越快）
    "swing_ratio": 0.4,
    "swing_ms": 40,
    # 揮拍後冷卻時間，避免同一球揮兩次
    "cooldown_ms": 400,
    # 揮拍後光圈要消失這麼久才準備下一球，避免揮完又對同一個光圈引拍
    "rearm_ms": 150,
    # 引拍後光圈消失超過此時間就放開滑鼠
    "lost_ms": 300,
    # 引拍中光圈突然消失，若最後比值 ≤ 目標 × 此倍率，就當作快到時機直接補揮
    "lost_swing_margin": 1.15,
    # 外圈半徑合理範圍（像素）。內圈約 20~25px、外圈不會比內圈小，
    # 下限 16 可排除對手的紅色球拍（約 10px）和選單上的小圖示（約 15px）
    "ring_r_range": [16, 300],
    # 輪廓點到圓心距離的標準差 / 半徑，超過就不是圓（排除桌面等）
    "max_roundness_err": 0.15,
    # 圓內被顏色填滿的比例上限：光圈是空心細線，實心的球拍會被排除
    "max_fill": 0.6,
    # 外圈線條至少要涵蓋圓周的這個比例：桌角、桌邊只是一段弧，會被排除
    "min_coverage": 0.75,
    # 比值離目標在此範圍內才用預估提早出手；光圈先快後慢縮小，離太遠時直線預估會太早
    "predict_window": 0.15,
    # 同一球內圈量測和目前中位數差超過此比例視為量錯，丟掉
    "inner_outlier": 0.15,
    # 外圈 / 內圈 低於此值就不是真光圈（誤認其他藍/紅圓形物體）
    "min_ratio": 0.95,
    # 比值只會越縮越小，單張突然變大超過此值視為量測雜訊，不拿來估速度
    "ratio_jump": 0.08,
    # 揮拍比值和目標差超過此值，--debug 時自動存截圖到 debug_shots 資料夾
    "save_off_by": 0.08,
    # 內圈比周圍亮多少（灰階）才算找到
    "inner_min_contrast": 8,
    "target_fps": 144,
    # 由校準自動寫入
    "output_idx": 0,            # 遊戲所在的螢幕編號（dxcam output）
    "monitor_origin": [0, 0],   # 該螢幕左上角在整個桌面的座標，滑鼠定位用
    "region": None,             # [left, top, right, bottom] 該螢幕內的實體像素
}


# ---------- 設定 ----------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return cfg


def save_config(cfg):
    if cfg.get("_profile"):
        # 用設定組執行時，校準結果存回那個設定組，不動 config.json
        profiles = load_profiles()
        profiles.setdefault(cfg["_profile"], {}).update({k: cfg[k] for k in PROFILE_CAL_KEYS})
        save_profiles(profiles)
        print(f"[設定組] 校準結果已存到 {PROFILES_PATH.name} 的「{cfg['_profile']}」")
        return
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------- 設定組：不同電腦/螢幕（平台）和遊戲模式各自的校準與時機 ----------

PROFILES_PATH = Path(__file__).with_name("profiles.json")
# 校準時會更新的值
PROFILE_CAL_KEYS = ("region", "output_idx", "monitor_origin")
# 新建設定組時從目前設定複製的值；其他 config 的鍵也都可以手動加進設定組
PROFILE_NEW_KEYS = PROFILE_CAL_KEYS + ("swing_mode", "match_scale", "match_scale_forehand",
                                       "match_scale_drive", "auto_serve", "auto_timing_bar")


def load_profiles():
    if PROFILES_PATH.exists():
        return json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
    return {}


def save_profiles(profiles):
    PROFILES_PATH.write_text(json.dumps(profiles, indent=2, ensure_ascii=False), encoding="utf-8")


def apply_profile(cfg, name):
    """把設定組的值蓋到 cfg 上；設定組不存在就用目前的值新建一個"""
    profiles = load_profiles()
    if name in profiles:
        cfg.update(profiles[name])
        print(f"[設定組] 使用「{name}」")
    else:
        profiles[name] = {k: cfg[k] for k in PROFILE_NEW_KEYS}
        save_profiles(profiles)
        print(f"[設定組] 新建「{name}」（先複製目前的設定，畫面位置不同就按 F9 重新校準）")
    cfg["_profile"] = name


# ---------- 顏色遮罩與光圈偵測 ----------

def hsv_mask(hsv_img, center, tol):
    h, s, v = center
    th, ts, tv = tol
    lo_sv = (max(s - ts, 0), max(v - tv, 0))
    hi_sv = (min(s + ts, 255), min(v + tv, 255))
    lo_h, hi_h = h - th, h + th
    # OpenCV 的 Hue 是 0~179，紅色會跨越 0，要拆成兩段
    if lo_h < 0 or hi_h > 179:
        m1 = cv2.inRange(hsv_img, (lo_h % 180, *lo_sv), (179, *hi_sv))
        m2 = cv2.inRange(hsv_img, (0, *lo_sv), (hi_h % 180, *hi_sv))
        return m1 | m2
    return cv2.inRange(hsv_img, (lo_h, *lo_sv), (hi_h, *hi_sv))


CLOSE_KERNEL = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
RING_ANGLES = np.linspace(0, 2 * np.pi, 72, endpoint=False)


def ring_coverage(blue, red, center, r):
    """
    沿外圈線條（外緣往內 1~6px）取 72 個方向，回傳 (有顏色的方向比例, 藍點數, 紅點數)。
    真光圈是完整一圈；桌角、桌邊這類藍色只會形成一小段弧，覆蓋率低。
    正反手也只用線條上的點判斷，不會被附近的桌面、球的殘影算進去。
    """
    cx, cy = center
    h, w = blue.shape
    hit = np.zeros(len(RING_ANGLES), bool)
    n_blue = n_red = 0
    # 外圈貼到球拍時外接圓半徑會偏大幾 px，往內多看幾 px 才對得到線條
    for d in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0):
        xs = np.clip(np.round(cx + (r - d) * np.cos(RING_ANGLES)).astype(int), 0, w - 1)
        ys = np.clip(np.round(cy + (r - d) * np.sin(RING_ANGLES)).astype(int), 0, h - 1)
        b, rd = blue[ys, xs] > 0, red[ys, xs] > 0
        hit |= b | rd
        n_blue += int(b.sum())
        n_red += int(rd.sum())
    return float(hit.mean()), n_blue, n_red


OPEN_KERNEL = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
SOLID_PAD_KERNEL = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))


def remove_solid(m):
    """去掉大片實心色塊（球桌），只留細線；光圈貼著球桌時才不會黏成一塊。縮半尺寸做比較快"""
    small = cv2.resize(m, (m.shape[1] // 2, m.shape[0] // 2), interpolation=cv2.INTER_AREA)
    solid = cv2.morphologyEx(small, cv2.MORPH_OPEN, OPEN_KERNEL)
    solid = cv2.dilate(solid, SOLID_PAD_KERNEL)
    solid = cv2.resize(solid, (m.shape[1], m.shape[0]), interpolation=cv2.INTER_NEAREST)
    return m & ~solid


def find_ring(hsv_img, cfg):
    """
    先用原本的顏色找；找不到再用寬鬆顏色 + 去掉球桌再找一次
    （有些模式鏡頭較近，光圈較淡又貼著球桌）。
    """
    ring, mask = find_ring_once(hsv_img, cfg, cfg["hsv_tol"], cfg["red_tol"], False)
    if ring is None and cfg["ring_fallback"]:
        ring2, mask2 = find_ring_once(hsv_img, dict(cfg, blue_hsv=cfg["blue_hsv_alt"]),
                                      cfg["hsv_tol_alt"], cfg["red_tol_alt"], True)
        if ring2 is not None:
            return ring2, mask2
    return ring, mask


def find_ring_once(hsv_img, cfg, blue_tol, red_tol, strip_solid):
    """
    回傳 (ring, mask)。ring = {"center", "r", "side"} 或 None。
    藍、紅兩半圈合併後用閉運算接起交界處的斷點，再挑「圓、空心」的輪廓。
    """
    blue = hsv_mask(hsv_img, cfg["blue_hsv"], blue_tol)
    red = hsv_mask(hsv_img, cfg["red_hsv"], red_tol)
    if strip_solid:
        blue, red = remove_solid(blue), remove_solid(red)
    raw = blue | red
    mask = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, CLOSE_KERNEL)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    r_min, r_max = cfg["ring_r_range"]
    best = None
    for c in contours:
        if len(c) < 20:
            continue
        (cx, cy), r = cv2.minEnclosingCircle(c)
        if not (r_min <= r <= r_max):
            continue
        pts = c.reshape(-1, 2).astype(np.float32)
        dist = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
        if dist.std() / r > cfg["max_roundness_err"]:
            continue
        x0, y0 = max(int(cx - r), 0), max(int(cy - r), 0)
        x1, y1 = int(cx + r) + 1, int(cy + r) + 1
        n_px = cv2.countNonZero(raw[y0:y1, x0:x1])
        if n_px / (np.pi * r * r) > cfg["max_fill"]:
            continue
        coverage, n_blue, n_red = ring_coverage(blue, red, (cx, cy), r)
        if coverage < cfg["min_coverage"]:
            continue
        if warm_fill(hsv_img, (cx, cy), r) > cfg["max_warm_fill"]:
            continue  # 裡面整片紅橘色 = 自己的紅色球拍，不是光圈
        if best is None or r > best[1]:
            best = ((cx, cy), r, n_blue, n_red)
    if best is None:
        return None, mask

    (cx, cy), r, n_blue, n_red = best
    return {"center": (cx, cy), "r": r, "side": side_from_counts(n_blue, n_red, cfg),
            "n_blue": n_blue, "n_red": n_red}, mask


def warm_fill(hsv_img, center, r):
    """圓內 70% 半徑範圍有多少比例是飽和的紅橘色。球拍約 1.0，真光圈內部是透明的，約 0.1 以下"""
    cx, cy = center
    rr = r * 0.7
    x0, y0 = max(int(cx - rr), 0), max(int(cy - rr), 0)
    x1, y1 = int(cx + rr) + 1, int(cy + rr) + 1
    roi = hsv_img[y0:y1, x0:x1]
    if roi.size == 0:
        return 0.0
    yy, xx = np.ogrid[y0:y0 + roi.shape[0], x0:x0 + roi.shape[1]]
    inside = (xx - cx) ** 2 + (yy - cy) ** 2 <= rr * rr
    h, s, v = roi[..., 0], roi[..., 1], roi[..., 2]
    warm = ((h <= 25) | (h >= 160)) & (s >= 120) & (v >= 120)
    return float(warm[inside].mean()) if inside.any() else 0.0


def side_from_counts(n_blue, n_red, cfg):
    total = max(n_blue + n_red, 1)
    if n_blue / total >= cfg["side_ratio"]:
        return "backhand"
    if n_red / total >= cfg["side_ratio"]:
        return "forehand"
    return "middle"


SERVE_ANGLES = np.linspace(0, 2 * np.pi, 48, endpoint=False)


def find_serve_icon(hsv_img, cfg):
    """
    找發球圖示：純白小圓點（約 10px），四周幾乎全是紫色背景。回傳圓心 (x, y) 或 None。
    球員的白手套大小顏色一樣，但周圍有黑色輪廓，用「周圍是不是紫色」排除。
    """
    h_img, w_img = hsv_img.shape[:2]
    white = cv2.inRange(hsv_img, (0, 0, 225), (179, 60, 255))
    n, _, stats, cents = cv2.connectedComponentsWithStats(white)
    hue, sat, val = hsv_img[:, :, 0], hsv_img[:, :, 1], hsv_img[:, :, 2]
    best, best_bg = None, cfg["serve_bg_ratio"]
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if not (6 <= w <= 18 and 6 <= h <= 18) or abs(w - h) > 3 or area / (w * h) < 0.6:
            continue
        cx, cy = cents[i]
        if cy < h_img * cfg["serve_y_min"]:
            continue
        r = (w + h) / 4
        bg = []
        for d in (r + 3, r + 5, r + 7):
            xs = np.clip(np.round(cx + d * np.cos(SERVE_ANGLES)).astype(int), 0, w_img - 1)
            ys = np.clip(np.round(cy + d * np.sin(SERVE_ANGLES)).astype(int), 0, h_img - 1)
            hh = hue[ys, xs]
            bg.append((hh >= 115) & (hh <= 150) & (sat[ys, xs] >= 80) & (val[ys, xs] >= 60))
        bg_ratio = float(np.mean(bg))
        if bg_ratio >= best_bg:
            best, best_bg = (float(cx), float(cy)), bg_ratio
    return best


def find_timing_bar(hsv_img):
    """
    找計時條：橘褐色目標區（H≈15、S≈170、V≈135）+ 同一高度的白色細長移動棒。
    回傳 (白棒中心 x, 目標區左 x, 目標區右 x, 目標區中心 y) 或 None。
    白棒經過目標區時會把橘色切成兩塊，所以把同一列的橘色塊合併算範圍。
    """
    zone = cv2.inRange(hsv_img, (8, 110, 90), (24, 230, 190))
    n, _, st, _ = cv2.connectedComponentsWithStats(zone)
    blocks = [st[i] for i in range(1, n) if st[i][3] >= 20 and st[i][4] >= 150]
    if not blocks:
        return None
    white = cv2.inRange(hsv_img, (0, 0, 220), (179, 50, 255))
    n, _, wst, wcen = cv2.connectedComponentsWithStats(white)
    for b in blocks:
        by0, by1 = b[1], b[1] + b[3]
        same_row = [c for c in blocks if abs(c[1] - b[1]) <= 6 and abs(c[3] - b[3]) <= 8]
        left = min(c[0] for c in same_row)
        right = max(c[0] + c[2] for c in same_row)
        if not (30 <= right - left <= 250):
            continue
        for i in range(1, n):
            x, y, w, h, _ = wst[i]
            # 白棒：細長、比目標區高、上下涵蓋目標區
            if 3 <= w <= 16 and h >= 1.2 * b[3] and h >= 3 * w and y <= by0 + 3 and y + h >= by1 - 3:
                return float(wcen[i][0]), float(left), float(right), float((by0 + by1) / 2)
    return None


def resolve_side(side, cfg):
    if cfg["swing_mode"] == "drive":
        return "forehand"
    return cfg["middle_side"] if side == "middle" else side


def match_for(side, cfg):
    if cfg["swing_mode"] == "drive":
        return cfg["match_scale_drive"]
    return cfg["match_scale_forehand"] if resolve_side(side, cfg) == "forehand" else cfg["match_scale"]


DILATE_KERNEL = np.ones((3, 3), np.uint8)


def fit_circle(xs, ys):
    """最小平方法擬合圓，回傳 (cx, cy, r)"""
    A = np.column_stack([xs, ys, np.ones_like(xs)])
    (a, b, c), *_ = np.linalg.lstsq(A, xs ** 2 + ys ** 2, rcond=None)
    cx, cy = a / 2, b / 2
    return cx, cy, float(np.sqrt(max(c + cx ** 2 + cy ** 2, 0)))


def measure_inner(gray, color_mask, ring, cfg):
    """
    從外圈圓心往 90 個方向，各自找「比前後 2px 都亮」的點（灰色內圈線），再用這些點擬合圓。
    - 逐方向找點再擬合：外圈圓心估偏幾 px（外圈部分顏色沒抓到時常見）也量得準
    - 外圈本身的彩色像素先排除：外圈縮到很貼近內圈時（快球常見）也量得到
    - 擬合後剔除離圓太遠的點再擬合一次：球、球拍、衣服花紋等亮點不會帶偏
    """
    max_r = ring["r"]
    if max_r < 6:
        return None
    rows = 90
    bins = int(max_r * 2)  # 0.5px 解析度
    px_per_bin = max_r / bins
    cx, cy = ring["center"]
    polar = cv2.warpPolar(gray, (bins, rows), (cx, cy), max_r,
                          cv2.WARP_POLAR_LINEAR | cv2.INTER_LINEAR).astype(np.float32)
    excl = cv2.warpPolar(cv2.dilate(color_mask, DILATE_KERNEL), (bins, rows), (cx, cy), max_r,
                         cv2.WARP_POLAR_LINEAR)
    polar[excl > 0] = np.nan

    k = 4                                   # 和前後各 2px 比較亮度
    lo = max(int(3 / px_per_bin), k)        # 半徑 3px 以內是圓心雜訊，略過
    if bins - k - lo < 4:
        return None
    contrast = polar[:, lo:bins - k] - (polar[:, lo - k:bins - 2 * k] + polar[:, lo + k:bins]) / 2
    contrast = np.nan_to_num(contrast, nan=-1e9)
    idx = np.argmax(contrast, axis=1)
    ok = contrast[np.arange(rows), idx] >= cfg["inner_min_contrast"]
    if ok.sum() < rows * 0.3:
        return None

    radii = (idx[ok] + lo + 0.5) * px_per_bin
    ang = np.flatnonzero(ok) * 2 * np.pi / rows
    # 先用中位數半徑剔除明顯離群點：外圈疊在球員身上時，有一兩成方向會抓到衣服亮點，會把第一次擬合整個帶偏
    med = np.median(radii)
    near = np.abs(radii - med) <= max(0.3 * med, 3)
    if near.sum() < rows * 0.25:
        return None
    radii, ang = radii[near], ang[near]
    xs, ys = cx + radii * np.cos(ang), cy + radii * np.sin(ang)
    fx, fy, fr = fit_circle(xs, ys)
    keep = np.abs(np.hypot(xs - fx, ys - fy) - fr) <= 1.5
    if keep.sum() < rows * 0.25:
        return None
    _, _, fr = fit_circle(xs[keep], ys[keep])
    return fr if 3 < fr < max_r else None


def robust_line(history, now):
    """
    Theil–Sen 直線擬合：斜率取所有兩點斜率的中位數，單張量錯不會把預估拉偏。
    回傳 (每秒比值變化量, 擬合後「現在」的比值)
    """
    ts = np.array([h[0] for h in history]) - now
    qs = np.array([h[1] for h in history])
    i, j = np.triu_indices(len(ts), 1)
    slope = float(np.median((qs[j] - qs[i]) / (ts[j] - ts[i])))
    return slope, float(np.median(qs - slope * ts))


# ---------- 滑鼠 ----------

user32 = ctypes.windll.user32
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_MOVE, MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x4000, 0x8000


def move_cursor(x, y):
    """
    用真正的滑鼠移動事件移動游標。SetCursorPos 只是瞬移，不會產生移動事件，
    按住拖曳時瀏覽器裡的遊戲看不到，引拍就不會被吃到。
    座標是整個桌面的實體像素，要換算成 0~65535 的虛擬桌面比例。
    """
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    nx = round((x - vx) * 65535 / max(vw - 1, 1))
    ny = round((y - vy) * 65535 / max(vh - 1, 1))
    user32.mouse_event(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny, 0, 0)


def left_button_down():
    """滑鼠左鍵現在是否按著（程式按的或使用者按的都算）"""
    return bool(user32.GetAsyncKeyState(0x01) & 0x8000)


class Mouse:
    """滑鼠動作丟到背景執行緒依序執行，主迴圈拖曳期間仍持續看畫面"""

    def __init__(self, cfg):
        left, top, right, bottom = cfg["region"]
        mx, my = cfg["monitor_origin"]
        self.ox, self.oy = mx + left, my + top
        self.w, self.h = right - left, bottom - top
        self.cfg = cfg
        self.down = False
        self.x = self.y = 0
        self.jobs = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        while True:
            job = self.jobs.get()
            try:
                job()
            finally:
                self.jobs.task_done()

    def pull(self, side):
        self.jobs.put(lambda: self._pull(side))

    def swing(self):
        self.jobs.put(self._swing)

    def serve(self):
        self.jobs.put(self._serve)

    def click_at(self, x, y):
        """在遊戲區座標 (x, y) 點一下；drive 正按著引拍就先放開"""
        def job():
            self._release()
            self.x, self.y = x, y
            move_cursor(self.ox + x, self.oy + y)
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(self.cfg["tap_hold_ms"] / 1000)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        self.jobs.put(job)

    def wait(self, seconds):
        self.jobs.put(lambda: time.sleep(seconds))

    def release(self):
        self.jobs.put(self._release)

    def busy(self):
        """還有動作在排隊或執行中（例如揮完正在等著再引拍）"""
        return self.jobs.unfinished_tasks > 0

    def release_now(self):
        """等排隊中的動作做完再放開，結束程式時用"""
        self.jobs.join()
        self._release()

    def _drag(self, x1, y1, ms):
        x0, y0 = self.x, self.y
        steps = max(int(ms / 5), 1)
        for i in range(1, steps + 1):
            f = i / steps
            self.x, self.y = x0 + (x1 - x0) * f, y0 + (y1 - y0) * f
            move_cursor(self.ox + self.x, self.oy + self.y)
            time.sleep(ms / 1000 / steps)

    def _pull(self, side, press=None):
        """依正反手決定起點，按下並往下拖曳引拍，之後保持按住（tap 模式只移游標，不按；發球時強制按）"""
        cfg = self.cfg
        side = resolve_side(side, cfg)
        self.x = self.w * cfg["backhand_x" if side == "backhand" else "forehand_x"]
        self.y = self.h * cfg["swipe_y"]
        move_cursor(self.ox + self.x, self.oy + self.y)
        if not (cfg["swing_mode"] != "tap" if press is None else press):
            return
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        self.down = True
        self._drag(self.x, self.y + self.h * cfg["pull_ratio"], cfg["pull_ms"])
        print(f"\n[滑鼠] 按下並往下拖（引拍），游標在 ({int(self.ox + self.x)}, {int(self.oy + self.y)})")

    def _swing(self):
        """從引拍位置往上滑並放開（tap 模式則在左/右側點一下）"""
        if self.cfg["swing_mode"] == "tap":
            move_cursor(self.ox + self.x, self.oy + self.y)
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(self.cfg["tap_hold_ms"] / 1000)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            return
        self._drag(self.x, self.y - self.h * self.cfg["swing_ratio"], self.cfg["swing_ms"])
        self._release()
        print("\n[滑鼠] 往上推並放開（揮拍）")
        if self.cfg["swing_mode"] == "drive":
            # 等揮拍動畫做完再按下引拍，太早按會被遊戲忽略
            time.sleep(self.cfg["repull_gap_ms"] / 1000)
            self._pull("forehand")

    def _serve(self):
        """發球：按住往下拖引拍，停一下再往上推放開。drive 已經按住引拍就直接推"""
        cfg = self.cfg
        if not (self.down and left_button_down()):
            self._pull("forehand", press=True)
            time.sleep(cfg["serve_hold_ms"] / 1000)
        self._drag(self.x, self.y - self.h * cfg["swing_ratio"], cfg["swing_ms"])
        self._release()
        print("\n[滑鼠] 發球：往上推並放開")
        if cfg["swing_mode"] == "drive":
            time.sleep(cfg["repull_gap_ms"] / 1000)
            self._pull("forehand")

    def _release(self):
        if self.down:
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            self.down = False


# ---------- 校準 ----------

def wait_snapshot():
    """F6 立即截圖；或在終端機按 Enter，倒數 5 秒後截圖（給 F6 沒反應的筆電用）"""
    ready = threading.Event()
    keyboard.add_hotkey("f6", ready.set)

    def countdown():
        input()
        for i in range(5, 0, -1):
            if ready.is_set():
                return
            print(f"  {i} 秒後截圖，請切到遊戲畫面…")
            time.sleep(1)
        ready.set()

    threading.Thread(target=countdown, daemon=True).start()
    print("[校準] 兩種方式擇一：")
    print("  A. 切到遊戲，光圈出現時按 F6（筆電可能要按 Fn + F6）")
    print("  B. 在這個視窗按 Enter，5 秒內切到遊戲讓光圈出現（可先按遊戲的 Pause 停住畫面）")
    ready.wait()
    keyboard.remove_hotkey("f6")

    # 截滑鼠游標所在的那個螢幕
    pt = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    outputs = list_outputs()
    idx, left, top = outputs[0][0], outputs[0][1], outputs[0][2]
    for i, l, t, r, b in outputs:
        if l <= pt.x < r and t <= pt.y < b:
            idx, left, top = i, l, t
            break
    print(f"[校準] 已截圖（第 {idx} 號螢幕，共 {len(outputs)} 個）")

    cam = dxcam.create(output_idx=idx, output_color="BGR")
    # F9 重新校準時 dxcam 會回傳執行中那個舊物件，它還記得上次的遊戲區範圍，要明確指定截整個螢幕
    full_screen = (0, 0, cam.width, cam.height)
    frame = None
    while frame is None:
        frame = cam.grab(region=full_screen)
    del cam
    return frame, idx, [left, top]


def list_outputs():
    """回傳 [(output_idx, left, top, right, bottom)]，座標是整個桌面的實體像素"""
    outs = []
    for i in range(8):
        try:
            cam = dxcam.create(output_idx=i, output_color="BGR")
        except Exception:
            break
        rect = cam._output.desc.DesktopCoordinates
        outs.append((i, rect.left, rect.top, rect.right, rect.bottom))
        del cam
    return outs


def calibrate(cfg):
    full, cfg["output_idx"], cfg["monitor_origin"] = wait_snapshot()

    win = "Drag to select GAME AREA, then ENTER"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    x, y, w, h = cv2.selectROI(win, full, showCrosshair=False)
    cv2.destroyWindow(win)
    if w == 0 or h == 0:
        raise SystemExit("沒有框選遊戲區")
    cfg["region"] = [int(x), int(y), int(x + w), int(y + h)]
    crop = full[y:y + h, x:x + w]

    # 用擷取的畫面驗證一次：綠圈 = 外圈，紅圈 = 偵測到的內圈
    ring, mask = find_ring(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV), cfg)
    vis = crop.copy()
    if ring is None:
        print("[校準] ⚠ 這張畫面偵測不到外圈，請看 mask 視窗，必要時調整 hsv_tol 後按 F9 重來")
        shots_dir = Path(__file__).with_name("debug_shots")
        shots_dir.mkdir(exist_ok=True)
        name = time.strftime("%H%M%S") + "_校準失敗.png"
        cv2.imencode(".png", crop)[1].tofile(str(shots_dir / name))
        print(f"  → 已存 debug_shots/{name}")
    else:
        inner = measure_inner(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), mask, ring, cfg)
        c = tuple(map(int, ring["center"]))
        cv2.circle(vis, c, int(ring["r"]), (0, 255, 0), 1)
        if inner is None:
            print(f"[校準] 外圈 {ring['r']:.1f}px（{ring['side']}），⚠ 找不到內圈，可調小 inner_min_contrast")
        else:
            cv2.circle(vis, c, int(round(inner)), (0, 0, 255), 1)
            print(f"[校準] 外圈 {ring['r']:.1f}px（{ring['side']}），內圈 {inner:.1f}px，比值 {ring['r'] / inner:.2f}")
    vis = cv2.resize(vis, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
    cv2.imshow("check: green=outer red=inner (any key)", vis)
    cv2.imshow("check mask (any key)", mask)
    cv2.waitKey(0)
    cv2.destroyAllWindows()

    save_config(cfg)
    print(f"[校準] 已儲存到 {CONFIG_PATH}")


# ---------- 主迴圈 ----------

def run(cfg, debug):
    cam = dxcam.create(output_idx=cfg["output_idx"], output_color="BGR")
    # 不用 video_mode：畫面沒變時不會重複送同一張，縮小速度估算才準確
    cam.start(region=tuple(cfg["region"]), target_fps=cfg["target_fps"])
    mouse = Mouse(cfg)

    state = {"active": False, "quit": False, "recal": False, "test": False}

    def toggle():
        state["active"] = not state["active"]
        print("\n▶ 開始" if state["active"] else "\n⏸ 暫停")

    keyboard.add_hotkey("f7", lambda: state.update(test=True))
    keyboard.add_hotkey("f8", toggle)
    keyboard.add_hotkey("f9", lambda: state.update(recal=True))
    keyboard.add_hotkey("f10", lambda: state.update(quit=True))
    mode_name = {"tap": "點擊 block（正反手自動）", "swipe": "拖曳揮拍（正反手自動）",
                 "drive": "全部正手拉球"}.get(cfg["swing_mode"], cfg["swing_mode"])
    print(f"模式：{mode_name}")
    print("就緒：F7 測試揮拍，F8 開始/暫停，F9 重新校準，F10 結束")

    drive = cfg["swing_mode"] == "drive"
    drive_ready = False     # drive 模式：滑鼠已按住引拍中
    btn_up_since = None     # drive 模式：左鍵從什麼時候開始是放開的
    serve_pos = None        # 發球圖示第一次出現的位置
    serve_since = serve_last = 0.0  # 圖示從何時開始穩定出現、最後一次看到的時間
    serve_block_until = 0.0  # 剛發完球，這段時間內不再發
    bar_track = deque(maxlen=6)  # 計時條白棒的 (時間, x)
    bar_last = -10.0        # 最後一次看到計時條的時間
    bar_block_until = 0.0   # 剛點完計時條，這段時間內不再點
    # drive 不用另外提早：實測提早 swing_ms 出手，每球都早約 45ms（遊戲一開始往上推就判定擊球）
    lead_ms = cfg["lead_ms"]
    phase = "idle"          # idle → held → cooldown → idle
    phase_t = 0.0
    held_side = None        # 光圈出現時決定的正反手，出手時用同一手的比值
    side_counts = [0, 0]    # 這球累計的藍、紅點數；第一張光圈常較淡，累計多張判斷才不會用錯手
    last_seen = 0.0
    seen_streak = miss_streak = 0
    # 本球量到的內圈半徑（每球不同、同一球內固定），取中位數去雜訊；光圈消失就清空換下一球
    inners = deque(maxlen=15)
    rejected = deque(maxlen=3)  # 本球最近連續被當作量錯剔除的內圈值
    prev_inner = None           # 最近一次的內圈，本球還沒量到時暫用（快球可能來不及量）
    history = deque(maxlen=8)   # (時間, 外圈/內圈 比值)
    frame_dts = deque(maxlen=30)
    prev_t = None
    last_ratio = None
    shot_frames = shot_inner_hits = 0   # 本球看到幾張、量到內圈幾次，取消時印出診斷
    last_vis = None             # 最近一張有光圈的標註畫面，異常時存檔用
    shots_dir = Path(__file__).with_name("debug_shots")
    fps_t, fps_n = time.perf_counter(), 0

    def save_shot(tag, vis):
        if not debug or vis is None:
            return
        shots_dir.mkdir(exist_ok=True)
        name = time.strftime("%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}_{tag}.png"
        # cv2.imwrite 不支援中文路徑，改用 imencode + tofile
        cv2.imencode(".png", vis)[1].tofile(str(shots_dir / name))
        print(f"  → 已存 debug_shots/{name}")

    try:
        while not state["quit"]:
            if state["recal"]:
                return "recal"
            if state["test"]:
                state["test"] = False
                print(f"\n[測試] 正手 {cfg['swing_mode']}")
                if not drive_ready:
                    mouse.pull("forehand")
                    mouse.wait(0.15)
                mouse.swing()
                if drive and not state["active"]:
                    mouse.release()  # 暫停中測試：drive 揮完會自動再引拍，要放開

            frame = cam.get_latest_frame()
            now = time.perf_counter()
            if prev_t is not None:
                frame_dts.append(now - prev_t)
            prev_t = now
            frame_dt_ms = float(np.median(frame_dts)) * 1000 if frame_dts else 16.7

            hsv =cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            ring, mask = find_ring(hsv, cfg)
            inner = ratio = ttm_ms = slope = ratio_now = None
            measured = None
            inner_src = "-"

            if ring is not None:
                measured = measure_inner(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), mask, ring, cfg)
                if measured is not None and len(inners) >= 3:
                    med = float(np.median(inners))
                    if abs(measured - med) / med > cfg["inner_outlier"]:
                        rejected.append(measured)
                        measured = None  # 和本球其他量測差太多，當作量錯
                        last3 = list(rejected)
                        if len(last3) == 3 and (max(last3) - min(last3)) / min(last3) <= cfg["inner_outlier"]:
                            # 連續 3 次被剔除、彼此卻很一致 = 一開始那幾次量錯了，改用新的值，
                            # 否則前面量錯的值會把之後正確的量測全部擋掉，比值一直錯到取消
                            print(f"\n[內圈修正] {med:.1f}px → {float(np.median(last3)):.1f}px")
                            inners.clear()
                            inners.extend(last3[:2])
                            history.clear()
                            measured = last3[2]
                            rejected.clear()
                    else:
                        rejected.clear()
                cand = list(inners) + ([measured] if measured is not None else [])
                if cand:
                    inner, inner_src = float(np.median(cand)), "實測"
                elif prev_inner is not None:
                    # 只顯示參考用；暫用值不拿來出手（每球內圈不同，上一球的值會讓時機大亂）
                    inner_src = f"未量到(上球{prev_inner:.0f})"
                if inner is not None and ring["r"] / inner < cfg["min_ratio"]:
                    # 外圈比內圈還小，不可能是光圈，當作沒看到
                    ring = inner = measured = None
                    inner_src = "-"

            if ring is not None:
                last_seen = now
                seen_streak += 1
                miss_streak = 0
                if measured is not None:
                    inners.append(measured)
                    prev_inner = inner
                if inner is not None:
                    ratio = last_ratio = ring["r"] / inner
                    # 比值只會越縮越小；突然變大是量測雜訊，不拿來估速度
                    if not history or ratio <= history[-1][1] + cfg["ratio_jump"]:
                        history.append((now, ratio))
            else:
                seen_streak = 0
                miss_streak += 1
                # 光圈疊到球員身上時常有一兩張偵測失敗；真的消失一段時間才當作換下一球，
                # 否則量好的內圈和縮小速度被清掉，要重新量，快球就來不及出手。
                # 引拍中不清：光圈被頭擋住時要靠之前的縮小速度推算出手時機
                if phase != "held" and now - last_seen > cfg["rearm_ms"] / 1000:
                    inners.clear()
                    rejected.clear()
                    history.clear()
            # 光圈這張沒看到時也照算，ratio_now 就是用縮小速度推算的「現在」比值
            if len(history) >= 2:
                slope, ratio_now = robust_line(history, now)

            def ttm_for(m):
                if slope is None or slope >= 0:
                    return None
                return (ratio_now - m) / -slope * 1000

            def should_fire(m):
                """
                已到目標，或預估在「下一張畫面之前」就會到目標（現在出手比等下一張更接近目標）。
                60 FPS 時兩張畫面差約 17ms，快球一張就縮很多，所以把半個畫面間隔算進提前量。
                """
                if ratio is None:
                    return False
                if ratio <= m:
                    return True
                if ratio - m > cfg["predict_window"]:
                    return False  # 離目標還遠，光圈先快後慢，直線預估會太早
                t = ttm_for(m)
                return t is not None and t <= lead_ms + frame_dt_ms / 2

            match = match_for(held_side if phase == "held" else (ring["side"] if ring else "backhand"), cfg)
            ttm_ms = ttm_for(match)

            vis = None
            if debug:
                vis = frame.copy()
                if ring is not None:
                    c = tuple(map(int, ring["center"]))
                    cv2.circle(vis, c, int(ring["r"]), (0, 255, 0), 2)
                    if inner is not None:
                        cv2.circle(vis, c, int(round(inner)), (0, 0, 255), 1)
                label = (f"{phase}  {ring['side'] if ring else '-'}"
                         f"  ratio={'-' if ratio is None else f'{ratio:.2f}'}"
                         f"  inner={inner_src}"
                         f"  ttm={'-' if ttm_ms is None else round(ttm_ms)}")
                cv2.putText(vis, label, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
                if ring is not None:
                    last_vis = vis

            def log_swing(tag):
                ttm_txt = "-" if ttm_ms is None else f"{ttm_ms:.0f}ms"
                print(f"\n[{tag}] {held_side}  外圈 {ring['r']:.1f}px / 內圈 {inner:.1f}px（{inner_src}）"
                      f" = {ratio:.2f}（目標 {match}），預估差 {ttm_txt}，藍 {side_counts[0]} / 紅 {side_counts[1]}")
                if tag != "揮拍" or abs(ratio - match) > cfg["save_off_by"]:
                    save_shot(tag, vis)

            # 計時條小遊戲：預估白棒進到橘色區中間就點擊
            bar = find_timing_bar(hsv) if cfg["auto_timing_bar"] and state["active"] and ring is None else None
            if bar is not None:
                bar_x, zone_l, zone_r, zone_y = bar
                if now - bar_last > 0.3:
                    bar_track.clear()
                bar_last = now
                bar_track.append((now, bar_x))
                if len(bar_track) >= 3 and now > bar_block_until:
                    ts = np.array([t for t, _ in bar_track]) - now
                    xs = np.array([x for _, x in bar_track])
                    speed = float(np.polyfit(ts, xs, 1)[0])  # 每秒移動幾 px，正 = 往右
                    pred = bar_x + speed * (cfg["bar_lead_ms"] + frame_dt_ms / 2) / 1000
                    zc, half = (zone_l + zone_r) / 2, (zone_r - zone_l) / 2 * cfg["bar_window"]
                    if abs(pred - zc) <= half:
                        mouse.click_at(zc, zone_y)
                        print(f"\n[計時條] 白棒 x={bar_x:.0f}、預估 {pred:.0f}，目標區 {zone_l:.0f}~{zone_r:.0f}，速度 {speed:.0f}px/s")
                        save_shot("計時條", frame.copy() if vis is None else vis)
                        bar_block_until = now + 0.8
            bar_busy = now - bar_last < 1.0   # 計時條剛出現過：不要發球、不要自動按住引拍

            if (cfg["auto_serve"] and state["active"] and phase == "idle" and ring is None
                    and now > serve_block_until and not bar_busy):
                icon = find_serve_icon(hsv, cfg)
                if icon is not None:
                    if serve_pos is None or np.hypot(icon[0] - serve_pos[0], icon[1] - serve_pos[1]) > cfg["serve_max_drift"]:
                        serve_pos, serve_since = icon, now   # 新出現或跳太遠（在飛的真球）：重新計時
                    serve_last = now
                    if now - serve_since >= cfg["serve_stable_ms"] / 1000 and not mouse.busy():
                        mouse.serve()
                        print(f"\n[發球] 偵測到發球圖示 ({icon[0]:.0f}, {icon[1]:.0f})")
                        save_shot("發球", vis)
                        serve_pos, serve_block_until = None, now + 1.5
                elif serve_pos is not None and now - serve_last > 0.2:
                    serve_pos = None   # 偶爾一兩張沒抓到不算消失
            else:
                serve_pos = None

            if drive and state["active"] and not bar_busy:
                # 沒先引拍的球（例如第一球）容易大 early：左鍵放開一陣子就先按下引拍等球
                if left_button_down():
                    btn_up_since = None
                elif btn_up_since is None:
                    btn_up_since = now
                elif now - btn_up_since > cfg["auto_pull_ms"] / 1000 and not mouse.busy():
                    mouse.pull("forehand")
                    drive_ready = True
                    btn_up_since = None

            if not state["active"]:
                if phase == "held" or drive_ready:
                    mouse.release()
                    drive_ready = False
                phase = "idle"
            elif phase == "idle":
                # 有量到內圈 = 確定是真光圈，第一張就行動（快球只有幾張畫面）；否則要連續 2 張
                if ring is not None and (measured is not None or seen_streak >= 2):
                    held_side = resolve_side(ring["side"], cfg)
                    side_counts = [ring["n_blue"], ring["n_red"]]
                    match = match_for(held_side, cfg)
                    ttm_ms = ttm_for(match)
                    if not drive:
                        mouse.pull(held_side)
                    elif not mouse.busy() and not left_button_down():
                        # 按 F8 時不先按住（使用者還要點遊戲的開始），光圈出現才按下引拍；
                        # 中途被使用者點擊打斷、左鍵已放開，也在這裡補按回來
                        mouse.pull("forehand")
                        drive_ready = True
                    shot_frames, shot_inner_hits = 1, int(measured is not None)
                    last_ratio = ratio  # 不沿用上一球的最後比值，避免誤觸補揮
                    if should_fire(match):
                        # 光圈被偵測到時已到出手時機（快球常見）：立刻出手
                        mouse.swing()
                        log_swing("急揮")
                        phase, phase_t = "cooldown", now
                    else:
                        phase, phase_t = "held", now
            elif phase == "held":
                if ring is not None:
                    shot_frames += 1
                    shot_inner_hits += measured is not None
                    side_counts[0] += ring["n_blue"]
                    side_counts[1] += ring["n_red"]
                    new_side = resolve_side(side_from_counts(*side_counts, cfg), cfg)
                    # tap 只是把游標移到另一邊，可以改；swipe 已經按住拖曳就不改
                    if new_side != held_side and cfg["swing_mode"] == "tap":
                        print(f"\n[改手] {held_side} → {new_side}（累計 藍 {side_counts[0]} / 紅 {side_counts[1]}）")
                        held_side = new_side
                        match = match_for(held_side, cfg)
                        ttm_ms = ttm_for(match)
                        mouse.pull(held_side)
                if should_fire(match):
                    mouse.swing()
                    log_swing("揮拍")
                    phase, phase_t = "cooldown", now
                    history.clear()
                elif ratio is None and ttm_ms is not None and ttm_ms <= lead_ms + frame_dt_ms / 2:
                    # 光圈被擋住或內圈沒量到，但照之前的縮小速度推算已到時機：不等它重新出現
                    mouse.swing()
                    print(f"\n[推算揮] {held_side}  光圈看不到，推算比值 {ratio_now:.2f}（目標 {match}），"
                          f"最後實測 {last_ratio if last_ratio is None else round(last_ratio, 2)}")
                    save_shot("推算揮", last_vis)
                    phase, phase_t = "cooldown", now
                    history.clear()
                elif (ring is None and miss_streak >= 2 and last_ratio is not None
                      and last_ratio <= match * cfg["lost_swing_margin"]):
                    mouse.swing()
                    print(f"\n[補揮] {held_side}  光圈在接近出手時消失，最後比值 {last_ratio:.2f}（目標 {match}）")
                    save_shot("補揮", last_vis)
                    phase, phase_t = "cooldown", now
                elif now - last_seen > cfg["lost_ms"] / 1000:
                    if not drive:
                        mouse.release()
                    ratio_txt = "-" if last_ratio is None else f"{last_ratio:.2f}"
                    print(f"\n[取消] {held_side} 光圈消失，沒有出手"
                          f"（看到 {shot_frames} 張、量到內圈 {shot_inner_hits} 次、最後比值 {ratio_txt}）")
                    save_shot("取消", last_vis)
                    phase = "idle"
            elif (phase == "cooldown" and now - phase_t > cfg["cooldown_ms"] / 1000
                  and now - last_seen > cfg["rearm_ms"] / 1000):
                phase = "idle"

            if debug:
                cv2.imshow("debug (move this window away from the game)", vis)
                cv2.imshow("ring mask", mask)
                cv2.waitKey(1)

            fps_n += 1
            if now - fps_t >= 2:
                print(f"\r處理速度 {fps_n / (now - fps_t):.0f} FPS   ", end="")
                fps_t, fps_n = now, 0
    finally:
        mouse.release_now()
        cam.stop()
        del cam
        keyboard.unhook_all_hotkeys()
        cv2.destroyAllWindows()
    return "quit"


def main():
    # 讓座標使用實體像素，避免 Windows 縮放(125%/150%)造成滑鼠位置偏移
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        user32.SetProcessDPIAware()
    # 把 Windows 計時精度提高到 1ms，否則 sleep 最少約 15ms，拖曳會太慢
    ctypes.windll.winmm.timeBeginPeriod(1)

    ap = argparse.ArgumentParser()
    ap.add_argument("--calibrate", action="store_true", help="強制重新校準")
    ap.add_argument("--debug", action="store_true", help="顯示識別畫面")
    ap.add_argument("--drive", action="store_true", help="全部正手拉球（只限這次執行，不改設定檔）")
    ap.add_argument("--profile", help="使用 profiles.json 裡的設定組（平台/模式），沒有就新建")
    args = ap.parse_args()

    cfg = load_config()
    if not CONFIG_PATH.exists():
        save_config(cfg)  # 產生預設設定檔讓使用者修改
    if args.profile:
        apply_profile(cfg, args.profile)
    elif PROFILES_PATH.exists():
        print(f"[設定組] 可用：{'、'.join(load_profiles())}（用 --profile 名稱 選擇）")
    if args.calibrate or not cfg["region"]:
        calibrate(cfg)
    if args.drive:
        cfg["swing_mode"] = "drive"

    while run(cfg, args.debug) == "recal":
        calibrate(cfg)


if __name__ == "__main__":
    main()
