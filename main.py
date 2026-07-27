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


# ---------------------------------------------------------------
# 今天关注：真实财经快讯 + 真实指数快照（不用 AI 生成，全部是抓来的原始数据）
# ---------------------------------------------------------------
CAT_KEYWORDS = {
    "A股": ["A股", "上证", "深证", "沪深", "创业板", "科创板", "北交所", "沪指", "深指"],
    "美股": ["美股", "纳斯达克", "道指", "标普", "美联储", "美国", "华尔街"],
    "日经": ["日经", "日本", "日元", "东京"],
    "ETF": ["ETF", "指数基金"],
    "FOF": ["FOF"],
    "LOF": ["LOF"],
    "QDII": ["QDII", "海外基金", "跨境"],
}


def tag_categories(title: str):
    hit = [cat for cat, kws in CAT_KEYWORDS.items() if any(k in title for k in kws)]
    return hit


@app.get("/api/news")
def news(_=Depends(check_key)):
    """真实财经快讯（东方财富-全球资讯）+ 几个关键指数的真实当日涨跌，作为“影响”的量化参考。"""
    try:
        try:
            df = ak.stock_info_global_em()
        except Exception:
            df = ak.stock_info_cjzc_em()  # 备用：东方财富-财经早餐
        df = df.head(15)
        title_col = "标题" if "标题" in df.columns else df.columns[0]
        time_col = "发布时间" if "发布时间" in df.columns else (df.columns[1] if len(df.columns) > 1 else None)
        items = []
        for _, r in df.iterrows():
            title = str(r.get(title_col, "")).strip()
            if not title:
                continue
            items.append({
                "title": title,
                "time": str(r.get(time_col, "")) if time_col else "",
                "cats": tag_categories(title),
            })

        # 指数快照：作为“对投资圈影响”的真实量化参照，而不是编的文字总结
        snapshot = []
        try:
            idx = ak.stock_zh_index_spot_em()
            for name, key in [("上证指数", "上证指数"), ("深证成指", "深证成指"), ("创业板指", "创业板指")]:
                row = idx[idx["名称"] == key]
                if not row.empty:
                    r = row.iloc[0]
                    snapshot.append({"name": name, "price": float(r["最新价"]), "change_pct": float(r["涨跌幅"])})
        except Exception:
            pass
        try:
            gidx = ak.index_global_spot_em()
            for kw, label in [("日经", "日经225"), ("道琼斯", "道琼斯"), ("纳斯达克", "纳斯达克")]:
                row = gidx[gidx["名称"].str.contains(kw, na=False)]
                if not row.empty:
                    r = row.iloc[0]
                    snapshot.append({"name": label, "price": float(r["最新价"]), "change_pct": float(r["涨跌幅"])})
        except Exception:
            pass

        return {"items": items, "snapshot": snapshot}
    except Exception as e:
        raise HTTPException(500, f"抓取失败：{e}")


# ---------------------------------------------------------------
# 每日一支：从真实财报数据里按“四点法”组织，不是 AI 编的判断
# ---------------------------------------------------------------
DEFAULT_POOL = ["600519", "000001", "600036", "000651", "601318", "300750", "000858", "600900"]


def n(x):
    try:
        v = float(x)
        return v if v == v else None  # filter NaN
    except Exception:
        return None


@app.get("/api/pick")
def pick(symbol: str = "", _=Depends(check_key)):
    """
    四点法真实数据版：
    1) 赚不赚钱 —— 最新净利润 + 同比增长率（真实财报）
    2) 护城河代理 —— ROE 在同行业中的真实排名
    3) 行业变大 —— 营业收入同比增长率 vs 行业平均（真实数据）
    4) 怎么赚钱 —— 真实的主营业务构成（收入分产品占比）
    没有 AI 编的“点评”，结论都是从数字直接推出来的简单规则判断。
    """
    try:
        code = symbol.strip() or DEFAULT_POOL[pd.Timestamp.today().dayofyear % len(DEFAULT_POOL)]

        info = ak.stock_individual_info_em(symbol=code)
        info_map = {str(r["item"]): r["value"] for _, r in info.iterrows()}
        name = info_map.get("股票简称", code)
        industry = info_map.get("行业", "未知")

        ind = ak.stock_financial_analysis_indicator_em(symbol=code)

        def latest(metric_name):
            rows = ind[ind["指标名称"].astype(str).str.contains(metric_name, na=False)]
            if rows.empty:
                return None
            r = rows.iloc[0]
            return {
                "period": str(r.get("报告期", "")),
                "value": n(r.get("指标值")),
                "yoy": n(r.get("同比增长率")),
                "industry_avg": n(r.get("行业平均")),
                "industry_rank": n(r.get("行业排名")),
            }

        revenue = latest("营业收入")
        profit = latest("净利润")
        roe = latest("ROE") or latest("净资产收益率")

        try:
            zygc = ak.stock_zygc_em(symbol=code)
            zygc = zygc.sort_values(zygc.columns[zygc.columns.str.contains("收入").tolist().index(True)] if any(zygc.columns.str.contains("收入")) else zygc.columns[0], ascending=False).head(4)
            biz_col = next((c for c in zygc.columns if "项目" in c or "产品" in c or "分类" in c), zygc.columns[0])
            pct_col = next((c for c in zygc.columns if "占比" in c), None)
            business = [
                {"item": str(r[biz_col]), "pct": str(r[pct_col]) if pct_col else ""}
                for _, r in zygc.iterrows()
            ]
        except Exception:
            business = []

        def profit_verdict():
            if not profit or profit["value"] is None:
                return "财报数据暂缺，换一支看看。"
            v = profit["value"]
            yoy = profit["yoy"]
            base = f"最新报告期（{profit['period']}）净利润 {v:.2f}（同比{'+' if (yoy or 0) >= 0 else ''}{yoy:.1f}%）。" if yoy is not None else f"最新报告期（{profit['period']}）净利润 {v:.2f}。"
            if v > 0 and (yoy or 0) >= 0:
                return base + " 利润为正且同比在增长，属于持续盈利。"
            elif v > 0:
                return base + " 目前仍盈利，但同比在下滑，要留意趋势是否延续。"
            else:
                return base + " 最新报告期是亏损的，需要更谨慎看待。"

        def moat_verdict():
            if not roe or roe["industry_rank"] is None:
                return "同行业排名数据暂缺。"
            rank = roe["industry_rank"]
            return f"ROE 同行业排名第 {int(rank)} 位（数字越小越靠前），{'排名靠前，盈利能力相对同行有优势' if rank <= 10 else '排名中等或靠后，护城河不算突出，自己再多确认一下'}。"

        def industry_verdict():
            if not revenue or revenue["yoy"] is None:
                return "行业增速数据暂缺。"
            yoy = revenue["yoy"]
            avg = revenue.get("industry_avg")
            cmp_txt = f"，行业平均为 {avg:.1f}%" if avg is not None else ""
            return f"公司营业收入同比 {'+' if yoy >= 0 else ''}{yoy:.1f}%{cmp_txt}。{'跑赢行业平均' if (avg is not None and yoy > avg) else '大致与行业同步或偏弱' if avg is not None else ''}"

        def biz_verdict():
            if not business:
                return "主营构成数据暂缺。"
            parts = "、".join([f"{b['item']}({b['pct']})" for b in business if b['pct']])
            return f"收入主要来自：{parts}" if parts else "主营构成数据暂缺。"

        return {
            "name": name, "code": code, "industry": industry,
            "profit": {"verdict": profit_verdict(), "raw": profit},
            "moat": {"verdict": moat_verdict(), "raw": roe},
            "industry_growth": {"verdict": industry_verdict(), "raw": revenue},
            "howtomoney": {"verdict": biz_verdict(), "raw": business},
        }
    except Exception as e:
        raise HTTPException(500, f"抓取失败：{e}（换一支代码试试，用 ?symbol=600519 这种方式指定）")
