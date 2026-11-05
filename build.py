#!/usr/bin/env python3
"""美股每日十分鐘：每日收市後組裝 data.json（+ history.json）。

數據來源（全部現成管道）：
- 觀察名單＋指數：~/workspace/x-finance/fetch_market_data.py（Yahoo）
- 板塊資金流：~/workspace/x-finance/sector_flow.py（Yahoo 5m bar，Chaikin 近似）
- 黑盒 Top 10：https://at3838.github.io/blackbox-models/data.json（08:51 同步）
- 內幕交易：SEC EDGAR Form 4（/tmp 副本跑 fetch_form4.py，唔掂正本 seen 檔）
- 財報：~/workspace/goals/goal/hidden_files/earnings-nightly-*.txt
- 宏觀：Yahoo chart（^TNX、CL=F、GC=F、BTC-USD）

用法：python3 build.py [--date YYYY-MM-DD] [--form4 FILE] [--repo DIR]
預設 date = 市場數據入面最新收市日；預設 repo = 呢個 script 所在目錄。
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
HOME = os.path.expanduser("~")
BASE = os.path.dirname(os.path.abspath(__file__))
XF = os.path.join(HOME, "workspace", "x-finance")
HIDDEN = os.path.join(HOME, "workspace", "goals", "goal", "hidden_files")
WATCHLIST_ORDER = ["TSLA", "NVDA", "AAPL", "SPCX", "GOOGL", "NBIS", "CRWV",
                   "ONDS", "PLTR", "RKLB", "AVAV", "SMR", "UNH", "META",
                   "MSFT", "AMZN", "INTC", "AMD", "AVGO", "SNDK", "MU",
                   "TSM", "BE", "SOFI", "ASTS"]
NAME_MAP_TOMORROW = {"Airbnb": "ABNB", "Air Products": "APD",
                     "Warner Bros. Discovery": "WBD", "SMR": "SMR"}


def get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout).read()


def yahoo_last2(ticker):
    """Yahoo chart 1d：返最近兩個收市價 [(date, close), ...]。"""
    d = json.loads(get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?interval=1d&range=5d"))
    r = d["chart"]["result"][0]
    ts = r["timestamp"]
    closes = r["indicators"]["quote"][0]["close"]
    pts = []
    for t, c in zip(ts, closes):
        if c is not None:
            pts.append((datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"), c))
    return pts[-2:]


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[-n:]) / n
    al = sum(losses[-n:]) / n
    for i in range(len(closes) - n, len(closes)):
        pass
    # Wilder 平滑：由頭計起
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return round(100 - 100 / (1 + rs), 1)


def parse_args():
    args = {"date": None, "form4": None, "repo": BASE}
    for i, a in enumerate(sys.argv[1:]):
        if a == "--date":
            args["date"] = sys.argv[i + 2]
        elif a == "--form4":
            args["form4"] = sys.argv[i + 2]
        elif a == "--repo":
            args["repo"] = sys.argv[i + 2]
    return args


def latest_earnings_file():
    cands = []
    for f in os.listdir(HIDDEN):
        if f.startswith("earnings-nightly-"):
            cands.append(os.path.join(HIDDEN, f))
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def parse_earnings(path, tomorrow_str):
    """由財報跟進 txt 抽每間公司：ticker、名、時段、一句撮要、股價升跌。"""
    if not path or not os.path.exists(path):
        return [], []
    text = open(path, encoding="utf-8", errors="ignore").read()
    out = []
    # 1. McDonald's MCD（開市前出）
    pat = re.compile(r"^(\d+)\.\s+(.+?)\s+([A-Z]{1,5})（([^）]*)）", re.M)
    matches = list(pat.finditer(text))
    for i, m in enumerate(matches):
        body = text[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        first = body.strip().split("。")[0].strip()
        if len(first) > 160:
            first = first[:160] + "…"
        chg = None
        pm = re.search(r"股價：.*?([升跌])([\d.]+)%", body, re.S)
        if pm:
            chg = float(pm.group(2)) * (1 if pm.group(1) == "升" else -1)
        out.append({"ticker": m.group(3), "name": m.group(2).strip(),
                    "timing": m.group(4).strip(), "summary": first, "chg": chg})
    tomorrow = []
    seen_t = set()
    for sent in re.split(r"[。\n]", text):
        if tomorrow_str in sent:
            for name, tick in NAME_MAP_TOMORROW.items():
                if name in sent and tick not in seen_t:
                    seen_t.add(tick)
                    note = "收市後出業績" if "收市後" in sent else "出業績"
                    tomorrow.append({"ticker": tick, "name": name, "note": note})
            for tick in sorted(set(re.findall(r"\b[A-Z]{2,5}\b", sent))):
                if tick in WATCHLIST_ORDER and tick not in seen_t:
                    seen_t.add(tick)
                    note = "收市後出業績" if "收市後" in sent else "出業績"
                    tomorrow.append({"ticker": tick, "name": tick, "note": note})
    return out, tomorrow


def run_form4_copy():
    """用 /tmp 副本跑 fetch_form4.py（唔掂正本 form4-seen.txt），回傳行列。"""
    copy_dir = "/tmp/insider-copy"
    src = os.path.join(HOME, "workspace", "insider-track")
    subprocess.run(["rm", "-rf", copy_dir], check=False)
    subprocess.run(["cp", "-r", src, copy_dir], check=True)
    seen = os.path.join(copy_dir, "form4-seen.txt")
    if os.path.exists(seen):
        os.remove(seen)
    try:
        r = subprocess.run([sys.executable, os.path.join(copy_dir, "fetch_form4.py"),
                            "--days", "2"], capture_output=True, text=True,
                           timeout=600, cwd=copy_dir)
        lines = [l[2:].strip() for l in r.stdout.splitlines()
                 if l.startswith("• ")]
        return lines
    except Exception as e:
        print(f"WARN form4: {e}", file=sys.stderr)
        return []


def main():
    args = parse_args()
    repo = args["repo"]

    # --- 1. 市場數據（先跑 fresh，失敗就用現成檔） ---
    market_path = "/tmp/stock-web-data/data.json"
    try:
        subprocess.run([sys.executable,
                        os.path.join(XF, "fetch_market_data.py")],
                       capture_output=True, timeout=600)
    except Exception as e:
        print(f"WARN fetch_market_data: {e}", file=sys.stderr)
    market = json.load(open(market_path))
    idx = market["indices"]
    target = args["date"] or idx["標普500"]["prices"][-1]["d"]
    td = datetime.strptime(target, "%Y-%m-%d").date()
    wd = "一二三四五六日"[td.weekday()]
    tomorrow = td + timedelta(days=1)
    tomorrow_str = f"{tomorrow.month}月{tomorrow.day}號"

    # --- 2. 大市 ---
    def idx_chg(name):
        ps = idx[name]["prices"]
        return {"close": ps[-1]["c"],
                "chg": round((ps[-1]["c"] / ps[-2]["c"] - 1) * 100, 2)}
    spx, dji, ndx = idx_chg("標普500"), idx_chg("道瓊斯"), idx_chg("納斯達克")
    vix_ps = idx["VIX"]["prices"]
    vix = {"close": vix_ps[-1]["c"],
           "chg": round((vix_ps[-1]["c"] / vix_ps[-2]["c"] - 1) * 100, 1)}
    macro = []
    for ticker, name, unit in [("^TNX", "十年債息", "%"), ("CL=F", "油價", "美元"),
                               ("GC=F", "黃金", "美元"), ("BTC-USD", "Bitcoin", "美元")]:
        try:
            pts = yahoo_last2(ticker)
            c0, c1 = pts[-2][1], pts[-1][1]
            if ticker == "^TNX":
                macro.append({"name": name, "value": round(c1, 2),
                              "chg": round(c1 - c0, 2), "unit": "厘"})
            else:
                macro.append({"name": name, "value": round(c1, 2),
                              "chg": round((c1 / c0 - 1) * 100, 2), "unit": "%"})
        except Exception as e:
            print(f"WARN macro {ticker}: {e}", file=sys.stderr)

    # --- 3. 觀察名單：現價、升跌、50/200 日線、RSI ---
    watchlist = []
    for sym in WATCHLIST_ORDER:
        s = market["stocks"].get(sym)
        if not s:
            continue
        closes = [p["c"] for p in s["prices"]]
        price = closes[-1]
        chg = round((price / closes[-2] - 1) * 100, 2)
        ma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
        ma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else None
        watchlist.append({
            "symbol": sym, "name": s.get("name") or sym, "price": price,
            "chg": chg,
            "above50": (price > ma50) if ma50 else None,
            "above200": (price > ma200) if ma200 else None,
            "rsi": rsi(closes),
        })
    watchlist_sorted = sorted(watchlist, key=lambda x: -x["chg"])
    up = sum(1 for w in watchlist if w["chg"] > 0)
    above50 = sum(1 for w in watchlist if w["above50"])

    # --- 4. 板塊資金流 ---
    sys.path.insert(0, XF)
    import sector_flow as sf
    p1, p2 = sf.day_range_et(target)
    sectors = []
    for ticker, name in sf.SECTORS:
        try:
            flow, _shares, _diff = sf.best_flow(ticker, p1, p2, target)
            sectors.append({"name": name, "ticker": ticker,
                            "flow_m": round(flow / 1e6)})
        except Exception as e:
            print(f"WARN sector {ticker}: {e}", file=sys.stderr)
    sectors.sort(key=lambda x: -x["flow_m"])
    # 板塊圖：sector_flow.py 會自己畫，呢度直接畫一張放 repo
    try:
        chart_src = os.path.join(XF, f"sector_flow_{target}.png")
        if not os.path.exists(chart_src):
            sf.make_chart([(s["name"], s["ticker"], s["flow_m"])
                           for s in sectors], target, chart_src)
        import shutil
        shutil.copy(chart_src, os.path.join(repo, "sector_flow.png"))
    except Exception as e:
        print(f"WARN sector chart: {e}", file=sys.stderr)
    inflow_n = sum(1 for s in sectors if s["flow_m"] > 0)

    # --- 5. 黑盒 Top 10 ---
    blackbox = []
    try:
        bb = json.loads(get("https://at3838.github.io/blackbox-models/data.json"))
        for i, r in enumerate(bb.get("top30", [])[:10]):
            blackbox.append({"rank": i + 1, "symbol": r.get("symbol"),
                             "composite": round(r.get("composite") or 0, 2),
                             "sector": r.get("sector"),
                             "chg": (round(r["chgPct"] * 100, 2)
                                     if r.get("chgPct") is not None else None),
                             "price": r.get("price")})
    except Exception as e:
        print(f"WARN blackbox: {e}", file=sys.stderr)

    # --- 6. Form 4 內幕交易 ---
    if args["form4"] and os.path.exists(args["form4"]):
        lines = [l.strip() for l in open(args["form4"], encoding="utf-8")
                 if l.strip()]
    else:
        lines = run_form4_copy()
    trades = []
    for l in lines[:12]:
        trades.append({"text": l, "buy": "買入" in l})
    buys = sum(1 for t in trades if t["buy"])

    # --- 7. 財報＋明日觀察 ---
    ef = latest_earnings_file()
    earnings, tomorrow_list = parse_earnings(ef, tomorrow_str)

    # --- 8. 今日要聞（觀察名單新聞去重，最新 6 則） ---
    news_seen, news = set(), []
    all_news = []
    for sym in WATCHLIST_ORDER:
        s = market["stocks"].get(sym)
        for n in (s.get("news") or [])[:4]:
            all_news.append(n)
    all_news.sort(key=lambda n: n.get("p", ""), reverse=True)
    for n in all_news:
        if n.get("t") in news_seen:
            continue
        news_seen.add(n.get("t"))
        news.append({"title": n.get("t"), "link": n.get("l"),
                     "source": n.get("s"), "time": n.get("p")})
        if len(news) >= 6:
            break

    # --- 9. 今日結構一句話（由數字砌，唔估） ---
    direction = "升" if spx["chg"] >= 0 else "跌"
    best = sectors[0] if sectors else None
    worst = sectors[-1] if sectors else None
    headline = (f"標普 500 {direction} {abs(spx['chg']):.2f}% 收 {spx['close']:,.0f} 點，"
                f"道指 {'升' if dji['chg'] >= 0 else '跌'} {abs(dji['chg']):.2f}%，"
                f"納指 {'升' if ndx['chg'] >= 0 else '跌'} {abs(ndx['chg']):.2f}%；"
                f"11 個板塊 {inflow_n} 個淨流入，"
                f"{best['name']}最強（+{best['flow_m']:,.0f}M），"
                f"{worst['name']}最弱（{worst['flow_m']:,.0f}M）；"
                f"觀察名單 25 隻 {up} 升 {25 - up} 跌，"
                f"{watchlist_sorted[0]['symbol']}最勁 +{watchlist_sorted[0]['chg']:.1f}%，"
                f"{watchlist_sorted[-1]['symbol']}最弱 {watchlist_sorted[-1]['chg']:.1f}%。")
    big = [w for w in watchlist if abs(w["chg"]) >= 5]
    focus = ""
    if big:
        focus = "焦點：" + "、".join(
            f"{w['symbol']} {'升' if w['chg'] >= 0 else '跌'} {abs(w['chg']):.1f}%"
            for w in sorted(big, key=lambda x: -abs(x["chg"]))[:4]) + "。"

    now_lon = datetime.now(timezone(timedelta(hours=0))).astimezone(
        timezone(timedelta(hours=0)))
    data = {
        "date": target, "weekday": f"星期{wd}",
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "headline": headline, "focus": focus,
        "market": {
            "indices": [
                {"name": "標普500", "close": spx["close"], "chg": spx["chg"]},
                {"name": "道瓊斯", "close": dji["close"], "chg": dji["chg"]},
                {"name": "納斯達克", "close": ndx["close"], "chg": ndx["chg"]},
            ],
            "vix": vix, "macro": macro,
        },
        "breadth": {"up": up, "down": 25 - up, "above50": above50, "total": 25},
        "sectors": sectors,
        "watchlist": watchlist_sorted,
        "blackbox": blackbox,
        "insider": {"buys": buys, "sells": len(trades) - buys, "trades": trades},
        "earnings": earnings,
        "tomorrow": tomorrow_list,
        "news": news,
        "sources": "Yahoo Finance、SEC EDGAR、黑盒模型（space-6）、財報跟進",
        "disclaimer": "僅供參考，不構成投資建議。",
    }
    with open(os.path.join(repo, "data.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)

    # history.json：過去 30 日一句話
    hist_path = os.path.join(repo, "history.json")
    hist = []
    if os.path.exists(hist_path):
        try:
            hist = json.load(open(hist_path))
        except Exception:
            hist = []
    entry = {"date": target, "headline": headline,
             "spx_close": spx["close"], "spx_chg": spx["chg"],
             "inflow_n": inflow_n,
             "best_sector": best["name"] if best else None,
             "worst_sector": worst["name"] if worst else None}
    hist = [h for h in hist if h.get("date") != target] + [entry]
    hist = hist[-30:]
    with open(hist_path, "w", encoding="utf-8") as fh:
        json.dump(hist, fh, ensure_ascii=False, indent=2)

    print(f"OK {target}（星期{wd}）：data.json + history.json 寫好，"
          f"板塊 {inflow_n}/11 流入，觀察名單 {up}/25 升，"
          f"Form 4 {len(trades)} 筆，財報 {len(earnings)} 間，明日 {len(tomorrow_list)} 隻。")


if __name__ == "__main__":
    main()
