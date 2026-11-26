# 台股日報（全程用手機設定）

每天收盤後自動跑，手機看網頁、Telegram 收摘要。免費，不用電腦。

**台股日報**（每天 17:00）：加權指數、三大法人買賣金額、自選股（含外資/投信張數）、
類股漲跌、爆量長紅、創 20 日新高、外資連買、投信連買。

**美股晨報**（台灣週二到週六 06:30）：S&P 500、那斯達克、道瓊、費城半導體、VIX、
台積電 ADR 換算台幣與溢價、美元／台幣、美股自選。開盤前看完就知道今天台股的風向。

**消息面燈號**（自選股每一支）：把四種公開資料整理成燈號，紅＝偏多、綠＝偏空、灰＝中性。
- 趨勢：收盤價在 20 日均線之上或之下
- 籌碼：外資＋投信近 3 日合計買超或賣超
- 營收：最新月營收的年增率（公開資訊觀測站）
- 消息：當天重大訊息＋近兩天新聞標題，用關鍵字粗分正負面
三個以上偏多、沒有偏空 → 「偏多」；兩個以上偏空 → 「偏空」；其他是「中性」。
另外會列出「全市場警示重訊」（停工、訴訟、虧損、處分等字眼）。

燈號只是把資料整理在一起，**不是買賣訊號**。關鍵字判斷很粗，標題一定要點開自己看。

**模擬交易**（衛星部位的規則，用假錢跑）：
- 0050 站上 20 日均線才開新倉
- 進場：站上 20 日均線、外資＋投信 3 日買超、營收年增為正、沒有負面重訊、成交額 ≥ 1 億，
  符合的股票依「法人買超 ÷ 20 日均量」排序，挑最強的補滿空位
- 每檔風險 2%、停損 -8%（每檔約投入資產的 25%），最多 4 檔
- 收盤跌破 -8% → 隔天開盤停損；跌破 20 日均線 → 全賣；漲 20% → 先賣一半
- 連虧 3 筆暫停 20 個交易日
- 收盤後產生訊號，隔天用開盤價成交，扣手續費 0.1425%（最低 20 元）和證交稅 0.3%
- 報告會顯示總資產、報酬率和同期 0050 對照、持股、明天要做的動作、已平倉紀錄
- 數字都在 `config.json` 的 `paper` 裡；`start_cash` 是模擬本金

跑滿 3 個月再看：報酬有沒有贏 0050？贏了才考慮用真錢照做。

兩份報告在同一個網站，頁面右上角可以互相切換。

## 一、建立 GitHub 專案（Safari 開 github.com）
1. 右上角「+」→ New repository，名稱例如 `tw-daily`，選 **Public**（免費 Pages 需要）→ Create
2. 點「uploading an existing file」，把壓縮檔解開後的**所有檔案**上傳
   - `.github/workflows/` 是隱藏資料夾，iPhone 上傳可能漏掉裡面的 `daily.yml` 和 `us.yml`。漏掉的話：
     Add file → Create new file，檔名輸入 `.github/workflows/daily.yml`，把內容貼上；`us.yml` 同樣做一次
3. Commit changes

## 二、開權限
- Settings → Actions → General → 最下面 Workflow permissions 選 **Read and write** → Save

## 三、開網頁
- Settings → Pages → Source 選 Deploy from a branch → Branch 選 `main`、資料夾 `/docs` → Save
- 網址會是 `https://你的帳號.github.io/tw-daily/`，Safari 打開後「分享 → 加入主畫面」

## 四、Telegram（可選）
1. Telegram 找 @BotFather → `/newbot` → 拿到 token
2. 傳一句話給你的 bot，Safari 打開 `https://api.telegram.org/bot<token>/getUpdates`，找 `"chat":{"id":` 後面的數字
3. Settings → Secrets and variables → Actions → New repository secret，新增 `TG_TOKEN` 和 `TG_CHAT`

## 五、改設定（config.json，點檔案 → 鉛筆圖示編輯）
- `watchlist`：自選股代號
- `page_url`：填你的網頁網址，Telegram 摘要就會附連結
- `us.watchlist`：美股自選代號（例如 NVDA、AAPL）
- `us.indices`：要看的美股指數
- `screens`：篩選條件，例如 `vol_mult` 爆量倍數、`min_up_pct` 漲幅、`min_streak_lots` 連買張數

## 六、第一次執行
Actions → 台股每日報告 → Run workflow；再到 美股晨報 → Run workflow。
建議先跑台股，美股頁的 ADR 溢價需要台積電的台股收盤價。綠勾 = 成功；紅叉點進去看錯誤，截圖給 Claude。

## 須知
- 每週一到五 17:00 執行，22:00 補跑一次；休市日自動跳過
- 爆量和新高需要累積 20 個交易日，前四週會顯示「累積中」；法人連買第一天就能用
- 美股資料來自 Yahoo Finance 的非官方介面，偶爾會擋 GitHub 的伺服器；失敗時隔天通常就恢復
- 台積電 ADR 溢價 = ADR 價格 × 匯率 ÷ 5 ÷ 2330 收盤，平常就有正溢價，要看的是變化
- 新聞標題來自 Google 新聞 RSS，只放標題和連結
- 只含上市股票，上櫃（櫃買）目前沒有
- 漲是紅色、跌是綠色（台股慣例）
- GitHub 的排程偶爾會延遲 10–30 分鐘
- 篩選結果只是條件比對，不是買賣建議
