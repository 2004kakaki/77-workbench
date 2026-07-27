"""
77 投资工作台 - 本地行情后端
用 AkShare（免费开源，对接东方财富/新浪财经）取数据，包成本地 HTTP API 给前端用。

启动方式（项目根目录下）：
    pip install -r requirements.txt
    uvicorn main:app --reload --port 8000

AkShare 官方文档（接口名如果报错，先来这里核对是否改名了）：
    https://akshare.akfamily.xyz/data/index.html

支持的 market 取值：
    a      A股个股，code 例如 "000001"（平安银行）
    fund   开放式基金净值，code 为基金代码，例如 "110020"
    etf    ETF，code 为带交易所前缀的代码，例如 "sh510300"
    index  全球指数（含日经225等），从 index_global_spot_em 里按名称模糊匹配

注意：这些接口背后是免费公开数据源，有一定的更新延迟（一般是分钟级/日级，不是逐笔实时），
且对方网站偶尔会限流或调整字段名，稳定性不如付费终端，仅适合个人查看参考用。
"""

import os
from fastapi import FastAPI, HTTPException, Query, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from functools import lru_cache
import time
import akshare as ak
import pandas as pd

app = FastAPI(title="77 投资工作台 - 行情后端")

# 本地开发直接放开跨域，方便 frontend/index.html 用 fetch 访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 部署到公网后，任何人拿到网址都能调用这个后端。设置环境变量 API_KEY 后，
# 前端设置里也填同一个 key，请求会带 X-API-Key 头，不匹配就拒绝。
# 不设置 API_KEY 环境变量时默认不校验（本地自用图省事可以不设）。
API_KEY = os.environ.get("API_KEY", "")


def check_key(x_api_key: str = Header(default="")):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "API key 不对，去前端设置里检查一下")
    return True

# 简单的内存缓存，避免每次刷新页面都重新拉全市场数据（stock_zh_a_spot_em 返回全市场几千只股票，比较慢）
_cache = {}
CACHE_TTL = 60  # 秒


def cached(key, fn, ttl=CACHE_TTL):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit["t"] < ttl:
        return hit["v"]
    v = fn()
    _cache[key] = {"t": now, "v": v}
    return v


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/quote")
def quote(market: str = Query(...), code: str = Query(...), _=Depends(check_key)):
    """返回单个标的的最新价、涨跌幅等快照数据。"""
    try:
        if market == "a":
            df = cached("a_spot", ak.stock_zh_a_spot_em)
            row = df[df["代码"] == code]
            if row.empty:
                raise HTTPException(404, f"未找到A股代码 {code}")
            r = row.iloc[0]
            return {
                "name": r.get("名称"), "code": code, "price": float(r.get("最新价")),
                "change_pct": float(r.get("涨跌幅")), "prev_close": float(r.get("昨收")),
                "open": float(r.get("今开")), "high": float(r.get("最高")), "low": float(r.get("最低")),
            }

        if market == "fund":
            df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
            if df.empty:
                raise HTTPException(404, f"未找到基金代码 {code}")
            last = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else last
            nav = float(last["单位净值"])
            prev_nav = float(prev["单位净值"])
            return {
                "name": code, "code": code, "price": nav,
                "change_pct": (nav - prev_nav) / prev_nav * 100 if prev_nav else 0,
                "date": str(last.get("净值日期", "")),
            }

        if market == "etf":
            df = ak.fund_etf_hist_sina(symbol=code)
            if df.empty:
                raise HTTPException(404, f"未找到ETF代码 {code}")
            last = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else last
            close = float(last["close"])
            prev_close = float(prev["close"])
            return {
                "name": code, "code": code, "price": close,
                "change_pct": (close - prev_close) / prev_close * 100 if prev_close else 0,
                "date": str(last.get("date", "")),
            }

        if market == "index":
            df = cached("index_spot", ak.index_global_spot_em)
            row = df[df["名称"].str.contains(code, na=False)]
            if row.empty:
                raise HTTPException(404, f"未找到指数 {code}（试试更短的关键词，如“日经”）")
            r = row.iloc[0]
            return {
                "name": r.get("名称"), "code": r.get("代码"), "price": float(r.get("最新价")),
                "change_pct": float(r.get("涨跌幅")),
            }

        raise HTTPException(400, "market 需为 a / fund / etf / index")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"抓取失败：{e}")


@app.get("/api/hist")
def hist(market: str = Query(...), code: str = Query(...), days: int = 60, _=Depends(check_key)):
    """返回近期历史 OHLC，用于走势图/K线图。"""
    try:
        if market == "a":
            end = pd.Timestamp.today().strftime("%Y%m%d")
            start = (pd.Timestamp.today() - pd.Timedelta(days=days * 2)).strftime("%Y%m%d")
            df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq")
            df = df.tail(days)
            out = [
                {"date": str(r["日期"]), "open": float(r["开盘"]), "high": float(r["最高"]),
                 "low": float(r["最低"]), "close": float(r["收盘"])}
                for _, r in df.iterrows()
            ]
            return {"series": out}

        if market == "fund":
            df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势").tail(days)
            out = []
            prev = None
            for _, r in df.iterrows():
                nav = float(r["单位净值"])
                o = prev if prev is not None else nav
                out.append({"date": str(r.get("净值日期", "")), "open": o, "high": max(o, nav),
                             "low": min(o, nav), "close": nav})
                prev = nav
            return {"series": out}

        if market == "etf":
            df = ak.fund_etf_hist_sina(symbol=code).tail(days)
            out = [
                {"date": str(r["date"]), "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"])}
                for _, r in df.iterrows()
            ]
            return {"series": out}

        raise HTTPException(400, "market 需为 a / fund / etf（index 暂不支持历史K线）")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"抓取失败：{e}")
