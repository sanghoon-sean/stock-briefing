#!/usr/bin/env python3
"""
Daily Stock Briefing
 - 주가: yfinance (Yahoo Finance)
 - 뉴스: Google News RSS
 - 발송: SendGrid v3 API
 - 스케줄: cron '0 22 * * *' (UTC) = 07:00 KST
"""

import html as html_lib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import xml.etree.ElementTree as ET

import requests
import yfinance as yf

# ── Config ────────────────────────────────────────────────────────────────────
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
TO_EMAIL         = os.environ.get("TO_EMAIL",   "seanc1122@gmail.com")
FROM_EMAIL       = os.environ.get("FROM_EMAIL", "seanc1122@gmail.com")
KST = ZoneInfo("Asia/Seoul")

# (ticker, display_name, currency, card_group)
TICKERS = [
    ("^GSPC",     "S&P 500",             "",    "us"),
    ("^IXIC",     "NASDAQ",              "",    "us"),
    ("^DJI",      "Dow Jones",           "",    "us"),
    ("^VIX",      "VIX 공포지수",        "",    "us"),
    ("^KS11",     "KOSPI",               "KRW", "kr"),
    ("^KQ11",     "KOSDAQ",              "KRW", "kr"),
    ("KRW=X",     "USD/KRW",             "",    "kr"),
    ("005930.KS", "삼성전자",            "KRW", "semi"),
    ("000660.KS", "SK하이닉스",          "KRW", "semi"),
    ("005380.KS", "현대자동차",          "KRW", "robot"),
    ("066570.KS", "LG전자",              "KRW", "robot"),
    ("NVDA",      "NVIDIA",              "USD", "semi"),
    ("TSM",       "TSMC",                "USD", "semi"),
    ("ARKX",      "ARKX ETF",            "USD", "etf"),
    ("465780.KS", "TIGER 우주테크TOP10", "KRW", "etf"),
]

# Akros U.S. Space Tech Index 구성 (참고용 — 분기마다 수동 업데이트)
TIGER_HOLDINGS = [
    ("Rocket Lab (RKLB)",          "~22%"),
    ("AST SpaceMobile (ASTS)",     "~19%"),
    ("Intuitive Machines (LUNR)",  "~16%"),
    ("Redwire (RDW)",              "~15%"),
    ("Planet Labs (PL)",           "~8%"),
    ("EchoStar (SATS)",            "~6%"),
    ("GlobalStar (GSAT)",          "~5%"),
    ("기타 3개 종목",              "~9%"),
]

NEWS_QUERIES = [
    ("🇺🇸 미국 증시",
     "US stock market S&P 500 NASDAQ Dow Jones today", "en-US", "US"),
    ("🇰🇷 한국 증시",
     "코스피 코스닥 한국 증시 오늘",                   "ko",    "KR"),
    ("💾 반도체 (삼성·SK하이닉스·NVIDIA·TSMC)",
     "Samsung SK Hynix NVIDIA TSMC semiconductor AI HBM chip", "en-US", "US"),
    ("🤖 로봇/모빌리티 (현대·LG전자)",
     "Hyundai LG Electronics robot humanoid EV news",  "en-US", "US"),
    ("🚀 우주테크/방산",
     "SpaceX aerospace defense rocket satellite stock", "en-US", "US"),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── HTML helpers ──────────────────────────────────────────────────────────────

def fmt_price(price, currency):
    if price is None:
        return "조회 실패"
    if currency == "KRW":
        return f"{price:,.0f}원"
    if currency == "USD":
        return f"${price:,.2f}"
    return f"{price:,.2f}"

def chg_cell(chg):
    if chg is None:
        return '<span style="color:#94a3b8;">━</span>'
    color = "#22c55e" if chg >= 0 else "#ef4444"
    arrow = "▲" if chg >= 0 else "▼"
    sign  = "+" if chg >= 0 else ""
    return f'<span style="color:{color};">{arrow} {sign}{chg:.2f}%</span>'

_TH = (
    '<tr style="border-bottom:1px solid #2d3748;">'
    '<th style="color:#94a3b8;text-align:left;padding:8px 6px;">종목</th>'
    '<th style="color:#94a3b8;text-align:right;padding:8px 6px;">현재가</th>'
    '<th style="color:#94a3b8;text-align:right;padding:8px 6px;">전일종가</th>'
    '<th style="color:#94a3b8;text-align:right;padding:8px 6px;">변동률</th>'
    '</tr>'
)

def trow(name, price_s, prev_s, chg):
    return (
        f'<tr style="border-bottom:1px solid #1e2535;">'
        f'<td style="color:#e2e8f0;padding:10px 6px;">{name}</td>'
        f'<td style="color:#e2e8f0;text-align:right;padding:10px 6px;">{price_s}</td>'
        f'<td style="color:#94a3b8;text-align:right;padding:10px 6px;">{prev_s}</td>'
        f'<td style="text-align:right;padding:10px 6px;">{chg_cell(chg)}</td>'
        f'</tr>'
    )

def card(title, rows, footnote=""):
    fn = (
        f'<p style="color:#475569;font-size:11px;margin:10px 0 0;">{footnote}</p>'
        if footnote else ""
    )
    return (
        '<div style="background:#1a1d2e;border-radius:12px;padding:20px 24px;'
        'margin-bottom:16px;border:1px solid #2d3748;">'
        f'<h2 style="color:#e2e8f0;margin:0 0 16px;font-size:15px;">{title}</h2>'
        '<table style="width:100%;border-collapse:collapse;font-size:14px;">'
        f'{_TH}{"".join(rows)}</table>{fn}</div>'
    )


# ── Step 1: Fetch stock prices ────────────────────────────────────────────────

def _fetch_one(ticker, name, currency):
    """Fetch price/prev_close via yfinance fast_info, fall back to history."""
    try:
        t  = yf.Ticker(ticker)
        fi = t.fast_info
        price = getattr(fi, "last_price", None)
        prev  = getattr(fi, "previous_close", None)

        if not price or not prev:
            hist = t.history(period="5d", interval="1d", auto_adjust=True)
            if not hist.empty:
                closes = hist["Close"].dropna()
                if len(closes) >= 2:
                    price, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
                elif len(closes) == 1:
                    price = float(closes.iloc[-1])

        chg = ((price - prev) / prev * 100) if (price and prev and prev != 0) else None
        log.info(f"  {name:22s}  {fmt_price(price, currency):>14s}  "
                 f"{f'{chg:+.2f}%' if chg is not None else '─':>8s}")
        return {"price": price, "prev": prev, "chg": chg}
    except Exception as exc:
        log.warning(f"  {name} ({ticker}) 실패: {exc}")
        return {"price": None, "prev": None, "chg": None}

def fetch_all_prices():
    log.info("── 주가 수집 시작")
    out = {}
    for ticker, name, currency, group in TICKERS:
        d = _fetch_one(ticker, name, currency)
        out[ticker] = {**d, "name": name, "currency": currency, "group": group}
        time.sleep(0.3)   # gentle rate limiting
    return out


# ── Step 3: Fetch news (Google News RSS) ──────────────────────────────────────

def _google_rss(query, lang, country):
    """Google News RSS → list of {title, source, link} dicts."""
    ceid = f"{country}:{lang.split('-')[0]}"
    url  = (
        "https://news.google.com/rss/search"
        f"?q={requests.utils.quote(query)}&hl={lang}&gl={country}&ceid={ceid}"
    )
    try:
        resp = requests.get(url, timeout=10,
                            headers={"User-Agent": "Mozilla/5.0 (compatible; StockBot/1.0)"})
        resp.raise_for_status()
        root  = ET.fromstring(resp.content)
        items = []
        for item in root.findall(".//item")[:3]:
            raw    = (item.findtext("title") or "").strip()
            link   = (item.findtext("link")  or "").strip()
            parts  = raw.rsplit(" - ", 1)
            title  = parts[0].strip()
            source = parts[1].strip() if len(parts) == 2 else "뉴스"
            items.append({"title": title, "source": source, "link": link})
        return items or [{"title": "최근 뉴스 없음", "source": "", "link": ""}]
    except Exception as exc:
        log.warning(f"  뉴스 수집 실패 '{query}': {exc}")
        return [{"title": "뉴스 조회 실패", "source": "", "link": ""}]

def fetch_all_news():
    log.info("── 뉴스 수집 시작")
    return {
        label: _google_rss(query, lang, country)
        for label, query, lang, country in NEWS_QUERIES
    }


# ── Step 4: Build HTML email ──────────────────────────────────────────────────

def build_html(prices, news):
    now  = datetime.now(KST)
    days = ["월","화","수","목","금","토","일"]
    date_str = f"{now.year}년 {now.month:02d}월 {now.day:02d}일 ({days[now.weekday()]})"

    def p(tk):
        d = prices[tk]
        return trow(
            html_lib.escape(d["name"]),
            fmt_price(d["price"], d["currency"]),
            fmt_price(d["prev"],  d["currency"]),
            d["chg"],
        )

    # ── Key badges ────────────────────────────────────────────────────────────
    def badge(tk, label):
        d   = prices[tk]
        c   = d["chg"]
        col = "#22c55e" if (c or 0) >= 0 else "#ef4444"
        arr = "▲" if (c or 0) >= 0 else "▼"
        txt = f"{arr} {abs(c):.2f}%" if c is not None else "━"
        return (
            f'<span style="background:#1e2535;border-radius:6px;'
            f'padding:6px 12px;font-size:13px;color:{col};">'
            f'{label} {txt}</span>'
        )

    usd = prices["KRW=X"]
    badges = (
        badge("^GSPC", "S&amp;P 500") +
        badge("^KS11", "KOSPI") +
        badge("^KQ11", "KOSDAQ") +
        f'<span style="background:#1e2535;border-radius:6px;padding:6px 12px;'
        f'font-size:13px;color:#e2e8f0;">USD/KRW {fmt_price(usd["price"],"")}</span>' +
        badge("^VIX", "VIX")
    )

    # ── ETF holdings table ────────────────────────────────────────────────────
    _th_hold = (
        '<tr style="border-bottom:1px solid #2d3748;">'
        '<th style="color:#94a3b8;text-align:left;padding:7px 5px;">종목명</th>'
        '<th style="color:#94a3b8;text-align:right;padding:7px 5px;">비중</th>'
        '<th style="color:#94a3b8;text-align:right;padding:7px 5px;">현재가</th>'
        '<th style="color:#94a3b8;text-align:right;padding:7px 5px;">등락률</th>'
        '</tr>'
    )
    hold_rows = "".join(
        f'<tr style="border-bottom:1px solid #1e2535;">'
        f'<td style="color:#e2e8f0;padding:7px 5px;font-size:13px;">{nm}</td>'
        f'<td style="color:#3b82f6;text-align:right;padding:7px 5px;font-size:13px;">{wt}</td>'
        f'<td style="color:#94a3b8;text-align:right;padding:7px 5px;font-size:13px;">-</td>'
        f'<td style="color:#94a3b8;text-align:right;padding:7px 5px;font-size:13px;">-</td>'
        f'</tr>'
        for nm, wt in TIGER_HOLDINGS
    )

    # ── News sections ─────────────────────────────────────────────────────────
    news_sections = ""
    for label, items in news.items():
        items_html = ""
        for item in items:
            t = item["title"]
            s = item["source"]
            if t and t not in ("최근 뉴스 없음", "뉴스 조회 실패"):
                items_html += (
                    f'<div style="margin-bottom:8px;padding:10px 12px;'
                    f'background:#0f1117;border-radius:8px;">'
                    f'<p style="color:#e2e8f0;font-size:13px;font-weight:600;margin:0 0 4px;">'
                    f'{html_lib.escape(t)}</p>'
                    f'<p style="color:#475569;font-size:11px;margin:0;">'
                    f'출처: {html_lib.escape(s)}</p></div>'
                )
            else:
                items_html += '<p style="color:#475569;font-size:12px;">최근 뉴스 없음</p>'
        news_sections += (
            f'<div style="margin-bottom:18px;">'
            f'<p style="color:#3b82f6;margin:0 0 8px;font-size:13px;font-weight:600;'
            f'border-left:3px solid #3b82f6;padding-left:8px;">{label}</p>'
            f'{items_html}</div>'
        )

    tiger = prices["465780.KS"]
    arkx  = prices["ARKX"]

    return f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="background:#0f1117;font-family:Arial,Helvetica,sans-serif;margin:0;padding:20px;">
<div style="max-width:680px;margin:0 auto;">

<!-- HEADER -->
<div style="background:linear-gradient(135deg,#1e3a5f,#1a1d2e);border-radius:12px;
     padding:24px 28px;margin-bottom:16px;border:1px solid #2d3748;">
  <h1 style="color:#3b82f6;margin:0;font-size:22px;">🌅 주식 일일 브리핑</h1>
  <p style="color:#94a3b8;margin:6px 0 0;font-size:14px;">{date_str} · KST 오전 7:00</p>
</div>

<!-- 핵심 지표 -->
<div style="background:#1a1d2e;border-radius:12px;padding:16px 24px;
     margin-bottom:16px;border:1px solid #2d3748;">
  <p style="color:#94a3b8;margin:0 0 10px;font-size:12px;">📊 오늘의 핵심 지표</p>
  <div style="display:flex;gap:8px;flex-wrap:wrap;">{badges}</div>
</div>

<!-- 미국 시장 -->
{card("🇺🇸 미국 시장", [p("^GSPC"), p("^IXIC"), p("^DJI"), p("^VIX")])}

<!-- 한국 시장 -->
{card("🇰🇷 한국 시장", [p("^KS11"), p("^KQ11"), p("KRW=X")])}

<!-- TIGER 우주테크 ETF -->
<div style="background:#1a1d2e;border-radius:12px;padding:20px 24px;
     margin-bottom:16px;border:1px solid #2d3748;">
  <h2 style="color:#e2e8f0;margin:0 0 4px;font-size:15px;">
    🚀 TIGER 우주테크TOP10 ETF (465780)
  </h2>
  <p style="color:#475569;margin:0 0 16px;font-size:12px;">
    미국 우주항공·민간우주·위성 TOP10 | Akros U.S. Space Tech Index<br>
    SpaceX 상장 시 D+2일 이내 25% 비중으로 자동 편입 예정
  </p>
  <table style="width:100%;border-collapse:collapse;font-size:14px;">
    {_TH}
    {trow("TIGER 우주테크TOP10 (465780)",
          fmt_price(tiger["price"], tiger["currency"]),
          fmt_price(tiger["prev"],  tiger["currency"]), tiger["chg"])}
    {trow("ARKX (ARK Space ETF)",
          fmt_price(arkx["price"], arkx["currency"]),
          fmt_price(arkx["prev"],  arkx["currency"]),  arkx["chg"])}
  </table>
  <hr style="border:none;border-top:1px solid #2d3748;margin:16px 0;">
  <p style="color:#94a3b8;font-size:13px;font-weight:600;margin:0 0 10px;">
    전체 구성 종목 (참고 — 분기마다 갱신 필요)
  </p>
  <table style="width:100%;border-collapse:collapse;font-size:13px;">
    {_th_hold}{hold_rows}
  </table>
</div>

<!-- 반도체 -->
{card("💾 반도체",
      [p("005930.KS"), p("000660.KS"), p("NVDA"), p("TSM")],
      "* NVIDIA: SK하이닉스 HBM 최대 고객")}

<!-- 로봇/모빌리티 -->
{card("🤖 로봇/모빌리티", [p("005380.KS"), p("066570.KS")])}

<!-- 뉴스 -->
<div style="background:#1a1d2e;border-radius:12px;padding:20px 24px;
     margin-bottom:16px;border:1px solid #2d3748;">
  <h2 style="color:#e2e8f0;margin:0 0 20px;font-size:15px;">📰 주요 뉴스 요약</h2>
  {news_sections}
</div>

<!-- FOOTER -->
<div style="text-align:center;padding:20px;color:#475569;font-size:11px;line-height:1.8;">
  ⚠️ 본 리포트는 자동 수집 데이터입니다. 투자 결정에 참고용으로만 활용하세요.<br>
  Powered by Claude Code · yfinance · Google News RSS · SendGrid
</div>

</div></body></html>"""


# ── Step 5: Send via SendGrid ─────────────────────────────────────────────────

def send_email(html_content, date_str):
    if not SENDGRID_API_KEY:
        log.error("SENDGRID_API_KEY 환경변수 없음 — 발송 건너뜀 (HTML은 로컬 저장됨)")
        return False

    payload = {
        "personalizations": [{"to": [{"email": TO_EMAIL, "name": "Sean"}]}],
        "from": {"email": FROM_EMAIL, "name": "주식 브리핑 봇"},
        "subject": f"[주식 브리핑] {date_str} 🌅",
        "content": [{"type": "text/html", "value": html_content}],
    }

    resp = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": f"Bearer {SENDGRID_API_KEY}",
            "Content-Type": "application/json",
        },
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=30,
    )

    if resp.status_code == 202:
        log.info("✅ 이메일 발송 완료 (HTTP 202)")
        return True
    log.error(f"❌ SendGrid 오류 {resp.status_code}: {resp.text[:400]}")
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    try:
        now  = datetime.now(KST)
        days = ["월","화","수","목","금","토","일"]
        date_str = f"{now.year}년 {now.month:02d}월 {now.day:02d}일 ({days[now.weekday()]})"

        log.info(f"═══ 주식 브리핑 {date_str} ═══")
        log.info(f"    TO_EMAIL  : {TO_EMAIL}")
        log.info(f"    API_KEY   : {'설정됨' if SENDGRID_API_KEY else '❌ 없음'}")

        log.info("── 주가 수집")
        prices = fetch_all_prices()

        log.info("── 뉴스 수집")
        news = fetch_all_news()

        log.info("── HTML 생성")
        html = build_html(prices, news)

        out_path = "briefing_latest.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        log.info(f"   HTML 저장됨: {out_path} ({len(html):,} chars)")

        log.info("── 이메일 발송")
        ok = send_email(html, date_str)

        failed = [tk for tk, d in prices.items() if d["price"] is None]
        log.info(f"═══ 완료 {'✅' if ok else '❌'} | 실패 티커: {failed or '없음'}")
        sys.exit(0 if ok else 1)

    except Exception as exc:
        log.error(f"치명적 오류: {exc}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
