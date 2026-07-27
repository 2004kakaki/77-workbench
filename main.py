"""
77 投资工作台 - 后端 (v3)
用 AkShare（免费开源，对接东方财富/新浪财经）取真实数据。

支持接口：
    GET /api/health
    GET /api/quote?market=a|fund|etf|index&code=...
    GET /api/hist?market=a|fund|etf&code=...&days=60
    GET /api/news              今日财经快讯，自动分类 + 关联基金/股票 + 摘要全文
    GET /api/pick               每日一支：真实财报数据的四点法拆解

AkShare 官方文档（接口报错先来这里核对函数名是否变了）：
    https://akshare.akfamily.xyz/data/index.html
"""

import os
import time
import re
from datetime import date
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Query, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
import akshare as ak
import pandas as pd

app = FastAPI(title="77 投资工作台 - 行情后端")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

API_KEY = os.environ.get("API_KEY", "")


def check_key(x_api_key: str = Header(default="")):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "API key 不对，去前端设置里检查一下")
    return True


_cache = {}
CACHE_TTL = 60


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


# ---------------- 行情快照 / 历史 ----------------

@app.get("/api/quote")
def quote(market: str = Query(...), code: str = Query(...), _=Depends(check_key)):
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
            nav = float(last["单位净值"]); prev_nav = float(prev["单位净值"])
            return {"name": code, "code": code, "price": nav,
                    "change_pct": (nav - prev_nav) / prev_nav * 100 if prev_nav else 0,
                    "date": str(last.get("净值日期", ""))}
        if market == "etf":
            df = ak.fund_etf_hist_sina(symbol=code)
            if df.empty:
                raise HTTPException(404, f"未找到ETF代码 {code}")
            last = df.iloc[-1]; prev = df.iloc[-2] if len(df) > 1 else last
            close = float(last["close"]); prev_close = float(prev["close"])
            return {"name": code, "code": code, "price": close,
                    "change_pct": (close - prev_close) / prev_close * 100 if prev_close else 0,
                    "date": str(last.get("date", ""))}
        if market == "index":
            df = cached("index_spot", ak.index_global_spot_em)
            row = df[df["名称"].str.contains(code, na=False)]
            if row.empty:
                raise HTTPException(404, f"未找到指数 {code}")
            r = row.iloc[0]
            return {"name": r.get("名称"), "code": r.get("代码"), "price": float(r.get("最新价")),
                    "change_pct": float(r.get("涨跌幅"))}
        raise HTTPException(400, "market 需为 a / fund / etf / index")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"抓取失败：{e}")


@app.get("/api/hist")
def hist(market: str = Query(...), code: str = Query(...), days: int = 60, _=Depends(check_key)):
    try:
        if market == "a":
            end = pd.Timestamp.today().strftime("%Y%m%d")
            start = (pd.Timestamp.today() - pd.Timedelta(days=days * 2)).strftime("%Y%m%d")
            df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq").tail(days)
            return {"series": [{"date": str(r["日期"]), "open": float(r["开盘"]), "high": float(r["最高"]),
                                 "low": float(r["最低"]), "close": float(r["收盘"])} for _, r in df.iterrows()]}
        if market == "fund":
            df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势").tail(days)
            out, prev = [], None
            for _, r in df.iterrows():
                nav = float(r["单位净值"]); o = prev if prev is not None else nav
                out.append({"date": str(r.get("净值日期", "")), "open": o, "high": max(o, nav),
                             "low": min(o, nav), "close": nav})
                prev = nav
            return {"series": out}
        if market == "etf":
            df = ak.fund_etf_hist_sina(symbol=code).tail(days)
            return {"series": [{"date": str(r["date"]), "open": float(r["open"]), "high": float(r["high"]),
                                 "low": float(r["low"]), "close": float(r["close"])} for _, r in df.iterrows()]}
        raise HTTPException(400, "market 需为 a / fund / etf")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"抓取失败：{e}")


# ---------------- 今天关注：真实快讯 + 分类 + 关联标的 ----------------

CATEGORY_KEYWORDS = {
    "A股": ["A股", "沪深", "上证", "深证", "创业板", "科创板", "沪指", "深指", "两市"],
    "美股": ["美股", "纳斯达克", "道指", "标普", "美联储", "纳指"],
    "日经": ["日经", "日本央行", "日股", "东京"],
    "ETF": ["ETF"],
    "FOF": ["FOF"],
    "LOF": ["LOF"],
    "QDII": ["QDII", "海外基金"],
}

# 主题关键词 -> (名称, 代码) 关联基金，命中就带出来，纯粹是给用户一个查证的方向
RELATED_MAP = [
    (["白酒", "茅台", "五粮液"], "白酒ETF", "512690"),
    (["半导体", "芯片"], "半导体ETF", "512480"),
    (["新能源车", "锂电", "宁德时代", "比亚迪"], "新能源车ETF", "515030"),
    (["医药", "生物医药", "创新药"], "医药ETF", "512010"),
    (["银行"], "银行ETF", "512800"),
    (["军工", "国防"], "军工ETF", "512660"),
    (["黄金"], "黄金ETF", "518880"),
    (["地产", "房地产"], "地产ETF", "512200"),
    (["原油", "石油", "能源"], "能源ETF", "159930"),
    (["纳斯达克", "美股科技"], "纳指ETF", "513100"),
    (["标普"], "标普500ETF", "513500"),
    (["日经", "日本"], "日经225ETF", "513520"),
    (["算力", "服务器", "AI"], "科技ETF", "515000"),
]


def classify_and_relate(text: str):
    cats = [c for c, kws in CATEGORY_KEYWORDS.items() if any(k in text for k in kws)]
    related = []
    for kws, name, code in RELATED_MAP:
        if any(k in text for k in kws):
            related.append(f"{name}({code})")
        if len(related) >= 2:
            break
    return cats, related


def build_name_index():
    """代码-简称 全量列表，用来在新闻里识别提到了哪家上市公司。"""
    try:
        df = ak.stock_info_a_code_name()
        return [(r["name"], r["code"]) for _, r in df.iterrows() if len(str(r["name"])) >= 3]
    except Exception:
        return []


def find_mentioned_stocks(text: str, name_index, spot_df):
    hits = []
    for name, code in name_index:
        if name in text:
            row = spot_df[spot_df["代码"] == code]
            if not row.empty:
                pct = row.iloc[0].get("涨跌幅")
                hits.append({"name": name, "code": code, "change_pct": float(pct) if pct is not None else None})
        if len(hits) >= 3:
            break
    return hits


MARKET_SIGNAL_KEYWORDS = [
    "股", "基金", "ETF", "指数", "板块", "涨", "跌", "IPO", "上市", "回购", "增持", "减持",
    "并购", "重组", "财报", "净利润", "营收", "业绩", "央行", "利率", "汇率", "关税", "制裁",
    "监管", "融资", "债券", "期货", "原油", "黄金", "芯片", "半导体",
]


def is_market_relevant(text, cats, related, stocks):
    if cats or related or stocks:
        return True
    return any(k in text for k in MARKET_SIGNAL_KEYWORDS)


def make_interpretation(cats, related, stocks):
    if stocks:
        s = stocks[0]
        if s["change_pct"] is not None:
            direction = "上涨" if s["change_pct"] >= 0 else "下跌"
            return (f"消息面提到了{s['name']}（{s['code']}），该股今日实际{direction} "
                    f"{abs(s['change_pct']):.2f}%，可以理解为市场目前对这条消息的反应"
                    f"{'偏正面' if s['change_pct']>=0 else '偏负面'}，但个股波动也可能混杂其他因素，"
                    f"不能全部归因于这一条新闻。")
    if related:
        return f"这类消息通常影响相关板块的资金情绪，可以关注 {related[0]} 这类关联标的近期走势来交叉验证市场反应。"
    if cats:
        return f"这条消息和 {'、'.join(cats)} 板块相关，建议结合板块指数当日涨跌幅一起看，单条消息的影响力有限。"
    return "这条消息对具体标的的传导路径不算直接，仅供了解背景。"


@app.get("/api/news")
def news(_=Depends(check_key)):
    try:
        df = cached("news_em", ak.stock_info_global_em, ttl=300)
        spot_df = cached("a_spot", ak.stock_zh_a_spot_em)
        name_index = cached("name_index", build_name_index, ttl=3600)

        items = []
        for _, r in df.iterrows():
            title = str(r.get("标题", "")).strip()
            summary = str(r.get("摘要", "")).strip()
            pub_time = str(r.get("发布时间", ""))
            link = str(r.get("链接", ""))
            text = title + summary
            cats, related = classify_and_relate(text)
            stocks = find_mentioned_stocks(text, name_index, spot_df)

            if not is_market_relevant(text, cats, related, stocks):
                continue

            stock_related = [f"{s['name']}({s['code']})" for s in stocks]
            all_related = stock_related + related

            items.append({
                "headline": title,
                "time": pub_time,
                "detail": summary or "（这条快讯没有更多摘要，点标题旁边的链接看原文）",
                "link": link,
                "cats": cats,
                "related": "、".join(all_related) if all_related else "暂无对应标的",
                "interpretation": make_interpretation(cats, related, stocks),
            })
            if len(items) >= 15:
                break

        return {"items": items}
    except Exception as e:
        raise HTTPException(500, f"抓取新闻失败：{e}")


# ---------------- 每日一支：真实财报数据四点法 ----------------

PICK_POOL = [
    ("600519", "贵州茅台"), ("000858", "五粮液"), ("601318", "中国平安"),
    ("000333", "美的集团"), ("600036", "招商银行"), ("300750", "宁德时代"),
    ("002594", "比亚迪"), ("601888", "中国中免"), ("600276", "恒瑞医药"),
    ("600030", "中信证券"), ("601899", "紫金矿业"), ("600809", "山西汾酒"),
    ("002415", "海康威视"), ("600900", "长江电力"), ("601166", "兴业银行"),
    ("000651", "格力电器"), ("600887", "伊利股份"), ("601012", "隆基绿能"),
    ("600585", "海螺水泥"), ("000568", "泸州老窖"),
]


def _num(x):
    """把 '1,234,567.00元' 这种字符串安全转成 float"""
    try:
        s = str(x).replace(",", "").replace("元", "").replace("%", "").strip()
        if s in ("", "nan", "None", "--"):
            return None
        return float(s)
    except Exception:
        return None


def _find_col(df, must_contain):
    """按包含的子串找列名，兼容不同 akshare 版本的列名差异。"""
    for c in df.columns:
        if all(k in c for k in must_contain):
            return c
    return None


def _recent_quarter_ends(n=4):
    """最近 n 个季度末日期字符串，形如 '20250331'，最新的排在最前面。"""
    today = date.today()
    ends = []
    y, m = today.year, today.month
    q_months = [3, 6, 9, 12]
    # 从当前月往前找最近一个已经过去的季度末
    idx = 0
    while len(ends) < n:
        cand_year = y
        # 找 <= 当前(y,m) 的季度月
        candidates = [(y, qm) for qm in q_months if qm <= m] or [(y - 1, 12)]
        cy, cm = candidates[-1]
        day = 31 if cm == 12 else (30 if cm in (6, 9) else 31)
        s = f"{cy}{cm:02d}{day:02d}"
        if s not in ends:
            ends.append(s)
        # 往前推一个季度
        qi = q_months.index(cm)
        if qi == 0:
            y, m = cy - 1, 12
        else:
            y, m = cy, q_months[qi - 1]
    return ends


@app.get("/api/pick")
def pick(_=Depends(check_key)):
    idx = date.today().timetuple().tm_yday % len(PICK_POOL)
    code, name = PICK_POOL[idx]
    try:
        row, report_date = None, None
        last_err = None
        for qend in _recent_quarter_ends():
            try:
                df = cached(f"yjbb_{qend}", lambda qend=qend: ak.stock_yjbb_em(date=qend), ttl=3600)
                match = df[df["股票代码"] == code]
                if not match.empty:
                    row, report_date = match.iloc[0], qend
                    break
            except Exception as e:
                last_err = e
                continue
        if row is None:
            raise Exception(f"最近几个报告期都没查到 {code} 的业绩数据（{last_err}）")

        col_rev = _find_col(row.to_frame().T, ["营业收入"]) or "营业收入"
        col_rev_yoy = _find_col(row.to_frame().T, ["营业收入", "同比"])
        col_profit = _find_col(row.to_frame().T, ["净利润"]) or "净利润"
        col_profit_yoy = _find_col(row.to_frame().T, ["净利润", "同比"])
        col_roe = _find_col(row.to_frame().T, ["净资产收益率"])
        col_industry = _find_col(row.to_frame().T, ["所处行业"])
        col_margin = _find_col(row.to_frame().T, ["毛利率"])

        revenue = _num(row.get(col_rev))
        profit = _num(row.get(col_profit))
        rev_yoy = _num(row.get(col_rev_yoy)) if col_rev_yoy else None
        profit_yoy = _num(row.get(col_profit_yoy)) if col_profit_yoy else None
        roe = _num(row.get(col_roe)) if col_roe else None
        margin = _num(row.get(col_margin)) if col_margin else None
        industry = row.get(col_industry) if col_industry else None

        spot = cached("a_spot", ak.stock_zh_a_spot_em)
        srow = spot[spot["代码"] == code]
        cap = float(srow.iloc[0].get("总市值")) if not srow.empty else None
        pe = srow.iloc[0].get("市盈率-动态") if not srow.empty and "市盈率-动态" in srow.columns else None

        def fmt_yi(v):
            return f"{v/1e8:.1f}亿元" if v else "数据缺失"
        def fmt_pct(v):
            return f"{v:+.1f}%" if v is not None else "暂无同比数据"

        return {
            "name": name, "code": code, "cat": "A股",
            "report_date": f"{report_date[:4]}-{report_date[4:6]}-{report_date[6:]}",
            "one_liner": f"{name}（{code}），所属行业「{industry or '未知'}」，最新报告期真实财报数据如下。",
            "profit": {
                "verdict": f"最新一期营业收入 {fmt_yi(revenue)}，净利润 {fmt_yi(profit)}，净利润同比 {fmt_pct(profit_yoy)}。",
                "data": f"营业收入同比 {fmt_pct(rev_yoy)}，净资产收益率(ROE) {fmt_pct(roe)}；"
                        f"数据来自东方财富-上市公司业绩报表，未做扣非调整，仅供初步参考。",
            },
            "moat": {
                "verdict": "护城河强弱这里不做主观判断，建议核对该公司近3年毛利率是否稳定，越稳定说明议价权越强。",
                "data": f"最新销售毛利率 {fmt_pct(margin)}；当前总市值约 {fmt_yi(cap)}，动态市盈率 {pe if pe else '暂无'}，"
                        f"市值和估值可以帮助你判断市场当下怎么给它的护城河定价。",
            },
            "industry": {
                "verdict": f"所处行业分类为「{industry or '未知'}」，行业空间建议结合最新年报「行业发展趋势」章节自行判断。",
                "data": "可以在巨潮资讯网（cninfo.com.cn）搜这家公司代码查最新年报原文。",
            },
            "howtomoney": {
                "verdict": f"{name} 的收入结构建议直接查利润表里的分产品/分地区收入明细来确认主营业务占比。",
                "data": "这里只有汇总数字，明细要看完整利润表，可在东方财富/巨潮资讯查该股“主营构成”页面。",
            },
            "key_takeaway": [
                f"最新净利润 {fmt_yi(profit)}，同比 {fmt_pct(profit_yoy)}（数据点：业绩报表-净利润）",
                f"最新营业收入 {fmt_yi(revenue)}，同比 {fmt_pct(rev_yoy)}（数据点：业绩报表-营业收入）",
                f"净资产收益率 {fmt_pct(roe)}，销售毛利率 {fmt_pct(margin)}（数据点：业绩报表）",
                f"当前总市值 {fmt_yi(cap)}，动态市盈率 {pe if pe else '暂无'}（数据点：实时行情-东方财富）",
            ],
            "risk": "自动生成的四点法只覆盖了报表层面的数字，护城河和行业空间需要你自己读年报判断，不要只看这个页面就下单。",
        }
    except Exception as e:
        raise HTTPException(500, f"抓取 {name}({code}) 的财报数据失败：{e}")
