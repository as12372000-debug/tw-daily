"""離線測試：用假資料模擬 22 個交易日，不連網。用法：python test_offline.py"""
import datetime as dt, json, os, pathlib, random, shutil, sys, tempfile

tmp = pathlib.Path(tempfile.mkdtemp())
shutil.copy(pathlib.Path(__file__).parent / "config.json", tmp / "config.json")
os.environ["DAILY_ROOT"] = str(tmp)
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import daily  # noqa: E402

daily.time.sleep = lambda s: None
random.seed(1)
STOCKS = {"2330": ("台積電", 1000), "2317": ("鴻海", 200), "2454": ("聯發科", 1300),
          "2308": ("台達電", 900), "0050": ("元大台灣50", 190), "3008": ("大立光", 2500), "1101": ("台泥", 33)}
state = {"day": None, "i": 0}


def roc(d):
    return f"{d.year - 1911}{d:%m%d}"


def fake_get(url, tries=3):
    d, i = state["day"], state["i"]
    if "STOCK_DAY_ALL" in url:
        out = []
        for code, (name, base) in STOCKS.items():
            close = base * (1 + 0.003 * i)
            vol = 10_000_000
            chg = base * 0.003
            if code == "3008" and i == 21:          # 最後一天爆量長紅
                close, vol, chg = base * 1.15, 40_000_000, base * 1.15 - base * (1 + 0.003 * 20)
            out.append({"Date": roc(d), "Code": code, "Name": name, "TradeVolume": f"{vol:,}",
                        "TradeValue": f"{int(vol * close):,}", "ClosingPrice": f"{close:,.2f}", "OpeningPrice": f"{close:,.2f}",
                        "Change": f"{chg:.2f}"})
        return out
    if "MI_INDEX" in url:
        return [{"指數": "發行量加權股價指數", "收盤指數": "23,456.78", "漲跌(+/-)": "<p style='color:red'>+</p>",
                 "漲跌點數": "123.45", "漲跌百分比(%)": "0.53"},
                {"指數": "半導體類指數", "收盤指數": "900.1", "漲跌(+/-)": "+", "漲跌點數": "9", "漲跌百分比(%)": "1.01"},
                {"指數": "金融保險類指數", "收盤指數": "2100", "漲跌(+/-)": "-", "漲跌點數": "10", "漲跌百分比(%)": "0.48"},
                {"指數": "半導體類報酬指數", "收盤指數": "1", "漲跌(+/-)": "+", "漲跌點數": "1", "漲跌百分比(%)": "1"}]
    if "T86" in url:
        q = dt.datetime.strptime(url.split("date=")[1][:8], "%Y%m%d").date()
        if q.weekday() >= 5 or q > state["day"]:
            return {"stat": "很抱歉，沒有符合條件的資料!"}
        f = ["證券代號", "證券名稱", "外陸資買賣超股數(不含外資自營商)", "投信買賣超股數", "自營商買賣超股數", "三大法人買賣超股數"]
        rows = [[c, n, "500,000" if c == "2330" else "-1,000", "200,000", "0", "700,000"] for c, (n, _) in STOCKS.items()]
        return {"stat": "OK", "fields": f, "data": rows}
    if "t187ap04_L" in url:
        return [{"出表日期": roc(d), "發言日期": roc(d), "發言時間": "173000", "公司代號": "2330",
                 "公司名稱": "台積電", "主旨 ": "本公司董事會決議配發現金股利", "說明": "..."},
                {"出表日期": roc(d), "發言日期": roc(d), "發言時間": "180000", "公司代號": "1101",
                 "公司名稱": "台泥", "主旨 ": "本公司工廠火災停工說明", "說明": "..."}]
    if "t187ap05_L" in url:
        return [{"資料年月": "11510", "公司代號": "2330", "公司名稱": "台積電",
                 "營業收入-上月比較增減(%)": "5.1", "營業收入-去年同月增減(%)": "32.5"},
                {"資料年月": "11510", "公司代號": "3008", "公司名稱": "大立光",
                 "營業收入-上月比較增減(%)": "-2", "營業收入-去年同月增減(%)": "-8.4"}]
    if "finance.yahoo.com" in url:
        sym = url.split("/chart/")[1].split("?")[0]
        base = {"%5EGSPC": 6000, "TSM": 300, "TWD%3DX": 32.0}.get(sym, 100)
        ts = [1764000000 + 86400 * k for k in range(20)]
        closes = [base * (1 + 0.001 * k) for k in range(19)] + [base * 1.02 * 1.018]
        return {"chart": {"result": [{"meta": {"gmtoffset": -18000}, "timestamp": ts,
                                      "indicators": {"quote": [{"close": closes}]}}]}}
    if "BFI82U" in url:
        return {"stat": "OK", "fields": ["單位名稱", "買進金額", "賣出金額", "買賣差額"],
                "data": [["自營商(自行買賣)", "1", "1", "100,000,000"], ["投信", "1", "1", "-320,000,000"],
                         ["外資及陸資(不含外資自營商)", "1", "1", "4,560,000,000"], ["合計", "1", "1", "4,340,000,000"]]}
    return None


daily.get = fake_get
daily.fetch_news = lambda q, k=3: [{"title": f"{q} 營收創新高", "link": "https://example.com/a", "src": "測試新聞"}] if q == "台積電" else []
d, i = dt.date(2026, 10, 26), 0
while i < 22:
    if d.weekday() < 5:
        state.update(day=d, i=i)
        daily.main()
        i += 1
    d += dt.timedelta(days=1)

page = (tmp / "docs" / "index.html").read_text(encoding="utf-8")


def check(c, m):
    print(("✓ " if c else "✗ ") + m)
    assert c, m


print("\n----")
check("23,456.78" in page and "+123.45" in page, "大盤指數與漲跌正確")
check("半導體" in page and "半導體類報酬" not in page, "類股只列類指數")
check("-0.48%" in page, "跌的類股帶負號")
check("3008" in page.split("爆量長紅")[1].split("</section>")[0], "大立光出現在爆量長紅")
check("2330" in page.split("外資連買")[1].split("</section>")[0], "台積電出現在外資連買")
check("+45.6 億" in page, "外資買超 45.6 億")
daily.main()
check(len(list((tmp / "data" / "prices").glob("*.json"))) == 22, "同一天重跑會跳過")
card = page.split("<b>2330</b>")[1].split('<div class="stock">')[0]
check("偏多" in card and "站上 20 日均線" in card and "年增 +32.5%" in card, "台積電四燈：趨勢、籌碼、營收、消息")
check("現金股利" in card and "營收創新高" in card, "重大訊息和新聞標題出現在卡片裡")
check("火災停工" in page.split("全市場警示重訊")[1], "負面重訊列入全市場警示")
check("營收年增 -8%" in page.split("爆量長紅")[1].split("</section>")[0], "篩選結果附上營收年增率")
st = json.loads((tmp / "data" / "paper.json").read_text(encoding="utf-8"))
check("2330" in st["positions"], f"模擬：四燈全亮的台積電被買進 {st['positions'].get('2330', {}).get('shares')} 股")
check("模擬交易" in page and "明天開盤要做" in page, "報告有模擬交易區塊")
check("0050" not in st["positions"] and "3008" not in st["positions"], "ETF 和營收衰退的股票不會被買")
# 手動做一次跳空大跌：收盤 -12% → 隔天開盤停損
hist = [json.loads(f.read_text()) for f in sorted((tmp / "data" / "prices").glob("*.json"))]
last = {c: dict(name=STOCKS[c][0], close=v[0], chg=0, open=v[0], vol=v[1], value=v[2]) for c, v in hist[-1].items()}
crash = {c: dict(x) for c, x in last.items()}
crash["2330"]["close"] = st["positions"]["2330"]["entry"] * 0.88
r1 = daily.paper_step("2026-11-25", crash, hist, [], {}, [], json.loads((tmp / "config.json").read_text()))
check(any(o["code"] == "2330" and "停損" in o["reason"] for o in r1["st"]["pending"]), "收盤跌破 -8% → 排定隔天停損")
nxt = {c: dict(x) for c, x in crash.items()}
nxt["2330"]["open"] = crash["2330"]["close"] * 0.97
r2 = daily.paper_step("2026-11-26", nxt, hist + [{c: [x["close"], 1, 1] for c, x in crash.items()}], [], {}, [],
                      json.loads((tmp / "config.json").read_text()))
t = r2["st"]["trades"][-1]
check(t["reason"].startswith("停損") and t["pnl"] < 0 and "2330" not in r2["st"]["positions"],
      f"隔天開盤停損成交，虧損 {t['pnl']:,} 元（含跳空、手續費、證交稅）")
check('href="us.html"' in page, "台股頁有連到美股晨報")
daily.main_us()
us = (tmp / "docs" / "us.html").read_text(encoding="utf-8")
check("費城半導體" in us and "台積電 ADR" in us, "美股頁有指數和台積電 ADR")
tsm = 300 * 1.02 * 1.018; fx = 32 * 1.02 * 1.018; tw = 1000 * (1 + 0.003 * 21)
want = f"{tsm * fx / 5 / tw * 100 - 100:+.1f}%"
check(want in us, f"ADR 溢價計算正確（{want}）")
daily.main_us()
shutil.copy(tmp / "docs" / "index.html", "/tmp/preview.html")
shutil.copy(tmp / "docs" / "us.html", "/tmp/preview_us.html")
print("\n全部通過。")
