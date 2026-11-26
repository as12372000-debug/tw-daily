"""台股每日報告：抓證交所公開資料 → 產生手機網頁 + Telegram 摘要。
GitHub Actions 每天收盤後自動執行；手動測試：python daily.py
"""
import datetime as dt
import html
import json
import os
import pathlib
import re
import sys
import time

import requests

ROOT = pathlib.Path(os.getenv("DAILY_ROOT", pathlib.Path(__file__).parent))
DATA, DOCS = ROOT / "data", ROOT / "docs"
TW = dt.timezone(dt.timedelta(hours=8))
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "Mozilla/5.0 (tw-daily-report)"


# ---------------- 工具 ----------------
def get(url, tries=3):
    for i in range(tries):
        try:
            r = SESSION.get(url, timeout=20)
            if r.status_code == 200:
                return r.json()
        except (requests.RequestException, ValueError):
            pass
        time.sleep(3 * (i + 1))
    return None


def n(x):
    if x is None:
        return None
    s = re.sub(r"<[^>]+>", "", str(x)).replace(",", "").strip()
    if s in ("", "--", "---", "X", "除權息", "除息", "除權"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def roc_to_iso(s):
    d = re.sub(r"\D", "", str(s))
    if len(d) < 7:
        return None
    return f"{int(d[:-4]) + 1911:04d}-{d[-4:-2]}-{d[-2:]}"


def load_json(p, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def pct_of(close, chg):
    prev = (close or 0) - (chg or 0)
    return chg / prev * 100 if prev > 0 and chg is not None else 0.0


# ---------------- 資料 ----------------
def fetch_prices():
    j = get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
    if not isinstance(j, list) or not j:
        return None, None
    out, date = {}, None
    for r in j:
        code = str(r.get("Code", "")).strip()
        close = n(r.get("ClosingPrice"))
        if not code or close is None:
            continue
        out[code] = dict(name=str(r.get("Name", "")).strip(), close=close, chg=n(r.get("Change")) or 0.0,
                         open=n(r.get("OpeningPrice")),
                         vol=(n(r.get("TradeVolume")) or 0) / 1000,      # 張
                         value=(n(r.get("TradeValue")) or 0) / 1e8)      # 億
        if not date and r.get("Date"):
            date = roc_to_iso(r["Date"])
    return out, date


def fetch_indices():
    j = get("https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX")
    rows = []
    for r in j if isinstance(j, list) else []:
        sign = re.sub(r"<[^>]+>", "", str(r.get("漲跌(+/-)", ""))).strip()

        def signed(v):
            if v is None:
                return None
            return -abs(v) if sign == "-" else (abs(v) if sign == "+" else v)
        rows.append(dict(name=str(r.get("指數", "")).strip(), close=n(r.get("收盤指數")),
                         pts=signed(n(r.get("漲跌點數"))), pct=signed(n(r.get("漲跌百分比(%)")))))
    taiex = next((x for x in rows if x["name"].startswith("發行量加權股價指數")), None)
    sectors = [x for x in rows if x["name"].endswith("類指數") and "報酬" not in x["name"]
               and x["pct"] is not None]
    return taiex, sorted(sectors, key=lambda x: -x["pct"])


def table(j):
    if not isinstance(j, dict) or j.get("stat") != "OK":
        return None
    if j.get("fields") and j.get("data"):
        return j["fields"], j["data"]
    for t in j.get("tables") or []:
        if t.get("fields") and t.get("data"):
            return t["fields"], t["data"]
    return None


def col(fields, exact, contains=None):
    if exact in fields:
        return fields.index(exact)
    for i, f in enumerate(fields):
        if (contains or exact) in f:
            return i
    return None


def fetch_t86(day):
    """個股三大法人買賣超（張）。回傳 {code: [外資, 投信, 自營商, 合計]}，休市回傳 None。"""
    cache = DATA / "inst" / f"{day.isoformat()}.json"
    if cache.exists():
        return load_json(cache)
    j = get(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={day:%Y%m%d}&selectType=ALLBUT0999&response=json")
    time.sleep(3)  # 證交所有頻率限制
    t = table(j)
    if not t:
        return None
    fields, data = t
    ic = col(fields, "證券代號")
    idx = [col(fields, "外陸資買賣超股數(不含外資自營商)", "外陸資買賣超"),
           col(fields, "投信買賣超股數"), col(fields, "自營商買賣超股數"), col(fields, "三大法人買賣超股數")]
    if ic is None or idx[0] is None or idx[1] is None:
        return None
    out = {str(row[ic]).strip(): [round((n(row[i]) or 0) / 1000) if i is not None else 0 for i in idx]
           for row in data}
    save_json(cache, out)
    return out


def fetch_bfi(day):
    """三大法人買賣金額（億）。"""
    j = get(f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&dayDate={day:%Y%m%d}&response=json")
    t = table(j)
    if not t:
        return None
    res = {"外資": 0.0, "投信": 0.0, "自營商": 0.0, "合計": None}
    for row in t[1]:
        name, diff = str(row[0]), (n(row[-1]) or 0) / 1e8
        if name.startswith("外資及陸資"):
            res["外資"] += diff
        elif name.startswith("投信"):
            res["投信"] += diff
        elif name.startswith("自營商"):
            res["自營商"] += diff
        elif name.startswith("合計"):
            res["合計"] = diff
    return res


def inst_history(day, need):
    """往回找 need 個有資料的交易日（含今天），新的在前。"""
    days, d = [], day
    for _ in range(need + 10):
        t = fetch_t86(d)
        if t:
            days.append((d, t))
            if len(days) >= need:
                break
        d -= dt.timedelta(days=1)
    return days


# ---------------- 分析 ----------------
def is_stock(code):
    return len(code) == 4 and code.isdigit() and not code.startswith("00")


def run_screens(today, hist, inst, cfg):
    sc = cfg["screens"]
    k = sc["avg_days"]
    res = {"爆量長紅": [], "創 20 日新高": [], "外資連買": [], "投信連買": [], "_notes": []}
    if len(hist) >= k:
        window = hist[-k:]
        for code, p in today.items():
            if not is_stock(code) or p["value"] < sc["min_value_e8"]:
                continue
            prev = [d[code] for d in window if code in d]
            if len(prev) < k * 0.8:
                continue
            pct = pct_of(p["close"], p["chg"])
            avgv = sum(x[1] for x in prev) / len(prev)
            if avgv > 0 and p["vol"] >= sc["vol_mult"] * avgv and pct >= sc["min_up_pct"]:
                res["爆量長紅"].append((code, p, f"量 {p['vol'] / avgv:.1f} 倍"))
            if p["close"] > max(x[0] for x in prev):
                res["創 20 日新高"].append((code, p, f"成交 {p['value']:.1f} 億"))
        res["爆量長紅"].sort(key=lambda x: -float(x[2].split()[1].rstrip("倍")))
        res["創 20 日新高"].sort(key=lambda x: -x[1]["value"])
    else:
        res["_notes"].append(f"爆量和新高需要 {k} 天歷史，目前累積 {len(hist)} 天")
    s = sc["streak_days"]
    if len(inst) >= s:
        last = [t for _, t in inst[:s]]
        for name, i in (("外資連買", 0), ("投信連買", 1)):
            for code, p in today.items():
                if not is_stock(code):
                    continue
                vals = [t.get(code, [0, 0, 0, 0])[i] for t in last]
                if all(v > 0 for v in vals) and sum(vals) >= sc["min_streak_lots"]:
                    res[name].append((code, p, f"{s} 天 +{sum(vals):,} 張"))
            res[name].sort(key=lambda x: -int(re.sub(r"\D", "", x[2].split("+")[1])))
    else:
        res["_notes"].append(f"法人連買需要 {s} 個交易日資料，目前 {len(inst)} 天")
    for key in res:
        if not key.startswith("_"):
            res[key] = res[key][:sc["max_rows"]]
    return res


# ---------------- 消息面（全部是公開資料） ----------------
NEG = ["虧損", "停工", "減產", "訴訟", "違約", "下修", "處分", "搜索", "資安", "減資", "跌停", "衰退", "下滑",
       "裁員", "火災", "爆炸", "停止買賣", "暫停交易", "退票", "重整", "破產", "掏空", "調降", "警示", "注意股"]
POS = ["上修", "創新高", "獲利", "成長", "庫藏股", "股利", "配息", "得標", "簽約", "擴產", "調升", "漲停",
       "轉盈", "大單", "新高"]


def tone(text):
    if any(k in text for k in NEG):
        return "-"
    if any(k in text for k in POS):
        return "+"
    return ""


def _norm(r):
    return {re.sub(r"\s", "", str(k)): v for k, v in r.items()} if isinstance(r, dict) else {}


def _pick(r, *names):
    for nm in names:
        for k, v in r.items():
            if nm in k:
                return v
    return None


def fetch_announcements():
    """上市公司當日重大訊息（公開資訊觀測站）。"""
    j = get("https://openapi.twse.com.tw/v1/opendata/t187ap04_L")
    out = []
    for r in map(_norm, j if isinstance(j, list) else []):
        code = str(_pick(r, "公司代號") or "").strip()
        title = re.sub(r"\s+", " ", str(_pick(r, "主旨") or "")).strip()
        if code and title:
            out.append(dict(code=code, name=str(_pick(r, "公司名稱") or "").strip(), title=title,
                            when=f"{roc_to_iso(_pick(r, '發言日期')) or ''} {_pick(r, '發言時間') or ''}".strip()))
    return out


def fetch_revenue():
    """上市公司最新月營收：{code: {ym, yoy, mom}}。"""
    j = get("https://openapi.twse.com.tw/v1/opendata/t187ap05_L")
    out = {}
    for r in map(_norm, j if isinstance(j, list) else []):
        code = str(_pick(r, "公司代號") or "").strip()
        ym = re.sub(r"\D", "", str(_pick(r, "資料年月") or ""))
        if code:
            out[code] = dict(ym=f"{int(ym[:-2]) + 1911}-{ym[-2:]}" if len(ym) >= 4 else "",
                             yoy=n(_pick(r, "去年同月增減")), mom=n(_pick(r, "上月比較增減")))
    return out


def fetch_news(query, k=3):
    """Google 新聞 RSS，近兩天的標題和連結。"""
    import xml.etree.ElementTree as ET
    from urllib.parse import quote
    url = f"https://news.google.com/rss/search?q={quote(query + ' when:2d')}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    try:
        root = ET.fromstring(SESSION.get(url, timeout=20).content)
    except Exception:
        return []
    out = []
    for it in root.iter("item"):
        title, src = it.findtext("title") or "", it.findtext("source") or ""
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        out.append(dict(title=title, link=it.findtext("link") or "", src=src))
        if len(out) >= k:
            break
    return out


def signals(code, p, hist, inst, rev, anns, news):
    """四個面向的燈號：True=偏多、False=偏空、None=中性或沒資料。"""
    lights = []
    closes = [d[code][0] for d in hist[-19:] if code in d] + [p["close"]]
    if len(closes) >= 20:
        ma = sum(closes[-20:]) / 20
        lights.append(("趨勢", p["close"] > ma, f"{'站上' if p['close'] > ma else '跌破'} 20 日均線 {ma:,.1f}"))
    else:
        lights.append(("趨勢", None, f"資料累積中 {len(closes)}/20 天"))
    if inst:
        days = inst[:3]
        s = sum((t.get(code) or [0, 0])[0] + (t.get(code) or [0, 0])[1] for _, t in days)
        lights.append(("籌碼", (s > 0) if s else None, f"外資＋投信 {len(days)} 日 {s:+,} 張"))
    else:
        lights.append(("籌碼", None, "沒有法人資料"))
    r = rev.get(code)
    if r and r["yoy"] is not None:
        lights.append(("營收", r["yoy"] > 0, f"{r['ym']} 年增 {r['yoy']:+.1f}%"))
    else:
        lights.append(("營收", None, "沒有營收資料"))
    texts = [a["title"] for a in anns if a["code"] == code] + [x["title"] for x in news]
    tones = [tone(t) for t in texts]
    if not texts:
        lights.append(("消息", None, "近兩天沒有消息"))
    elif "-" in tones:
        lights.append(("消息", False, f"{len(texts)} 則，{tones.count('-')} 則有負面字眼"))
    elif "+" in tones:
        lights.append(("消息", True, f"{len(texts)} 則，{tones.count('+')} 則有正面字眼"))
    else:
        lights.append(("消息", None, f"{len(texts)} 則，看不出方向"))
    bull = sum(1 for _, v, _ in lights if v is True)
    bear = sum(1 for _, v, _ in lights if v is False)
    label = "偏多" if bull >= 3 and bear == 0 else ("偏空" if bear >= 2 else "中性")
    return dict(label=label, bull=bull, bear=bear, lights=lights)


# ---------------- 模擬交易（規則版） ----------------
def ma(code, today, hist, k=20):
    closes = [d[code][0] for d in hist[-(k - 1):] if code in d]
    if code in today:
        closes.append(today[code]["close"])
    return sum(closes) / k if len(closes) >= k else None


def avg_vol(code, hist, k=20):
    v = [d[code][1] for d in hist[-k:] if code in d]
    return sum(v) / len(v) if v else None


def paper_step(day, today, hist, inst, rev, anns, cfg):
    """每個交易日跑一次：先用今天開盤價成交昨天的訊號，再用今天收盤價產生明天的訊號。"""
    pc = cfg["paper"]
    path = DATA / "paper.json"
    st = load_json(path) or dict(cash=pc["start_cash"], positions={}, pending=[], trades=[], history=[],
                                 loss_streak=0, pause_left=0, bench0=None, start=day)
    fee = lambda amt: max(pc["fee_min"], amt * pc["fee_pct"] / 100)  # noqa: E731
    done, notes = [], []

    # 1) 開盤成交
    keep = []
    for o in st["pending"]:
        p = today.get(o["code"])
        px = p.get("open") if p else None
        if not px:
            if o["side"] == "sell":
                keep.append(o)
            continue
        if o["side"] == "buy":
            sh = o["shares"]
            while sh > 0 and sh * px + fee(sh * px) > st["cash"]:
                sh = int(sh * 0.9)
            if sh <= 0:
                continue
            cost = sh * px + fee(sh * px)
            st["cash"] -= cost
            st["positions"][o["code"]] = dict(name=p["name"], shares=sh, entry=px, date=day, half=False,
                                              cost=cost, realized=0.0, why=o["reason"])
            done.append(f"買進 {o['code']} {p['name']} {sh:,} 股 @ {px:,.2f}")
            continue
        pos = st["positions"].get(o["code"])
        if not pos:
            continue
        qty = pos["shares"] if o["frac"] >= 1 else pos["shares"] // 2
        if qty <= 0:
            continue
        amt = qty * px
        net = amt - fee(amt) - amt * pc["tax_pct"] / 100
        part_cost = pos["cost"] * qty / pos["shares"]
        st["cash"] += net
        pos["realized"] += net - part_cost
        pos["shares"] -= qty
        pos["cost"] -= part_cost
        if o["frac"] < 1:
            pos["half"] = True
        done.append(f"賣出 {o['code']} {pos['name']} {qty:,} 股 @ {px:,.2f}（{o['reason']}）")
        if pos["shares"] <= 0:
            st["trades"].append(dict(code=o["code"], name=pos["name"], entry=pos["entry"], exit=px,
                                     d_in=pos["date"], d_out=day, pnl=round(pos["realized"]), reason=o["reason"]))
            del st["positions"][o["code"]]
            st["loss_streak"] = st["loss_streak"] + 1 if pos["realized"] < 0 else 0
            if st["loss_streak"] >= pc["pause_after_losses"]:
                st["pause_left"], st["loss_streak"] = pc["pause_days"], 0
                done.append(f"連續虧損 {pc['pause_after_losses']} 筆，暫停新倉 {pc['pause_days']} 個交易日")
    st["pending"] = keep

    # 2) 收盤後檢查出場
    new = []
    for code, pos in st["positions"].items():
        p = today.get(code)
        if not p or any(o["code"] == code for o in st["pending"]):
            continue
        c, m = p["close"], ma(code, today, hist)
        if c <= pos["entry"] * (1 - pc["stop_pct"] / 100):
            new.append(dict(code=code, side="sell", frac=1, reason=f"停損 -{pc['stop_pct']}%"))
        elif m and c < m:
            new.append(dict(code=code, side="sell", frac=1, reason="跌破 20 日均線"))
        elif not pos["half"] and c >= pos["entry"] * (1 + pc["tp_pct"] / 100):
            new.append(dict(code=code, side="sell", frac=0.5, reason=f"漲 {pc['tp_pct']}% 先賣一半"))

    # 3) 收盤後找進場
    value = lambda: st["cash"] + sum(  # noqa: E731
        ps["shares"] * (today.get(c, {}).get("close") or ps["entry"]) for c, ps in st["positions"].items())
    m50, c50 = ma("0050", today, hist), (today.get("0050") or {}).get("close")
    slots = pc["max_positions"] - len(st["positions"])
    if st["pause_left"] > 0:
        st["pause_left"] -= 1
        notes.append(f"連虧暫停中，還剩 {st['pause_left'] + 1} 個交易日")
    elif m50 is None:
        notes.append(f"資料累積中，滿 20 個交易日才開始（目前 {len(hist) + 1} 天）")
    elif c50 <= m50:
        notes.append(f"0050 收在 20 日均線 {m50:,.2f} 之下，大盤偏弱，不開新倉")
    elif slots <= 0:
        notes.append("持股已滿 4 檔")
    elif len(inst) < 3:
        notes.append("法人資料不足 3 天，暫不開新倉")
    else:
        neg = {a["code"] for a in anns if tone(a["title"]) == "-"}
        cands = []
        for code, p in today.items():
            if not is_stock(code) or code in st["positions"] or code in neg or p["value"] < pc["min_value_e8"]:
                continue
            m = ma(code, today, hist)
            r = rev.get(code)
            if not m or p["close"] <= m or not r or r["yoy"] is None or r["yoy"] <= 0:
                continue
            buy = sum((t.get(code) or [0, 0])[0] + (t.get(code) or [0, 0])[1] for _, t in inst[:3])
            av = avg_vol(code, hist)
            if buy > 0 and av:
                cands.append((buy / av, code, p, buy, r["yoy"]))
        cands.sort(reverse=True)
        per = value() * pc["risk_pct"] / pc["stop_pct"]
        for _, code, p, buy, yoy in cands[:slots]:
            sh = int(per / p["close"])
            if sh > 0:
                new.append(dict(code=code, side="buy", shares=sh, name=p["name"],
                                reason=f"四燈全亮：法人 3 日 +{buy:,} 張、營收年增 {yoy:+.0f}%"))
        if not cands:
            notes.append("今天沒有股票同時符合所有條件")
    st["pending"] += new

    if st["bench0"] is None and c50:
        st["bench0"] = c50
    eq = value()
    st["history"] = (st["history"] + [[day, round(eq)]])[-400:]
    save_json(path, st)
    wins = [t for t in st["trades"] if t["pnl"] > 0]
    return dict(st=st, equity=eq, ret=(eq / pc["start_cash"] - 1) * 100,
                bench=(c50 / st["bench0"] - 1) * 100 if c50 and st["bench0"] else None,
                done=done, notes=notes, wins=len(wins), today=today)


def paper_html(ps, cfg):
    e = html.escape
    st, pc = ps["st"], cfg["paper"]
    out = (f'<div class="grid"><div><div class="lbl">模擬總資產</div><div>NT${ps["equity"]:,.0f}</div></div>'
           f'<div><div class="lbl">報酬（同期 0050）</div><div><span class="{cls(ps["ret"])}">{sg(ps["ret"], 1, "%")}</span>'
           f'<small>（{sg(ps["bench"], 1, "%")}）</small></div></div></div>')
    for code, pos in st["positions"].items():
        c = (ps["today"].get(code) or {}).get("close") or pos["entry"]
        pct = (c / pos["entry"] - 1) * 100
        out += (f'<div class="row"><div><b>{e(code)}</b> {e(pos["name"])}<small>{pos["shares"]:,} 股・'
                f'{pos["date"]} 進場 {pos["entry"]:,.2f}{"・已賣一半" if pos["half"] else ""}</small></div>'
                f'<div class="r"><span>{c:,.2f}</span><span class="{cls(pct)}">{sg(pct, 1, "%")}</span></div></div>')
    if not st["positions"]:
        out += '<p class="muted">目前沒有持股</p>'
    todo = ""
    for o in st["pending"]:
        name = o.get("name") or st["positions"].get(o["code"], {}).get("name", "")
        qty = f" {o['shares']:,} 股" if o["side"] == "buy" else ""
        side = "買" if o["side"] == "buy" else "賣"
        todo += (f'<div class="news"><span class="tag">{side}</span>{e(o["code"])} {e(name)}{qty}'
                 f'<small>{e(o["reason"])}</small></div>')
    out += f'<h2 style="margin-top:12px">明天開盤要做</h2>{todo or "<p class=\'muted\'>沒有動作</p>"}'
    out += "".join(f'<p class="muted">{e(x)}</p>' for x in ps["notes"])
    if ps["done"]:
        out += '<h2 style="margin-top:12px">今天已成交</h2>' + "".join(f'<div class="news">{e(x)}</div>' for x in ps["done"])
    t = st["trades"]
    if t:
        out += (f'<h2 style="margin-top:12px">已平倉<small>{len(t)} 筆・勝率 {ps["wins"] / len(t):.0%}</small></h2>'
                + "".join(f'<div class="news">{e(x["code"])} {e(x["name"])}　<span class="{cls(x["pnl"])}">'
                          f'{x["pnl"]:+,} 元</span><small>{x["d_in"]} → {x["d_out"]}・{e(x["reason"])}</small></div>'
                          for x in t[-8:][::-1]))
    return (f'<section><h2>模擬交易<small>起始 NT${pc["start_cash"]:,}・{st["start"]} 開始</small></h2>{out}'
            f'<p class="muted">規則：0050 站上 20 日均線才開新倉；四燈全亮才買；每檔風險 {pc["risk_pct"]}%、停損 -{pc["stop_pct"]}%、'
            f'漲 {pc["tp_pct"]}% 賣一半、跌破 20 日均線全賣；最多 {pc["max_positions"]} 檔；連虧 {pc["pause_after_losses"]} 筆暫停。'
            f'用隔天開盤價成交，含手續費和證交稅。</p></section>')


def paper_lines(ps):
    st = ps["st"]
    lines = [f"模擬：NT${ps['equity']:,.0f}（{sg(ps['ret'], 1, '%')}｜0050 {sg(ps['bench'], 1, '%')}）"]
    for o in st["pending"]:
        name = o.get("name") or st["positions"].get(o["code"], {}).get("name", "")
        lines.append(f"明天{'買' if o['side'] == 'buy' else '賣'} {o['code']} {name}"
                     + (f" {o['shares']:,} 股" if o["side"] == "buy" else "") + f"（{o['reason']}）")
    return lines


# ---------------- 輸出 ----------------
def cls(v):
    return "up" if v and v > 0 else ("down" if v and v < 0 else "")


def sg(v, d=2, suf=""):
    return "—" if v is None else f"{v:+,.{d}f}{suf}"


def stock_row(code, p, extra=""):
    pct = pct_of(p["close"], p["chg"])
    e = html.escape
    return (f'<div class="row"><div><b>{e(code)}</b> {e(p["name"])}'
            f'{f"<small>{e(extra)}</small>" if extra else ""}</div>'
            f'<div class="r"><span>{p["close"]:,.2f}</span><span class="{cls(pct)}">{sg(pct, 2, "%")}</span></div></div>')


def build_html(day, today, taiex, sectors, bfi, inst, watch, screens, cfg, info=None):
    e = html.escape
    parts = []
    if taiex:
        parts.append(f'<section class="hero"><div class="lbl">加權指數</div><div class="big">{taiex["close"]:,.2f}</div>'
                     f'<div class="{cls(taiex["pts"])}">{sg(taiex["pts"])}（{sg(taiex["pct"], 2, "%")}）</div></section>')
    if bfi:
        cells = "".join(f'<div><div class="lbl">{k}</div><div class="{cls(v)}">{sg(v, 1)} 億</div></div>'
                        for k, v in bfi.items() if v is not None)
        parts.append(f'<section><h2>三大法人</h2><div class="grid">{cells}</div></section>')
    else:
        parts.append('<section><h2>三大法人</h2><p class="muted">尚未公布</p></section>')

    info = info or {}
    t0 = inst[0][1] if inst and inst[0][0].isoformat() == day else {}
    cards = []
    for code in watch:
        p = today.get(code)
        if not p:
            cards.append(f'<div class="row"><div><b>{e(code)}</b></div><div class="muted">今日無資料</div></div>')
            continue
        f, t = (t0.get(code) or [None, None, None, None])[:2]
        extra = f"外資 {f:+,} 張・投信 {t:+,} 張" if f is not None else ""
        sig = (info.get("sig") or {}).get(code)
        body = ""
        if sig:
            lab = {"偏多": "up", "偏空": "down"}.get(sig["label"], "")
            body += (f'<div class="sig"><b class="{lab}">{sig["label"]}</b>'
                     f'<span class="muted">（多 {sig["bull"]}・空 {sig["bear"]}）</span></div>')
            body += "".join(f'<div class="sig"><i class="dot {cls(1 if v else (-1 if v is False else 0))}"></i>'
                            f'{e(k)}：{e(txt)}</div>' for k, v, txt in sig["lights"])
        for a in [a for a in info.get("anns", []) if a["code"] == code]:
            body += f'<div class="news"><span class="tag">重訊</span>{e(a["title"])}<small>{e(a["when"])}</small></div>'
        for x in (info.get("news") or {}).get(code, []):
            body += (f'<div class="news"><a href="{e(x["link"])}">{e(x["title"])}</a>'
                     f'<small>{e(x["src"])}</small></div>')
        cards.append(f'<div class="stock">{stock_row(code, p, extra)}{body}</div>')
    parts.append(f'<section><h2>自選股</h2>{"".join(cards)}'
                 f'<p class="muted">燈號只是把四種公開資料整理在一起，不是買賣訊號。</p></section>')

    if info.get("paper"):
        parts.append(paper_html(info["paper"], cfg))

    warn = [a for a in info.get("anns", []) if tone(a["title"]) == "-" and is_stock(a["code"])][:10]
    if warn:
        rows = "".join(f'<div class="news"><b>{e(a["code"])} {e(a["name"])}</b>　{e(a["title"])}</div>' for a in warn)
        parts.append(f'<section><h2>全市場警示重訊<small>{len(warn)}</small></h2>{rows}</section>')

    if sectors:
        mx = max(abs(x["pct"]) for x in sectors) or 1
        srows = "".join(
            f'<div class="sec"><span>{e(x["name"].replace("類指數", ""))}</span>'
            f'<span class="bar"><i class="{cls(x["pct"])}" style="width:{abs(x["pct"]) / mx * 100:.0f}%"></i></span>'
            f'<span class="{cls(x["pct"])}">{sg(x["pct"], 2, "%")}</span></div>' for x in sectors)
        parts.append(f'<section><h2>類股</h2>{srows}</section>')

    for name, items in screens.items():
        if name.startswith("_"):
            continue
        rev = info.get("rev") or {}

        def rx(c, x):
            r = rev.get(c)
            return x + (f"・營收年增 {r['yoy']:+.0f}%" if r and r["yoy"] is not None else "")
        body = "".join(stock_row(c, p, rx(c, x)) for c, p, x in items) or '<p class="muted">今天沒有符合的股票</p>'
        parts.append(f'<section><h2>{name}<small>{len(items)}</small></h2>{body}</section>')
    notes = "".join(f"<p>{e(x)}</p>" for x in screens["_notes"])
    sc = cfg["screens"]
    rule = (f"爆量長紅：量 ≥ {sc['avg_days']} 日均量 {sc['vol_mult']} 倍、漲 ≥ {sc['min_up_pct']}%、成交 ≥ {sc['min_value_e8']} 億。"
            f"法人連買：連續 {sc['streak_days']} 天買超且合計 ≥ {sc['min_streak_lots']:,} 張。")
    gen = dt.datetime.now(TW).strftime("%H:%M")
    return wrap(f"台股日報 {day}", f'台股日報・{day}（{gen} 更新）<a href="us.html">美股晨報 →</a>', "".join(parts),
                f"{notes}<p>{e(rule)}</p><p>資料來源：臺灣證券交易所公開資料（僅上市）。篩選結果不是買賣建議。</p>")


CSS = """
:root{--bg:#fff;--fg:#111;--mut:#6b7280;--card:#f4f4f5;--line:#e4e4e7;--up:#d92d20;--down:#079455;--link:#2563eb;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0b0b0c;--fg:#f4f4f5;--mut:#9ca3af;--card:#18181b;--line:#27272a;--up:#f97066;--down:#32d583;--link:#60a5fa;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,"PingFang TC","Noto Sans TC",sans-serif;
padding:env(safe-area-inset-top) 16px calc(24px + env(safe-area-inset-bottom));max-width:640px;margin:auto}
header{padding:16px 0 4px;color:var(--mut);display:flex;justify-content:space-between;gap:8px}header a{color:var(--link);text-decoration:none;white-space:nowrap}
section{background:var(--card);border-radius:14px;padding:14px;margin:12px 0}
h2{font-size:16px;margin:0 0 8px;display:flex;justify-content:space-between}h2 small{color:var(--mut);font-weight:400}
.hero .big{font-size:34px;font-weight:700;font-variant-numeric:tabular-nums}.lbl,.muted,small{color:var(--mut);font-size:13px}
.up{color:var(--up)}.down{color:var(--down)}.row{display:flex;justify-content:space-between;gap:8px;padding:8px 0;border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}.row small{display:block}.r{text-align:right;font-variant-numeric:tabular-nums;display:flex;flex-direction:column}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;font-variant-numeric:tabular-nums}
.sec{display:grid;grid-template-columns:5.5em 1fr 4.5em;gap:8px;align-items:center;padding:3px 0;font-size:14px;font-variant-numeric:tabular-nums}
.sec span:last-child{text-align:right}.bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden}
.stock{padding:6px 0 10px;border-top:1px solid var(--line)}.stock:first-of-type{border-top:0}.stock .row{border:0}
.sig{font-size:13px;padding:1px 0}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--mut)}
.dot.up{background:var(--up)}.dot.down{background:var(--down)}
.news{font-size:13px;padding:6px 0 0}.news a{color:var(--link);text-decoration:none}.news small{display:block}
.tag{font-size:11px;border:1px solid var(--mut);border-radius:4px;padding:0 4px;margin-right:6px;color:var(--mut)}
.bar i{display:block;height:100%}.bar i.up{background:var(--up)}.bar i.down{background:var(--down)}
footer{color:var(--mut);font-size:12px;padding:8px 2px}
"""


def wrap(title, header, body, footer):
    return (f'<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            f'<title>{html.escape(title)}</title><style>{CSS}</style></head><body>'
            f'<header><span>{header.split("<a")[0]}</span>{"<a" + header.split("<a", 1)[1] if "<a" in header else ""}</header>'
            f'{body}<footer>{footer}</footer></body></html>')


def build_message(day, today, taiex, bfi, watch, screens, cfg, info=None):
    info = info or {}
    lines = [f"📊 台股 {day}"]
    if taiex:
        lines.append(f"加權 {taiex['close']:,.2f}  {sg(taiex['pts'])}（{sg(taiex['pct'], 2, '%')}）")
    if bfi:
        lines.append("法人：" + "｜".join(f"{k} {sg(v, 1)}億" for k, v in bfi.items() if k != "合計"))
    lines.append("自選：")
    for code in watch:
        p = today.get(code)
        if p:
            sig = (info.get("sig") or {}).get(code)
            nann = sum(1 for a in info.get("anns", []) if a["code"] == code)
            tail = f"｜{sig['label']}（多{sig['bull']} 空{sig['bear']}）" if sig else ""
            tail += f"｜重訊 {nann}" if nann else ""
            lines.append(f"{code} {p['name']} {p['close']:,.2f} {sg(pct_of(p['close'], p['chg']), 2, '%')}{tail}")
    if info.get("paper"):
        lines += paper_lines(info["paper"])
    lines.append("篩選：" + "｜".join(f"{k} {len(v)}" for k, v in screens.items() if not k.startswith("_")))
    if cfg.get("page_url"):
        lines.append(f"完整報告：{cfg['page_url']}")
    return "\n".join(lines)


def telegram(text):
    tok, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
    print(text)
    if tok and chat:
        try:
            SESSION.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                         json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=20)
        except requests.RequestException as ex:
            print("Telegram 失敗：", ex)


# ---------------- 美股 ----------------
def fetch_yahoo(sym):
    """Yahoo Finance 非官方日線，回傳最後一根完整 K 棒。"""
    from urllib.parse import quote
    for host in ("query1", "query2"):
        j = get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{quote(sym)}?range=1mo&interval=1d", tries=2)
        res = ((j or {}).get("chart") or {}).get("result") or []
        if not res:
            continue
        r = res[0]
        closes = (((r.get("indicators") or {}).get("quote") or [{}])[0].get("close")) or []
        pts = [(t, c) for t, c in zip(r.get("timestamp") or [], closes) if c is not None]
        if len(pts) < 2:
            continue
        (t1, c1), (_, c0) = pts[-1], pts[-2]
        off = (r.get("meta") or {}).get("gmtoffset") or 0
        date = dt.datetime.fromtimestamp(t1 + off, dt.timezone.utc).date().isoformat()
        return dict(close=c1, pct=(c1 / c0 - 1) * 100, m1=(c1 / pts[0][1] - 1) * 100, date=date)
    return None


def tw_close(code):
    files = sorted((DATA / "prices").glob("*.json")) if (DATA / "prices").exists() else []
    if not files:
        return None, None
    v = (load_json(files[-1], {}) or {}).get(code)
    return (v[0] if v else None), files[-1].stem


def us_row(label, sub, q, money=True):
    e = html.escape
    price = f"{q['close']:,.2f}" if q else "—"
    pct = q["pct"] if q else None
    sub_html = f"<small>{e(sub)}</small>" if sub else ""
    return (f'<div class="row"><div><b>{e(label)}</b>{sub_html}</div><div class="r"><span>{price}</span>'
            f'<span class="{cls(pct)}">{sg(pct, 2, "%")}</span></div></div>')


def main_us(force=False):
    cfg = load_json(ROOT / "config.json")
    us = cfg["us"]
    syms = list(us["indices"]) + [s for s in us["watchlist"] if s != "TSM"] + ["TSM", us["fx"]]
    q = {}
    for s in dict.fromkeys(syms):
        q[s] = fetch_yahoo(s)
        time.sleep(1)
    ref = q.get("^GSPC") or next((v for v in q.values() if v), None)
    if not ref:
        print("抓不到美股資料")
        sys.exit(1)
    day = ref["date"]
    mark = DATA / "us_last.txt"
    if mark.exists() and mark.read_text().strip() == day and not force:
        print(f"美股 {day} 已經產生過（或休市），跳過")
        return

    fx = (q.get(us["fx"]) or {}).get("close")
    tsm = q.get("TSM")
    tw, tw_day = tw_close("2330")
    adr_line = None
    if tsm and fx and tw:
        ntd = tsm["close"] * fx / 5
        prem = (ntd / tw - 1) * 100
        adr_line = f"換算 NT${ntd:,.0f}（台股 {tw:,.0f}，溢價 {prem:+.1f}%）"

    parts = []
    sp = q.get("^GSPC")
    if sp:
        parts.append(f'<section class="hero"><div class="lbl">S&amp;P 500</div><div class="big">{sp["close"]:,.2f}</div>'
                     f'<div class="{cls(sp["pct"])}">{sg(sp["pct"], 2, "%")}・近一月 {sg(sp["m1"], 1, "%")}</div></section>')
    parts.append("<section><h2>指數</h2>" + "".join(
        us_row(name, sym, q.get(sym)) for sym, name in us["indices"].items() if sym != "^GSPC") + "</section>")
    tw_part = us_row("台積電 ADR", adr_line or "TSM（1 股 ADR = 5 股 2330）", tsm)
    fx_part = us_row("美元／台幣", "", q.get(us["fx"])) if fx else ""
    parts.append(f"<section><h2>台股開盤參考</h2>{tw_part}{fx_part}</section>")
    parts.append("<section><h2>美股自選</h2>" + "".join(
        us_row(s, "", q.get(s)) for s in us["watchlist"] if s != "TSM") + "</section>")
    gen = dt.datetime.now(TW).strftime("%H:%M")
    DOCS.mkdir(exist_ok=True)
    (DOCS / "us.html").write_text(wrap(
        f"美股晨報 {day}", f'美股晨報・{day}（台灣 {gen} 更新）<a href="index.html">台股日報 →</a>', "".join(parts),
        "<p>資料來源：Yahoo Finance（非官方）。漲紅跌綠，與台股慣例一致。"
        f"ADR 溢價用最近一個台股交易日（{tw_day or '—'}）收盤計算。不是買賣建議。</p>"), encoding="utf-8")

    lines = [f"🇺🇸 美股 {day}"]
    for sym, name in us["indices"].items():
        v = q.get(sym)
        if v:
            lines.append(f"{name} {v['close']:,.2f} {sg(v['pct'], 2, '%')}")
    if tsm:
        lines.append(f"台積電 ADR ${tsm['close']:,.2f} {sg(tsm['pct'], 2, '%')}" + (f"｜{adr_line}" if adr_line else ""))
    if fx:
        lines.append(f"美元／台幣 {fx:.3f}")
    picks = [f"{s} {sg(q[s]['pct'], 1, '%')}" for s in us["watchlist"] if s != "TSM" and q.get(s)]
    if picks:
        lines.append("自選：" + "｜".join(picks))
    if cfg.get("page_url"):
        lines.append(f"完整報告：{cfg['page_url'].rstrip('/')}/us.html")
    telegram("\n".join(lines))
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text(day)


# ---------------- 主流程 ----------------
def main():
    cfg = load_json(ROOT / "config.json")
    force = "--force" in sys.argv
    prices, pdate = fetch_prices()
    if not prices:
        print("抓不到收盤資料")
        sys.exit(1)
    day = pdate or dt.datetime.now(TW).date().isoformat()
    pdir = DATA / "prices"
    compact = {c: [p["close"], round(p["vol"]), round(p["value"], 3)] for c, p in prices.items()}
    if (pdir / f"{day}.json").exists() and not force:
        print(f"{day} 已經產生過，跳過")
        return
    saved = sorted(p for p in pdir.glob("*.json") if p.stem < day) if pdir.exists() else []
    if saved and load_json(saved[-1]) == compact:
        print("資料和上一個交易日相同（休市或證交所尚未更新），跳過")
        return
    save_json(pdir / f"{day}.json", compact)
    for old in saved[:-79]:
        old.unlink()
    hist = [load_json(p, {}) for p in saved[-60:]]

    d = dt.date.fromisoformat(day)
    taiex, sectors = fetch_indices()
    inst = inst_history(d, cfg["screens"]["streak_days"])
    inst = [x for x in inst if x[0] <= d]
    for old in sorted((DATA / "inst").glob("*.json"))[:-30]:
        old.unlink()
    bfi = fetch_bfi(d)
    screens = run_screens(prices, hist, inst, cfg)
    anns, rev = fetch_announcements(), fetch_revenue()
    news = {}
    for code in cfg["watchlist"]:
        if code in prices:
            news[code] = fetch_news(prices[code]["name"])
            time.sleep(1)
    sig = {c: signals(c, prices[c], hist, inst, rev, anns, news.get(c, [])) for c in cfg["watchlist"] if c in prices}
    info = dict(anns=anns, rev=rev, news=news, sig=sig)
    info["paper"] = paper_step(day, prices, hist, inst, rev, anns, cfg)
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(
        build_html(day, prices, taiex, sectors, bfi, inst, cfg["watchlist"], screens, cfg, info), encoding="utf-8")
    telegram(build_message(day, prices, taiex, bfi, cfg["watchlist"], screens, cfg, info))


if __name__ == "__main__":
    if "us" in sys.argv[1:]:
        main_us(force="--force" in sys.argv)
    else:
        main()
