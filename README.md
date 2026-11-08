# MATDRIVE 自動揮拍 使用說明

僅限 **AI 挑戰模式 / 練習** 使用，不要用在線上對戰或排位。

## 快速查詢

先進資料夾：`cd C:\ai助手\pingpong_bot`

**執行**

| 指令 | 用途 |
|---|---|
| `python bot.py` | 用 `config.json` 的設定執行（最快） |
| `python bot.py --debug` | 加除錯視窗和異常截圖 |
| `python bot.py --drive` | 這次全部正手拉球（不改設定檔） |
| `python bot.py --calibrate` | 先重新校準再執行 |

**設定組（各模式）**

| 指令 | 用途 |
|---|---|
| `python bot.py --profile 桌機-一般AI挑戰` | 一般 AI 挑戰 |
| `python bot.py --profile 桌機-急速模式` | 近鏡頭、越打越快（러시 ON） |
| `python bot.py --profile 桌機-罐子模式` | 打桌上的罐子（第一次會先校準） |
| `python bot.py --profile 桌機-水果模式` | AI 打水果過來，炸彈用另一種打法 |
| `python bot.py --profile 名稱 --calibrate` | 重新校準該設定組（只存回該設定組） |
| `python bot.py --profile 新名稱` | 不存在就新建（先複製目前設定） |

參數可以組合，例如 `python bot.py --profile 桌機-罐子模式 --debug`

**錄影（給追蹤球模式用）**

| 指令 | 用途 |
|---|---|
| `python record.py` | 錄整個遊戲畫面（第一次會先框範圍） |
| `python record.py --select` | 重新框錄影範圍 |

**熱鍵**

| 按鍵 | bot.py | record.py |
|---|---|---|
| F2 | — | 錄 2 秒 |
| F6 | 校準時截圖 | — |
| F7 | 測試揮拍一次 | — |
| F8 | 開始 / 暫停 | — |
| F9 | 重新校準 | — |
| F10 | 結束 | — |
| Ctrl+C | — | 結束 |

> 切到別的視窗前先按 **F8** 暫停，避免誤判畫面亂發球。

## 一、第一次安裝（只做一次）

```powershell
winget install Python.Python.3.12        # 裝完重開終端機
cd C:\ai助手\pingpong_bot
python -m pip install -r requirements.txt
```

## 二、每次使用

1. 打開瀏覽器進入 matdrive.app 的 **AI 挑戰模式**
2. 開終端機執行：
   ```powershell
   cd C:\ai助手\pingpong_bot
   python bot.py --debug
   ```
   （不想看除錯視窗、想更快就用 `python bot.py`；想全部正手拉球就加 `--drive`，例如 `python bot.py --drive`）
3. 有存過設定就直接進入「就緒」，否則會先校準（見第三節）
4. 回到遊戲，按 **F7** 試揮一次，確認球員有揮拍
5. 按 **F8** 開始自動打，再按 F8 暫停
6. 打完按 **F10** 結束

> 程式運作時會控制滑鼠，想用滑鼠前先按 F8 暫停。

## 三、校準（第一次，或視窗移動 / 換螢幕 / 改大小後）

觸發方式：第一次執行自動進入、執行中按 **F9**、或 `python bot.py --calibrate --debug`

1. **截圖**，兩種方式擇一：
   - A. 在遊戲中光圈（球員旁的藍/紅圓圈）出現時按 **F6**（筆電可能要 **Fn + F6**）
   - B. 在終端機按 **Enter**，5 秒內切到遊戲並讓光圈出現（可先按遊戲右上角 **Pause** 停住）
   - 截圖時**滑鼠游標要停在遊戲所在的螢幕**，程式會截那個螢幕
2. **框遊戲區**：跳出全螢幕截圖，用滑鼠拖一個方框框住遊戲畫面（不含網址列、工作列），按 **Enter**
3. **看驗證圖**：綠圈要套在外圈上、紅圈要疊在灰色內圈上 → 按任意鍵關閉
4. 終端機出現「已儲存」即完成，下次不用再做

## 設定組（不同電腦 / 螢幕 / 遊戲模式）

各平台和模式的校準位置、出手時機存在 `profiles.json`，執行時用 `--profile 名稱` 選：

```powershell
python bot.py --profile 桌機-急速模式
python bot.py --profile 桌機-一般AI挑戰 --drive
```

- 名稱不存在就自動新建（先複製目前設定）；畫面位置不同就加 `--calibrate` 或執行中按 F9，校準結果會存回這個設定組，不動 `config.json`
- 每個設定組可以放 `config.json` 裡任何設定（例如 `match_scale`、`auto_timing_bar`），沒寫的就用 `config.json` 的值
- 不加 `--profile` 就跟以前一樣只用 `config.json`

## 罐子模式（打桌上的罐子）

```powershell
python bot.py --profile 桌機-罐子模式 --debug
```

- 第一次會先校準：截圖時要有光圈**和桌上的罐子**，先框整個遊戲區（要包含對手那半張桌子），再框「對手半場桌面」（畫面上網子**上方**、放罐子的那半張桌子，不要框到網子）
- 程式在等球時看罐子在對手桌面的左路 / 中間 / 右路，再配合這球是正手還是反手決定打法：

| 罐子位置 | 正手 | 反手 |
|---|---|---|
| 右路 | 直線：block 點擊 | 斜線：往左下拖引拍 → 往右上推 |
| 左路 | 斜線：往右下拖引拍 → 往左上推 | 直線：反手 block |
| 中間 | 從下拖住往上推 | 從下拖住往上推 |

- 打法改 `can_actions`：`tap`（點擊 block）、`flick`（斜線揮拍）、`drive`（打中間）；flick 和 drive 都是光圈一出現就按住往下拖引拍，時機到往上推，flick 往上推時會斜著推（正手左上、反手右上，斜的程度調 `flick_dx_ratio`，預設 0.25）；引拍往反方向斜著拖（正手右下、反手左下，調 `flick_pull_dx_ratio`，預設 0.1）
- 時機：block 用 `match_scale` / `match_scale_forehand`，揮拍用 `match_scale_flick`，拖住往上用 `match_scale_drive`
- 罐子會移動：引拍中也持續看罐子，換路就重新就位改打法（終端機印 `[改打法]`）；離出手不到 `can_replan_ms`（預設 100 毫秒）就不改，來不及重新引拍
- 罐子位置判斷錯：調 `can_lane_split`（預設 0.35，中間範圍太小就調小）；找不到罐子：調 `can_min_area` 或 `table_hsv_lo` / `table_hsv_hi`

## 水果模式（AI 打水果、炸彈過來）

```powershell
python bot.py --profile 桌機-水果模式 --debug
```

| 來球 | 正手：引拍 → 揮拍 | 反手：引拍 → 揮拍 |
|---|---|---|
| 水果 | 往右上 → 往左下 | 往左上 → 往右下 |
| 黑色炸彈 | 往右下 → 往左上 | 往左下 → 往右上 |

（`gestures` 寫的是正手方向，反手自動左右相反；不要反手相反就把 `gesture_mirror_backhand` 改成 `false`）

- 程式看桌面上方有沒有往我方飛來的黑色圓形物體，有就當作炸彈（終端機印 `[炸彈]`）；引拍後才看到炸彈會改打法（印 `[改打法]`）
- 按住引拍後光圈大小會突然改變（不同引拍方式大小不同），所以程式引拍完成後才開始量光圈、判斷時機（等多久調 `pull_settle_ms`，預設 100 毫秒）；中途改打法會重新引拍、重新量
- 畫面範圍沿用罐子模式，不同就加 `--calibrate`
- 拖曳方向和距離改 `gestures`；時機調 `match_scale_fruit` / `match_scale_bomb`
- 炸彈沒抓到：調大 `bomb_v_max`（預設 70，炸彈不夠黑）或調小 `bomb_bg_ratio`；把水果誤認成炸彈：調小 `bomb_v_max`

## 四、熱鍵總表

| 按鍵 | 功能 |
|---|---|
| F6 | 校準時截圖 |
| F7 | 測試揮拍一次 |
| F8 | 開始 / 暫停 |
| F9 | 重新校準 |
| F10 | 結束程式 |

## 五、調整時機（改 `config.json` 後重新執行）

每揮一次終端機會印出：
```
[揮拍] forehand  外圈 32.6px / 內圈 23.3px = 1.40（目標 1.4），預估差 3ms
```

| 狀況 | 要改的設定 | 怎麼改 |
|---|---|---|
| 反手太慢 / 太快 | `match_scale`（預設 1.3） | 太慢調大、太快調小，每次 ±0.05 |
| 正手太慢 / 太快 | `match_scale_forehand`（預設 1.4） | 同上 |
| 正反手用錯邊 | `backhand_x` / `forehand_x` | 兩個數值對調 |
| 想換回拖曳揮拍 | `swing_mode` | `"tap"`（點擊，預設）或 `"swipe"`（往下拖再往上滑） |
| 想全部正手拉球 | `swing_mode` | `"drive"`：一直按住正手引拍，時機到往上推，推完立刻再引拍（或執行時加 `--drive`） |
| 不想自動發球 | `auto_serve` | 改成 `false`（預設 `true`：拍子右側出現浮動白球圖示就自動引拍發球） |
| 發球太快 / 太慢揮出 | `serve_hold_ms`（預設 300） | 發球引拍後停多久才往上推 |
| 不想自動點計時條 | `auto_timing_bar` | 改成 `false`（預設 `true`：NET CORD 等計時條，白棒進到橘色區就自動點） |
| 計時條點太早 / 太晚 | `bar_lead_ms`（預設 30） | 太晚調大、太早調小，每次 ±10 |
| 拉球太早 / 太晚 | `match_scale_drive`（預設 1.45） | 只影響 drive，不影響 block：太早調小、太晚調大，每次 ±0.05 |
| 點擊沒被遊戲吃到 | `tap_hold_ms`（預設 30） | 調大到 50～80 |
| 半藍半紅的球想用反手 | `middle_side` | 改成 `"backhand"` |
| 揮拍力道不夠 / 沒反應 | `swing_ratio`、`swing_ms` | 距離調大、時間調小 |

## 六、常見問題

| 問題 | 原因與處理 |
|---|---|
| 按 F6 沒反應 | 筆電要按 Fn + F6，或改用終端機按 Enter 倒數 |
| 偵測不到光圈 | 視窗移動過 → 按 F9 重新校準。近鏡頭的 AI 模式光圈較淡又貼著球桌，程式會自動改用寬鬆顏色（`blue_hsv_alt`、`hsv_tol_alt`）再找一次 |
| 換到另一個螢幕就失效 | 按 F9 重新校準，截圖時游標放在遊戲那個螢幕 |
| 處理速度 FPS 很低 | 遊戲暫停時本來就低；比賽中仍低於 60 就拿掉 `--debug` |
| F7 測試球員不揮拍 | 遊戲可能只吃觸控 → 開 Chrome 手機模擬（F12 → Ctrl+Shift+M）後重新校準 |
| 出現紅字錯誤 | 把終端機全部內容複製下來求助 |
