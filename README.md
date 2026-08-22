# 雙字幕 Chromecast 投放骨架

三個部分:
1. `local_server.py` — 在你電腦上跑,把本機影片 + 兩個字幕檔透過 HTTP(支援 Range)分享給 Chromecast 讀取。
2. `sender.html` — 由 local_server 提供的網頁,負責選檔案並呼叫 Cast SDK 開始投放。
3. `receiver/index.html` — 自訂 Cast Receiver,同時渲染兩條字幕(上/下各一條)。
4. `extension/` — 最小 Chrome 擴充套件,popup 按一下就開啟 sender.html。

## 一次性設定

### 1. 註冊 Custom Receiver App ID
Chromecast 只認得向 Google 註冊過的 Receiver。步驟:
1. 先把 `receiver/index.html` 部署到一個公開的 HTTPS 網址(最簡單:建一個 GitHub repo,開啟 GitHub Pages,把這個檔案放進去,例如變成 `https://<你的帳號>.github.io/dual-sub-cast-receiver/`)。
2. 到 [Google Cast SDK Developer Console](https://cast.google.com/publish) 註冊帳號(需要一次性 $5 費用),新增一個 Custom Receiver,網址填上一步的 GitHub Pages 連結。
3. 把電視所在的 Google 帳號(Chromecast 綁定的帳號)加進「Test devices」清單,否則要等審核才能用。
4. 拿到系統給你的 Receiver App ID(一串英數字)。

### 2. 填入 App ID
編輯 `sender.html`,把:
```js
const RECEIVER_APP_ID = "YOUR_RECEIVER_APP_ID";
```
換成你剛拿到的 ID。

### 3. 準備媒體檔案
把要投放的影片檔跟兩個字幕檔(.vtt 或 .srt)放進 `media/` 資料夾(第一次執行 `local_server.py` 會自動建立)。
如果你的字幕還沒調好時間軸,先用 Subtitle Edit / Aegisub 各自校準好、轉出 .vtt,再放進來。

### 4. 啟動本機伺服器
```bash
python local_server.py
```
它會印出兩個網址,例如:
```
Local:  http://127.0.0.1:8787/
LAN:    http://192.168.1.23:8787/
```
**記住 LAN 那個網址**——Chromecast 裝置要用這個才連得到你的電腦。

### 5. 載入 Chrome 擴充套件
1. 打開 `chrome://extensions`,開啟右上角「開發人員模式」。
2. 「載入未封裝項目」,選擇 `extension/` 資料夾。
3. 點擴充套件圖示,貼上上一步的 LAN 網址,按「開啟投放頁面」。

### 6. 投放
在開啟的 sender.html 頁面裡:
1. 選影片檔、字幕 1(上方)、字幕 2(下方)。
2. 點右上角 Cast 圖示連上 Chromecast,或直接按「投放到 Chromecast」。
3. 電視畫面應該同時看到兩條字幕,一條在上、一條在下。

## 已知限制 / 之後可以再做的事
- 字幕比對是「整條字幕檔一次抓下來」,還沒做離線 offset 微調 UI——如果你的 .vtt 時間軸不準,先用 Aegisub/Subtitle Edit 調好再放進 `media/`。
- 目前用 setInterval(250ms) 輪詢目前播放時間來換字幕,精度足夠一般用途,但不是逐格同步。
- sender.html 目前用簡單的下拉選單讀 `media/` 資料夾內容,沒有做子資料夾瀏覽。
- 未做認證,`local_server.py` 對整個區網開放,建議只在信任的家用網路使用。
