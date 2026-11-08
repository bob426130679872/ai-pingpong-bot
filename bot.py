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
    # 罐子模式（swing_mode = "can"）：看罐子在對手桌面哪一路，決定這球的打法
    # 對手半場桌面範圍（遊戲區內的 [左, 上, 右, 下]），校準時框選
    "can_area": None,
    # 桌面藍色的 HSV 下限、上限
    "table_hsv_lo": [98, 180, 120],
    "table_hsv_hi": [114, 255, 255],
    # 罐子在桌面範圍內至少要有這麼多像素（排除飛行中的球、桌面白線）
    "can_min_area": 150,
    # 罐子在桌面左邊這個比例內算左路、右邊這個比例內算右路，其他算中間
    "can_lane_split": 0.35,
    # 打法：tap = 點擊 block；flick（斜線揮拍）、drive（打中間）都是光圈出現就按住往下拖引拍，
    # 時機到再往上推，只差在出手比值分開調
    "can_actions": {
        "forehand": {"straight": "tap", "cross": "flick", "middle": "drive"},
        "backhand": {"straight": "tap", "cross": "flick", "middle": "drive"},
    },
    # 找不到罐子時當作在哪一路：left / middle / right
    "can_default_lane": "middle",
    # flick 往上推時同時往旁邊推多少（遊戲區寬度比例）：正手往左上、反手往右上
    "flick_dx_ratio": 0.25,
    # flick 引拍時往反方向拖多少（遊戲區寬度比例）：正手往右下、反手往左下
    "flick_pull_dx_ratio": 0.1,
    # 罐子、水果模式按住引拍後，光圈大小會突然改變：引拍拖完再等這麼久（毫秒）才開始量光圈
    "pull_settle_ms": 100,
    # 引拍中罐子換路時，離出手還有超過這麼久（毫秒）才改打法；太接近出手就來不及重新引拍
    "can_replan_ms": 100,
    # flick 的出手比值。太早調小、太晚調大
    "match_scale_flick": 1.45,
    # 水果模式（swing_mode = "fruit"）：一般水果和黑色炸彈用不同的拖曳方向
    # pull = 光圈出現時按住往哪拖引拍、swing = 出手時從引拍位置往哪滑，[x, y] 是遊戲區寬、高的比例（x 正 = 右、y 正 = 下）
    "gestures": {
        "fruit": {"pull": [0.1, -0.15], "swing": [-0.25, 0.4]},   # 往右上引拍，往左下揮
        "bomb": {"pull": [0.1, 0.15], "swing": [-0.25, -0.4]},    # 往右下引拍，往左上揮
    },
    # gestures 寫的是正手方向，反手左右相反（水果：往左上引拍、往右下揮；炸彈：往左下引拍、往右上揮）
    "gesture_mirror_backhand": True,
    # 罐子、水果模式的出手比值再依正反手加減，例如 {"backhand": 0.05} = 反手早一點
    "match_side_offset": {},
    # 各打法的出手比值。太早調小、太晚調大
    "match_scale_fruit": 1.45,
    "match_scale_bomb": 1.45,
    # 炸彈：亮度低於這個值算黑色；大小範圍（像素面積）；周圍至少這個比例是桌面藍色
    "bomb_v_max": 70,
    "bomb_area": [30, 6000],
    "bomb_bg_ratio": 0.6,
    # 桌面範圍左右各放寬幾 px 找炸彈（貼著桌邊飛的）；紫色地板的 HSV 範圍（炸彈周圍可以是地板）
    "bomb_side_px": 60,
    "floor_hsv_lo": [120, 80, 60],
    "floor_hsv_hi": [145, 255, 255],
    # 引信火花的顏色（實測約 H15 S251 V243）和像素數範圍：要有火花才算炸彈（排除水果在地板上的影子），
    # 太多則是整顆橘色/黃色/紅色水果
    "spark_hsv_lo": [0, 120, 200],
    "spark_hsv_hi": [35, 255, 255],
    "bomb_spark_px": [6, 400],
    # 水果顏色（實測 橘子 H10、檸檬 H26、西瓜 H48，飽和度都 240 以上；蘋果紅色跨 H0/H179）：
    # 炸彈候選周圍 fruit_check_px 內有一塊至少 fruit_min_px 的水果色就當作水果
    "fruit_hsv": [[[0, 200, 100], [35, 255, 255]], [[40, 200, 50], [75, 255, 255]], [[170, 200, 100], [179, 255, 255]]],
    "fruit_check_px": 60,
    "fruit_min_px": 350,
    # 往我方飛（畫面上往下）至少這麼多像素才算來球，排除自己打回去的炸彈
    "bomb_min_dy": 3,
    # 揮拍後這麼久（毫秒）內看到的炸彈還是剛打的那顆（打到後還會往下飛一兩張），不算下一球
    "bomb_ignore_after_swing_ms": 350,
    # 炸彈球量不到內圈時用的預設內圈半徑（像素）；None = 不用預設
    "bomb_inner_default": 14.5,
    # 炸彈球只接受這個範圍內的內圈量測（實測 13.2~15px），超出當作量錯
    "bomb_inner_range": [12.5, 16.5],
    # 看到來球是炸彈後，這麼久（毫秒）內的出手都用炸彈打法
    "bomb_memory_ms": 1500,
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
    # 引拍後只接受比上一張小（或大不到 15%）的光圈，排除突然出現的大圓（炸彈爆炸光暈等）
    "ring_shrink_only": False,
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
    # 內圈取最近幾次量測的中位數。內圈會在引拍後變大小的模式（水果）設小一點，才跟得上
    "inner_window": 15,
    # 內圈取最近幾次量測的最小值而不是中位數（內圈會在引拍後縮小的模式用）
    "inner_use_min": False,
    # 內圈取「從圓心往外第一個夠亮的圈」而不是最亮的圈（外圈內側有亮光暈的模式用）
    "inner_pick_first": False,
    # 外圈 / 內圈 低於此值就不是真光圈（誤認其他藍/紅圓形物體）
    "min_ratio": 0.95,
    # 比值只會越縮越小，單張突然變大超過此值視為量測雜訊，不拿來估速度
    "ratio_jump": 0.08,
    # 揮拍比值和目標差超過此值，--debug 時自動存截圖到 debug_shots 資料夾
    "save_off_by": 0.08,
    # 內圈最多是外圈半徑的多少（None = 不限制）。外圈線條內側有光暈的模式設這個，避免把光暈當成內圈
    "inner_max_frac": None,
    # 光圈黏到球拍等東西、輪廓不圓時，改用剔除離群點的圓擬合再判斷一次
    "ring_robust_fit": False,
    # 引拍中還沒揮、光圈就變黃色（= 擊球點）時立刻補揮。黃色的 HSV 範圍（實測約 H24 S160 V252）
    "yellow_fire": False,
    "yellow_hsv_lo": [18, 100, 180],
    "yellow_hsv_hi": [32, 255, 255],
    # 內圈小於這個半徑就當作量錯（雜訊）
    "inner_min_r": 3,
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
PROFILE_CAL_KEYS = ("region", "output_idx", "monitor_origin", "can_area")
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
        pts = c.reshape(-1, 2).astype(np.float32)
        dist = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
        if cfg["ring_robust_fit"] and (dist.std() / r > cfg["max_roundness_err"]
                                       or ring_coverage(blue, red, (cx, cy), r)[0] < cfg["min_coverage"]):
            # 光圈黏到球拍：外接圓被球拍撐大、圓心偏掉。改用輪廓點擬合圓，剔除離圓太遠的點（球拍那段）
            fit = robust_circle(pts)
            if fit is not None:
                cx, cy, r = fit
                dist = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
                dist = dist[np.abs(dist - r) <= 3]
        if not (r_min <= r <= r_max):
            continue
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


def yellow_ring_near(hsv_img, center, r_hi, cfg):
    """光圈縮到碰到內圈時會整圈變黃（= 擊球點）。在上一個光圈的位置找黃色圓圈，回傳線條完整度最高的比例"""
    h, w = hsv_img.shape[:2]
    cx, cy = center
    x0, y0 = max(int(cx - r_hi - 3), 0), max(int(cy - r_hi - 3), 0)
    x1, y1 = min(int(cx + r_hi + 4), w), min(int(cy + r_hi + 4), h)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    yellow = cv2.inRange(hsv_img[y0:y1, x0:x1], tuple(cfg["yellow_hsv_lo"]), tuple(cfg["yellow_hsv_hi"]))
    best = 0.0
    for r in np.arange(6, r_hi + 0.01, 1.0):
        xs = np.clip(np.round(cx - x0 + r * np.cos(RING_ANGLES)).astype(int), 0, x1 - x0 - 1)
        ys = np.clip(np.round(cy - y0 + r * np.sin(RING_ANGLES)).astype(int), 0, y1 - y0 - 1)
        best = max(best, float((yellow[ys, xs] > 0).mean()))
    return best


def robust_circle(pts):
    """輪廓點擬合圓，逐步剔除離圓太遠的點。圓上的點要佔一半以上才算，回傳 (cx, cy, r) 或 None"""
    xs, ys = pts[:, 0].astype(np.float64), pts[:, 1].astype(np.float64)
    keep = np.ones(len(xs), bool)
    for tol in (None, 0.2, 0.08):
        cx, cy, r = fit_circle(xs[keep], ys[keep])
        if tol is not None and r > 0:
            keep = np.abs(np.hypot(xs - cx, ys - cy) - r) <= max(tol * r, 2)
            if keep.sum() < max(20, 0.5 * len(xs)):
                return None
    cx, cy, r = fit_circle(xs[keep], ys[keep])
    return cx, cy, r


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


def match_for(side, cfg, action=None):
    if cfg["swing_mode"] == "drive":
        return cfg["match_scale_drive"]
    if action and f"match_scale_{action}" in cfg:
        # 罐子、水果模式各打法分開調；再依正反手加減（比值越大越早出手）
        return cfg[f"match_scale_{action}"] + cfg["match_side_offset"].get(resolve_side(side, cfg), 0)
    return cfg["match_scale_forehand"] if resolve_side(side, cfg) == "forehand" else cfg["match_scale"]


def table_hull(hsv_img, cfg):
    """回傳 (桌面藍色遮罩, 桌面外框填滿的遮罩) 或 None。中線、網子、罐子會把桌面切成好幾塊，大塊的都算桌面"""
    table = cv2.inRange(hsv_img, tuple(cfg["table_hsv_lo"]), tuple(cfg["table_hsv_hi"]))
    n, labels, st, _ = cv2.connectedComponentsWithStats(table)
    if n < 2:
        return None
    areas = st[1:, cv2.CC_STAT_AREA]
    big = np.isin(labels, 1 + np.flatnonzero(areas >= 0.1 * areas.max()))
    hull = np.zeros_like(table)
    cv2.fillConvexPoly(hull, cv2.convexHull(cv2.findNonZero(big.astype(np.uint8))), 255)
    return table, hull


BOMB_ANGLES = np.linspace(0, 2 * np.pi, 36, endpoint=False)
_bomb_cache = {"t": 0.0, "hull": None, "box": None}


def find_bomb(hsv_img, cfg):
    """
    水果模式：找飛在桌面上方的黑色炸彈。桌面範圍（左右各放寬 bomb_side_px，貼著桌邊飛的也找得到）內、
    很暗、圓、周圍大多是桌面藍色或紫色地板、而且旁邊有引信的橘黃色火花。
    水果落在地板上的影子、網柱的黑色小腳都沒有火花；球員的黑頭髮連著衣服、形狀不圓；都會被排除。回傳 (x, y) 或 None
    """
    # 桌子不會動，桌面外框每秒重找一次就好（每張都找要 8ms，處理速度會掉一半）
    now = time.perf_counter()
    if now - _bomb_cache["t"] > 1.0 or _bomb_cache["hull"] is None:
        found = table_hull(hsv_img, cfg)
        if found is None:
            return None
        side = cfg["bomb_side_px"]
        hull = cv2.dilate(found[1], np.ones((1, 2 * side + 1), np.uint8))
        x, y, w, h = cv2.boundingRect(hull)
        _bomb_cache.update(t=now, hull=hull[y:y + h, x:x + w], box=(x, y, w, h))
    bx, by, bw, bh = _bomb_cache["box"]
    hull = _bomb_cache["hull"]
    sub = hsv_img[by:by + bh, bx:bx + bw]
    if sub.shape[:2] != hull.shape:
        _bomb_cache["hull"] = None   # 畫面大小變了（重新校準），下一張重找
        return None
    table = cv2.inRange(sub, tuple(cfg["table_hsv_lo"]), tuple(cfg["table_hsv_hi"]))
    # 周圍可以是桌面或紫色地板（貼著桌邊飛的炸彈一半在桌外）
    table |= cv2.inRange(sub, tuple(cfg["floor_hsv_lo"]), tuple(cfg["floor_hsv_hi"]))
    spark = cv2.inRange(sub, tuple(cfg["spark_hsv_lo"]), tuple(cfg["spark_hsv_hi"]))
    dark = cv2.inRange(sub, (0, 0, 0), (179, 255, cfg["bomb_v_max"])) & hull
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, _, st, cen = cv2.connectedComponentsWithStats(dark)
    h_img, w_img = table.shape
    a_min, a_max = cfg["bomb_area"]
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if not (a_min <= area <= a_max) or not (0.6 <= w / h <= 1.6) or area / (w * h) < 0.5:
            continue
        cx, cy = cen[i]
        r = (w + h) / 4
        bg = []
        for d in (r + 3, r + 6):
            xs = np.clip(np.round(cx + d * np.cos(BOMB_ANGLES)).astype(int), 0, w_img - 1)
            ys = np.clip(np.round(cy + d * np.sin(BOMB_ANGLES)).astype(int), 0, h_img - 1)
            bg.append(table[ys, xs] > 0)
        if np.mean(bg) < cfg["bomb_bg_ratio"]:
            continue
        # 引信的火花在炸彈旁邊約 2 倍半徑內，很小；整顆橘子、檸檬、蘋果太大，不算火花
        R = int(2.2 * r)
        n_spark = cv2.countNonZero(spark[max(int(cy) - R, 0):int(cy) + R, max(int(cx) - R, 0):int(cx) + R])
        if not (cfg["bomb_spark_px"][0] <= n_spark <= cfg["bomb_spark_px"][1]):
            continue
        if fruit_nearby(hsv_img, cx + bx, cy + by, cfg):
            continue   # 旁邊有一大塊水果色（橘子、檸檬、蘋果、西瓜）= 是水果，不是炸彈
        return float(cx + bx), float(cy + by)
    return None


def fruit_nearby(hsv_img, x, y, cfg):
    """(x, y) 附近有沒有一大塊高飽和的水果色。火花顏色和橘子一樣但很小；球拍的綠色那面飽和度低，不算"""
    R = cfg["fruit_check_px"]
    sub = hsv_img[max(int(y) - R, 0):int(y) + R, max(int(x) - R, 0):int(x) + R]
    if sub.size == 0:
        return False
    m = None
    for lo, hi in cfg["fruit_hsv"]:
        part = cv2.inRange(sub, tuple(lo), tuple(hi))
        m = part if m is None else m | part
    _, _, st, _ = cv2.connectedComponentsWithStats(m)
    return bool((st[1:, cv2.CC_STAT_AREA] >= cfg["fruit_min_px"]).any())


def direction(dx, dy):
    """拖曳方向的文字，例如 往右上"""
    return "往" + ("右" if dx > 0 else "左" if dx < 0 else "") + ("下" if dy > 0 else "上" if dy < 0 else "")


def find_can(hsv_img, cfg):
    """
    罐子模式：在 can_area 裡找罐子。藍色桌面圍起來的範圍內、不是桌面藍色的那一塊就是罐子
    （白線很細，開運算會去掉）。回傳 (x, y, 路線 left/middle/right) 或 None，座標是整個遊戲區的。
    """
    x0, y0, x1, y1 = cfg["can_area"]
    found = table_hull(hsv_img[y0:y1, x0:x1], cfg)
    if found is None:
        return None
    table, hull = found
    cand = cv2.morphologyEx(hull & ~table, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, _, st, cen = cv2.connectedComponentsWithStats(cand)
    if n < 2:
        return None
    i = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
    if st[i, cv2.CC_STAT_AREA] < cfg["can_min_area"]:
        return None
    cx, cy = cen[i]
    row = np.flatnonzero(hull[int(cy)])
    frac = (cx - row[0]) / max(row[-1] - row[0], 1)
    split = cfg["can_lane_split"]
    lane = "left" if frac < split else "right" if frac > 1 - split else "middle"
    return float(cx + x0), float(cy + y0), lane


def can_action(side, lane, cfg):
    """依正反手和罐子路線決定打法：正手打右路、反手打左路是直線，反過來是斜線"""
    side = resolve_side(side, cfg)
    if lane == "middle":
        kind = "middle"
    else:
        kind = "straight" if (lane == "right") == (side == "forehand") else "cross"
    return cfg["can_actions"][side][kind]


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
    hi = bins - k
    if cfg.get("inner_max_frac"):
        # 只在外圈半徑的這個比例內找：外圈線條內側的光暈很貼近外圈，不會被當成內圈
        hi = min(hi, int(max_r * cfg["inner_max_frac"] / px_per_bin))
    if hi - lo < 4:
        return None
    contrast = polar[:, lo:hi] - (polar[:, lo - k:hi - k] + polar[:, lo + k:hi + k]) / 2
    contrast = np.nan_to_num(contrast, nan=-1e9)
    idx = np.argmax(contrast, axis=1)
    if cfg["inner_pick_first"]:
        # 從圓心往外第一個夠亮的圈：外圈線條內側的光暈比灰色內圈亮，取最亮的會抓到光暈
        strong = contrast >= np.maximum(cfg["inner_min_contrast"], 0.4 * contrast[np.arange(rows), idx])[:, None]
        idx = np.argmax(strong, axis=1)
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
    return fr if cfg["inner_min_r"] < fr < max_r else None


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
        self.action = None  # 罐子模式這球的打法
        self.side = "forehand"
        self.release_t = 0.0  # 上次放開滑鼠的時間
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

    def prepare(self, side, action):
        self.jobs.put(lambda: self._prepare(side, action))

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

    def _pull(self, side, press=None, dx=0.0, dy=None):
        """依正反手決定起點，按下並往下拖曳引拍，之後保持按住（tap 模式只移游標，不按；發球時強制按）"""
        cfg = self.cfg
        self.action = None
        side = self.side = resolve_side(side, cfg)
        self.x = self.w * cfg["backhand_x" if side == "backhand" else "forehand_x"]
        self.y = self.h * cfg["swipe_y"]
        move_cursor(self.ox + self.x, self.oy + self.y)
        if not (cfg["swing_mode"] != "tap" if press is None else press):
            return
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        self.down = True
        if dy is None:
            dy = self.h * cfg["pull_ratio"]
        self._drag(self.x + dx, self.y + dy, cfg["pull_ms"])
        print(f"\n[滑鼠] 按下並{direction(dx, dy)}拖（引拍），游標在 ({int(self.ox + self.x)}, {int(self.oy + self.y)})")

    def gesture_sx(self, side):
        """gestures 是正手的方向；反手左右相反（gesture_mirror_backhand）"""
        mirror = self.cfg["gesture_mirror_backhand"] and resolve_side(side, self.cfg) == "backhand"
        return -1 if mirror else 1

    def _prepare(self, side, action):
        """罐子模式：依這球的打法就位。drive、flick 按住往下拖引拍（flick 斜著拖：正手右下、反手左下）；tap 只移游標"""
        self._release()
        # 上一拍的揮拍動畫還沒做完就按下，遊戲會忽略，這球的引拍和揮拍都不算
        wait = self.pull_ready_at() - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        g = self.cfg["gestures"].get(action)
        if g:
            # 水果模式：照 gestures 設定的方向引拍（反手左右相反）
            sx = self.gesture_sx(side)
            self._pull(side, press=True, dx=sx * self.w * g["pull"][0], dy=self.h * g["pull"][1])
        else:
            dx = 0.0
            if action == "flick":
                dx = self.w * self.cfg["flick_pull_dx_ratio"] * (1 if resolve_side(side, self.cfg) == "forehand" else -1)
            self._pull(side, press=action in ("drive", "flick"), dx=dx)
        self.action = action

    def _swing(self):
        """從引拍位置往上滑並放開（tap 則在左/右側點一下；flick 斜著推：正手往左上、反手往右上）"""
        if (self.action or self.cfg["swing_mode"]) == "tap":
            move_cursor(self.ox + self.x, self.oy + self.y)
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(self.cfg["tap_hold_ms"] / 1000)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            return
        dx, dy = 0.0, -self.h * self.cfg["swing_ratio"]
        g = self.cfg["gestures"].get(self.action) if self.action else None
        if g:
            dx, dy = self.gesture_sx(self.side) * self.w * g["swing"][0], self.h * g["swing"][1]
        elif self.action == "flick":
            dx = self.w * self.cfg["flick_dx_ratio"] * (-1 if self.side == "forehand" else 1)
        self._drag(self.x + dx, self.y + dy, self.cfg["swing_ms"])
        self._release()
        self.release_t = time.perf_counter()
        print(f"\n[滑鼠] {direction(dx, dy)}滑並放開（揮拍）")
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
        self.release_t = time.perf_counter()
        print("\n[滑鼠] 發球：往上推並放開")
        if cfg["swing_mode"] == "drive":
            time.sleep(cfg["repull_gap_ms"] / 1000)
            self._pull("forehand")

    def _release(self):
        if self.down:
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            self.down = False

    def pull_ready_at(self):
        """最早什麼時候可以再按下引拍（上次揮完放開 + repull_gap_ms）。只是取消引拍、沒有揮拍動畫就不用等"""
        return self.release_t + self.cfg["repull_gap_ms"] / 1000


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

    if cfg["swing_mode"] == "can":
        win = "Drag to select OPPONENT HALF of the table (above the net, where the can is), then ENTER"
        cv2.namedWindow(win, cv2.WINDOW_NORMAL)
        cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        cx, cy, cw, ch = cv2.selectROI(win, crop, showCrosshair=False)
        cv2.destroyWindow(win)
        if cw == 0 or ch == 0:
            raise SystemExit("沒有框選對手桌面")
        cfg["can_area"] = [int(cx), int(cy), int(cx + cw), int(cy + ch)]
        found = find_can(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV), cfg)
        if found is None:
            print("[校準] ⚠ 對手桌面上找不到罐子，可能要調 table_hsv_lo / table_hsv_hi 或 can_min_area")
        else:
            print(f"[校準] 罐子在 ({found[0]:.0f}, {found[1]:.0f})，判斷為 {found[2]}")

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
    # 先存檔：驗證視窗被按 X 關掉時 waitKey 不會返回，放在後面會存不到
    save_config(cfg)
    if not cfg.get("_profile"):
        print(f"[校準] 已儲存到 {CONFIG_PATH}")

    vis = cv2.resize(vis, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
    win = "check: green=outer red=inner (any key)"
    cv2.imshow(win, vis)
    cv2.imshow("check mask (any key)", mask)
    # 按任意鍵，或把驗證視窗按 X 關掉，都繼續
    while cv2.waitKey(100) == -1 and cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) >= 1:
        pass
    cv2.destroyAllWindows()


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
                 "drive": "全部正手拉球", "can": "打罐子（依罐子位置選打法）",
                 "fruit": "打水果（炸彈用另一種打法）"}.get(cfg["swing_mode"], cfg["swing_mode"])
    print(f"模式：{mode_name}")
    print("就緒：F7 測試揮拍，F8 開始/暫停，F9 重新校準，F10 結束")

    drive = cfg["swing_mode"] == "drive"
    can_mode = cfg["swing_mode"] == "can"
    can_lane = cfg["can_default_lane"]  # 罐子模式：罐子目前在哪一路
    can_pos = None          # 最近一次找到的罐子位置
    fruit_mode = cfg["swing_mode"] == "fruit"
    bomb_inner_cfg = dict(cfg, inner_pick_first=True)
    planned = can_mode or fruit_mode    # 每球依畫面選打法的模式
    bomb_track = deque(maxlen=6)  # 水果模式：最近看到炸彈的 (時間, x, y)
    bomb_seen = -10.0       # 最後一次確認有炸彈往我方飛來的時間
    last_fire = -10.0       # 最後一次揮拍的時間（之前看到的炸彈屬於上一球）
    prev_phase = "idle"     # 上一張的階段，用來抓「剛揮完」的那一刻
    held_action = None      # 罐子、水果模式這球的打法
    held_r = None           # 本球最近一次的外圈半徑（ring_shrink_only 用）
    shot_rs = []            # 本球每張的外圈半徑，取消時印出來看光圈怎麼縮
    held_center = None      # 本球最近一次的光圈圓心（找黃色光圈用）
    settle_until = 0.0      # 按住引拍後光圈大小會突然改變，這個時間之前不量光圈、不出手

    def pulls(action):
        """這種打法光圈出現時要先按住引拍（tap 不用）"""
        return action in ("drive", "flick") or action in cfg["gestures"]

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
    inners = deque(maxlen=cfg["inner_window"])
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
            ring_cfg = cfg
            if cfg["ring_shrink_only"] and phase == "held" and held_r:
                # 同一球的光圈只會縮小：比上一張大很多的圓（炸彈爆炸的光暈等）不算
                ring_cfg = dict(cfg, ring_r_range=[cfg["ring_r_range"][0], held_r * 1.15])
            ring, mask = find_ring(hsv, ring_cfg)
            inner = ratio = ttm_ms = slope = ratio_now = None
            measured = None
            inner_src = "-"

            if ring is not None:
                # 炸彈球引拍後內圈變小，外圈內側的亮光暈會被當成內圈：改取從圓心往外第一個亮圈
                inner_cfg = bomb_inner_cfg if fruit_mode and held_action == "bomb" else cfg
                measured = measure_inner(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), mask, ring, inner_cfg)
                if (inner_cfg is bomb_inner_cfg and measured is not None
                        and not (cfg["bomb_inner_range"][0] <= measured <= cfg["bomb_inner_range"][1])):
                    # 炸彈引拍後內圈固定約 13~15px；光圈中間是黑炸彈時常把反光量成 9~11px，取最小值會讓出手太晚
                    measured = None
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
                    # 內圈會在引拍後慢慢縮小的模式取最小值：中位數會落後一兩張，用舊的大內圈算會太早揮
                    inner = float(min(cand) if cfg["inner_use_min"] else np.median(cand))
                    inner_src = "實測"
                elif fruit_mode and held_action == "bomb" and phase == "held" and cfg["bomb_inner_default"]:
                    # 炸彈引拍後的內圈很固定（實測 13~15px），落在頭上量不到時用預設值照常出手，不然只能等到變黃才補揮
                    inner, inner_src = float(cfg["bomb_inner_default"]), "炸彈預設"
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
                if now < settle_until:
                    # 剛按住引拍：引拍方式會讓光圈大小突然改變，等引拍完成才開始量
                    inner, inner_src = None, "等引拍"
                if measured is not None and inner is not None:
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
                if phase == "held" and miss_streak == 1 and fruit_mode:
                    save_shot("光圈消失原圖", frame.copy())   # 不加標記的原圖，查為什麼抓不到
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

            def plan(side):
                if fruit_mode:
                    # 只認上次揮拍之後看到的炸彈（上一球的不算）
                    recent = bomb_seen > last_fire and now - bomb_seen < cfg["bomb_memory_ms"] / 1000
                    return "bomb" if recent else "fruit"
                return can_action(side, can_lane, cfg) if can_mode else None

            # 水果模式：炸彈往我方飛（畫面上往下）才算來球，自己打回去的往上飛不算。
            # 揮完的冷卻期間也要看：對手回球快時，炸彈過網那段剛好在冷卻中，等冷卻完才看到就太近了
            bomb = find_bomb(hsv, cfg) if fruit_mode else None
            if bomb is not None:
                while bomb_track and now - bomb_track[0][0] > 0.3:
                    bomb_track.popleft()
                bomb_track.append((now, *bomb))
                # 剛揮完的一小段時間不算：被我們打到的炸彈還會往下飛一兩張才反彈，會被誤當成下一球
                if (len(bomb_track) >= 2 and bomb_track[-1][2] - bomb_track[0][2] >= cfg["bomb_min_dy"]
                        and now - last_fire > cfg["bomb_ignore_after_swing_ms"] / 1000):
                    if bomb_seen <= last_fire:   # 這球第一次看到才印
                        print(f"\n[炸彈] 來球是炸彈 ({bomb[0]:.0f}, {bomb[1]:.0f})"
                              + ("（冷卻中看到）" if phase == "cooldown" else ""))
                    bomb_seen = now

            # 罐子會移動，等球和引拍中都持續找，出手前換路就改打法
            if can_mode and phase in ("idle", "held"):
                found = find_can(hsv, cfg)
                if found is not None:
                    can_pos = found[:2]
                    if found[2] != can_lane:
                        can_lane = found[2]
                        lane_name = {"left": "左路", "middle": "中間", "right": "右路"}[can_lane]
                        print(f"\n[罐子] 在{lane_name} ({can_pos[0]:.0f}, {can_pos[1]:.0f})")

            if phase == "held":
                match = match_for(held_side, cfg, held_action)
            else:
                side_now = ring["side"] if ring else "backhand"
                match = match_for(side_now, cfg, plan(side_now))
            ttm_ms = ttm_for(match)

            vis = None
            if debug:
                vis = frame.copy()
                if bomb is not None:
                    cv2.circle(vis, tuple(map(int, bomb)), 12, (0, 0, 255), 2)
                    cv2.putText(vis, "bomb", (int(bomb[0]) + 14, int(bomb[1])),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
                if can_mode:
                    x0, y0, x1, y1 = cfg["can_area"]
                    cv2.rectangle(vis, (x0, y0), (x1, y1), (255, 255, 0), 1)
                    if can_pos is not None:
                        cv2.circle(vis, tuple(map(int, can_pos)), 8, (0, 255, 255), 2)
                        cv2.putText(vis, can_lane, (int(can_pos[0]) + 10, int(can_pos[1])),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
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
                act_txt = f" {held_action}（罐子{can_lane}）" if can_mode else f" {held_action}" if fruit_mode else ""
                print(f"\n[{tag}] {held_side}{act_txt}  外圈 {ring['r']:.1f}px / 內圈 {inner:.1f}px（{inner_src}）"
                      f" = {ratio:.2f}（目標 {match}），預估差 {ttm_txt}，藍 {side_counts[0]} / 紅 {side_counts[1]}")
                if tag != "揮拍" or abs(ratio - match) > cfg["save_off_by"]:
                    save_shot(tag, vis)
                if fruit_mode:
                    # 標記會蓋住光圈細節，每次揮拍都另存原圖查內圈
                    save_shot("炸彈原圖" if held_action == "bomb" else "水果原圖", frame.copy())

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
                    held_r = ring["r"]
                    held_center = ring["center"]
                    shot_rs = [ring["r"]]
                    side_counts = [ring["n_blue"], ring["n_red"]]
                    held_action = plan(held_side)
                    match = match_for(held_side, cfg, held_action)
                    ttm_ms = ttm_for(match)
                    if planned:
                        mouse.prepare(held_side, held_action)
                    elif not drive:
                        mouse.pull(held_side)
                    elif not mouse.busy() and not left_button_down():
                        # 按 F8 時不先按住（使用者還要點遊戲的開始），光圈出現才按下引拍；
                        # 中途被使用者點擊打斷、左鍵已放開，也在這裡補按回來
                        mouse.pull("forehand")
                        drive_ready = True
                    shot_frames, shot_inner_hits = 1, int(measured is not None)
                    last_ratio = ratio  # 不沿用上一球的最後比值，避免誤觸補揮
                    if planned and pulls(held_action):
                        settle_until = max(now, mouse.pull_ready_at()) + (cfg["pull_ms"] + cfg["pull_settle_ms"]) / 1000
                        inners.clear(); rejected.clear(); history.clear(); last_ratio = None
                    if now >= settle_until and should_fire(match):
                        # 光圈被偵測到時已到出手時機（快球常見）：立刻出手
                        mouse.swing()
                        log_swing("急揮")
                        phase, phase_t = "cooldown", now
                    else:
                        phase, phase_t = "held", now
            elif phase == "held":
                if ring is not None:
                    shot_frames += 1
                    held_r = ring["r"]
                    held_center = ring["center"]
                    shot_rs.append(ring["r"])
                    shot_inner_hits += measured is not None
                    side_counts[0] += ring["n_blue"]
                    side_counts[1] += ring["n_red"]
                    new_side = resolve_side(side_from_counts(*side_counts, cfg), cfg)
                    # tap 只是把游標移到另一邊，可以改；swipe 已經按住拖曳就不改
                    # 罐子模式換手就重新就位（drive 已按住會先放開再引拍）
                    if new_side != held_side and (cfg["swing_mode"] == "tap" or planned):
                        print(f"\n[改手] {held_side} → {new_side}（累計 藍 {side_counts[0]} / 紅 {side_counts[1]}）")
                        held_side = new_side
                        held_action = plan(held_side)
                        match = match_for(held_side, cfg, held_action)
                        ttm_ms = ttm_for(match)
                        if planned:
                            mouse.prepare(held_side, held_action)
                            if pulls(held_action):
                                settle_until = max(now, mouse.pull_ready_at()) + (cfg["pull_ms"] + cfg["pull_settle_ms"]) / 1000
                                inners.clear(); rejected.clear(); history.clear(); last_ratio = None
                        else:
                            mouse.pull(held_side)
                    elif (planned and plan(held_side) != held_action
                          and (can_mode or plan(held_side) == "bomb")   # 水果模式只會改成炸彈，不會改回來
                          and (ttm_ms is None or ttm_ms > cfg["can_replan_ms"])):
                        # 罐子在光圈出現後才移動、引拍後才看到炸彈：改打法；快出手了來不及重新引拍就不改
                        why = f"罐子移到 {can_lane}" if can_mode else "看到炸彈"
                        print(f"\n[改打法] {why}：{held_action} → {plan(held_side)}")
                        held_action = plan(held_side)
                        match = match_for(held_side, cfg, held_action)
                        ttm_ms = ttm_for(match)
                        mouse.prepare(held_side, held_action)
                        if pulls(held_action):
                            settle_until = max(now, mouse.pull_ready_at()) + (cfg["pull_ms"] + cfg["pull_settle_ms"]) / 1000
                            inners.clear(); rejected.clear(); history.clear(); last_ratio = None
                if should_fire(match):
                    mouse.swing()
                    log_swing("揮拍")
                    phase, phase_t = "cooldown", now
                    history.clear()
                elif (cfg["yellow_fire"] and ring is None and held_center is not None
                      and yellow_ring_near(hsv, held_center, held_r * 1.2, cfg) >= 0.6):
                    # 光圈變黃色 = 已到擊球點，還沒揮就立刻補揮（例如很晚才認出炸彈、重新引拍後量不到內圈）
                    mouse.swing()
                    print(f"\n[黃色補揮] {held_side} {held_action}  光圈變黃色（擊球點）還沒出手，立刻補揮"
                          f"（最後比值 {'-' if last_ratio is None else f'{last_ratio:.2f}'}，目標 {match:.2f}）")
                    step = max(1, len(shot_rs) // 12)
                    print("  外圈變化：" + " → ".join(f"{r:.0f}" for r in shot_rs[::step]) + "px")
                    save_shot("黃色補揮", frame.copy())
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
                    step = max(1, len(shot_rs) // 10)
                    print("  外圈變化：" + " → ".join(f"{r:.0f}" for r in shot_rs[::step]) + "px")
                    save_shot("取消", last_vis)
                    phase = "idle"
            elif (phase == "cooldown" and now - phase_t > cfg["cooldown_ms"] / 1000
                  and (now - last_seen > cfg["rearm_ms"] / 1000
                       # 水果模式下一球的光圈常常一回球就出現，光圈不會中斷：比剛才出手時大很多 = 新的一球
                       or (fruit_mode and ring is not None and held_r and ring["r"] > held_r * 1.5))):
                if now - last_seen <= cfg["rearm_ms"] / 1000:
                    print(f"\n[下一球] 光圈 {ring['r']:.0f}px（剛才出手時 {held_r:.0f}px）")
                    inners.clear()
                    rejected.clear()
                    history.clear()
                phase = "idle"

            if phase == "cooldown" and prev_phase != "cooldown":
                last_fire = now
                bomb_track.clear()
            prev_phase = phase

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
    if args.calibrate or not cfg["region"] or (cfg["swing_mode"] == "can" and not cfg["can_area"]):
        calibrate(cfg)
    if args.drive:
        cfg["swing_mode"] = "drive"

    while run(cfg, args.debug) == "recal":
        calibrate(cfg)


if __name__ == "__main__":
    main()
