#!/usr/bin/env python3
"""번개시황뉴스 v2

실행 시점 기준 최근 N분(30 또는 60)의 시세와 뉴스로 지수 등락 원인과 경제 지표를 정리한다.
실행: WINDOW_MIN=30 python3 brief.py
출력: output/번개시황뉴스(YYYYMMDD-HH-MM).md, output/latest.md, output/history.json

환경변수
- WINDOW_MIN          : 30 또는 60 (기본 30)
- KIWOOM_APPKEY       : 키움 REST 앱키 (국내 지수 실시간). 없으면 Yahoo 지연 시세로 대체
- KIWOOM_SECRETKEY    : 키움 REST 시크릿키
- NAVER_CLIENT_ID     : 네이버 검색 API 클라이언트 ID (국내 뉴스). 없으면 구글 뉴스만 사용
- NAVER_CLIENT_SECRET : 네이버 검색 API 시크릿
- ANTHROPIC_API_KEY   : 원인 요약. 없으면 수치만 출력
- GITHUB_STEP_SUMMARY : 있으면 실행 화면(Summary)에 결과를 표시
"""
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

VERSION = "v2"
KST = dt.timezone(dt.timedelta(hours=9))
MODEL = "claude-haiku-5-5"
WINDOW_MIN = int(os.environ.get("WINDOW_MIN", "30"))
MOVE_DAY_THRESHOLD = 1.0      # 전일 대비 원인 요약 기준(%)
MOVE_WINDOW_THRESHOLD = 0.3   # 분석 구간 변화 원인 요약 기준(%)
NEWS_PER_INDEX = 5
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
HIST_PATH = OUTPUT_DIR / "history.json"
KIWOOM_BASE = os.environ.get("KIWOOM_BASE", "https://api.kiwoom.com")
UA = {"User-Agent": "Mozilla/5.0"}

# 국내: kiwoom=(시장코드, 업종코드) → 키움 실시간, 없으면 Yahoo 지연 시세
INDEXES = [
    {"name": "S&P 500", "region": "미국", "yahoo": "^GSPC", "q": "S&P 500 stock market", "lang": "en"},
    {"name": "다우존스", "region": "미국", "yahoo": "^DJI", "q": "Dow Jones stock market", "lang": "en"},
    {"name": "나스닥 종합", "region": "미국", "yahoo": "^IXIC", "q": "Nasdaq stock market", "lang": "en"},
    {"name": "코스피", "region": "국내", "yahoo": "^KS11", "q": "코스피 증시", "lang": "ko", "kiwoom": ("0", "001")},
    {"name": "코스닥", "region": "국내", "yahoo": "^KQ11", "q": "코스닥 증시", "lang": "ko", "kiwoom": ("1", "101")},
]

# 경제 지표 (무료 소스 = Yahoo 지연 시세)
ECON = [
    {"name": "S&P500 VIX", "yahoo": "^VIX", "kind": "price"},
    {"name": "미국 국채 10년물 이자율", "yahoo": "^TNX", "kind": "yield"},
    {"name": "텍사스 원유 가격 (WTI 선물)", "yahoo": "CL=F", "kind": "price"},
    {"name": "원화환율 (원/달러)", "yahoo": "KRW=X", "kind": "price"},
]


# ---------- 시세 ----------

def yahoo_series(ticker):
    """Yahoo 지연 시세. 전일 종가 대비 등락률과 최근 WINDOW_MIN분 등락률을 구한다."""
    import yfinance as yf
    t = yf.Ticker(ticker)
    daily = t.history(period="5d", interval="1d")
    if len(daily) < 2:
        raise RuntimeError("일봉 데이터 부족")
    prev = float(daily["Close"].iloc[-2])
    intra = t.history(period="2d", interval="5m")
    if len(intra) == 0:
        raise RuntimeError("분봉 데이터 없음")
    last = float(intra["Close"].iloc[-1])
    cutoff = intra.index[-1] - dt.timedelta(minutes=WINDOW_MIN)
    past = intra[intra.index <= cutoff]
    base = float(past["Close"].iloc[-1]) if len(past) else float(intra["Close"].iloc[0])
    return {
        "last": last,
        "prev": prev,
        "day_pct": (last - prev) / prev * 100,
        "win_pct": (last - base) / base * 100,
        "src": "Yahoo(지연 가능)",
    }


def _post(url, headers, body):
    data = json.dumps(body).encode("utf-8")
    h = {**UA, "Content-Type": "application/json;charset=UTF-8", **headers}
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _num(v):
    """키움 응답의 '+1234', '-0.28' 같은 문자열을 실수로 바꾼다."""
    try:
        return float(str(v).replace(",", "").replace("+", ""))
    except (TypeError, ValueError):
        return None


def kiwoom_token():
    res = _post(f"{KIWOOM_BASE}/oauth2/token", {}, {
        "grant_type": "client_credentials",
        "appkey": os.environ["KIWOOM_APPKEY"],
        "secretkey": os.environ["KIWOOM_SECRETKEY"],
    })
    if "token" not in res:
        raise RuntimeError(f"키움 토큰 발급 실패: {res.get('return_msg', res)}")
    return res["token"]


def kiwoom_quote(mkt, code, token):
    """키움 업종지수 현재가(ka20001). 응답 필드명은 공식 명세로 확인이 필요하다."""
    res = _post(f"{KIWOOM_BASE}/api/dostk/sect",
                {"api-id": "ka20001", "authorization": f"Bearer {token}"},
                {"mrkt_tp": mkt, "inds_cd": code})
    if res.get("return_code", 0) != 0:
        raise RuntimeError(f"키움 지수 조회 실패: {res.get('return_msg')}")
    last = abs(_num(res.get("cur_prc")))
    pred = _num(res.get("pred_pre")) or 0.0
    day_pct = _num(res.get("flu_rt"))
    prev = last - pred
    return {"last": last, "prev": prev, "day_pct": day_pct if day_pct is not None else (last - prev) / prev * 100,
            "src": "키움 REST(실시간)"}


# ---------- 구간 이력 (키움 실시간 값의 구간 등락 계산용) ----------

def load_hist():
    return json.loads(HIST_PATH.read_text(encoding="utf-8")) if HIST_PATH.exists() else {}


def window_base(hist, name, now_ts):
    """구간 안에서 처음 기록된 값. 구간 안에 기록이 없으면 None."""
    cutoff = now_ts - WINDOW_MIN * 60
    pts = [p for p in hist.get(name, []) if p[0] >= cutoff]
    return pts[0][1] if pts else None


def record(hist, name, now_ts, value):
    keep_after = now_ts - 3 * 24 * 3600
    pts = [p for p in hist.get(name, []) if p[0] >= keep_after]
    pts.append([now_ts, value])
    hist[name] = pts


def save_hist(hist):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    HIST_PATH.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")


# ---------- 뉴스 ----------

def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def naver_news(query):
    cid, sec = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not sec:
        return []
    url = "https://openapi.naver.com/v1/search/news.json?" + urllib.parse.urlencode(
        {"query": query, "display": 20, "sort": "date"})
    req = urllib.request.Request(url, headers={**UA, "X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    out = []
    for it in data.get("items", []):
        try:
            pub = parsedate_to_datetime(it["pubDate"])
        except (KeyError, TypeError, ValueError):
            continue
        src = urllib.parse.urlparse(it.get("originallink") or it.get("link") or "").netloc
        out.append({"title": strip_html(it.get("title")), "pub": pub, "source": src or "네이버뉴스"})
    return out


def google_news(query, lang):
    if lang == "en":
        params = {"hl": "en-US", "gl": "US", "ceid": "US:en"}
    else:
        params = {"hl": "ko", "gl": "KR", "ceid": "KR:ko"}
    params["q"] = f"{query} when:1h"
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        root = ET.fromstring(r.read())
    out = []
    for it in root.iter("item"):
        try:
            pub = parsedate_to_datetime((it.findtext("pubDate") or "").strip())
        except (TypeError, ValueError):
            continue
        out.append({"title": (it.findtext("title") or "").strip(), "pub": pub,
                    "source": (it.findtext("source") or "구글뉴스").strip()})
    return out


def recent_news(idx, cutoff):
    """구간(cutoff 이후) 기사만 모아 중복 제거 후 최신순으로 자른다."""
    items = []
    for fetch in ((lambda: naver_news(idx["q"])) if idx["lang"] == "ko" else (lambda: []),
                  lambda: google_news(idx["q"], idx["lang"])):
        try:
            items += fetch()
        except Exception as e:
            print(f"[경고] {idx['name']} 뉴스 조회 실패: {e}", file=sys.stderr)
    fresh = [i for i in items if i["pub"] >= cutoff]
    seen, uniq = set(), []
    for i in sorted(fresh, key=lambda x: x["pub"], reverse=True):
        key = i["title"][:30]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(i)
    return uniq[:NEWS_PER_INDEX]


# ---------- 요약 ----------

def fmt_win(v):
    return f"{v:+.2f}%" if v is not None else "구간 이력 부족"


def index_lines(rows):
    lines = []
    for r in rows:
        if "error" in r:
            continue
        lines.append(f"- {r['name']}({r['region']}) {r['last']:,.2f}, 전일 대비 {r['day_pct']:+.2f}%, "
                     f"최근 {WINDOW_MIN}분 {fmt_win(r.get('win_pct'))} [{r['src']}]")
    return lines


def is_target(r):
    if "error" in r:
        return False
    return (abs(r["day_pct"]) >= MOVE_DAY_THRESHOLD
            or abs(r.get("win_pct") or 0) >= MOVE_WINDOW_THRESHOLD)


SYSTEM = (
    "너는 증시 시황 요약 도우미다. 주어진 수치와 뉴스 제목만 근거로 한국어 마크다운을 쓴다. "
    "형식: '### 1-1. 미국 증시 추세 요약'과 '### 1-2. 국내 증시 추세 요약' 제목 아래에 "
    "지수별 수치 한 줄 뒤 '상승 요인 1/2/3' 또는 '하락 요인 1/2/3'을 목록으로 쓴다. "
    "각 원인 줄 끝에 매체명을 괄호로 붙인다. 근거 뉴스가 없으면 '확인 불가'라고만 쓴다. "
    "수치는 주어진 값을 그대로 쓴다. 추측, 투자 권유, 매수·매도 의견은 쓰지 않는다."
)


def summarize(rows):
    numbers = "\n".join(index_lines(rows))
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    targets = [r for r in rows if is_target(r)]
    if not api_key:
        return "ANTHROPIC_API_KEY가 없어 원인 요약을 생략했습니다. 지수 수치만 표시합니다.\n\n" + numbers
    if not targets:
        return "기준을 넘은 지수가 없어 원인 요약을 생략했습니다.\n\n" + numbers
    from anthropic import Anthropic
    lines = [numbers, "", "[뉴스 근거]"]
    for r in targets:
        lines.append(f"## {r['name']}")
        for n in r.get("news", []):
            lines.append(f"- {n['pub'].astimezone(KST):%H:%M} {n['title']} [{n['source']}]")
        if not r.get("news"):
            lines.append("- (구간 내 뉴스 없음)")
    client = Anthropic(api_key=api_key)
    msg = client.messages.create(model=MODEL, max_tokens=1000, system=SYSTEM,
                                 messages=[{"role": "user", "content": "\n".join(lines)}])
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


def econ_table(econ):
    out = ["| 지표명 | 현재가 | 등락 | 등락률 | 출처 |", "|---|---|---|---|---|"]
    for e in econ:
        if "error" in e:
            out.append(f"| {e['name']} | - | - | 조회 실패 | - |")
            continue
        diff = e["last"] - e["prev"]
        if e["kind"] == "yield":
            out.append(f"| {e['name']} | {e['last']:.3f}% | {diff:+.3f}%p | {e['day_pct']:+.2f}% | {e['src']} |")
        else:
            out.append(f"| {e['name']} | {e['last']:,.2f} | {diff:+,.2f} | {e['day_pct']:+.2f}% | {e['src']} |")
    return "\n".join(out)


def build_markdown(now, body, econ):
    lines = [
        f"# 번개시황뉴스({now:%Y%m%d-%H-%M})",
        f"기준 시각: {now:%Y-%m-%d %H:%M} KST · 분석 구간: 최근 {WINDOW_MIN}분 · 버전 {VERSION}",
        "",
        "## 1. 증시 추세 요약",
        body,
        "",
        "## 2. 주요 경제 지표",
        econ_table(econ),
        "",
    ]
    return "\n".join(lines)


def main():
    if WINDOW_MIN not in (30, 60):
        print("WINDOW_MIN은 30 또는 60이어야 합니다.", file=sys.stderr)
        sys.exit(2)
    now = dt.datetime.now(KST)
    now_ts = now.timestamp()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=WINDOW_MIN)
    hist = load_hist()

    token = None
    if os.environ.get("KIWOOM_APPKEY") and os.environ.get("KIWOOM_SECRETKEY"):
        try:
            token = kiwoom_token()
        except Exception as e:
            print(f"[경고] 키움 토큰 실패, Yahoo 지연 시세로 대체: {e}", file=sys.stderr)

    rows = []
    for idx in INDEXES:
        row = {"name": idx["name"], "region": idx["region"]}
        try:
            if token and "kiwoom" in idx:
                row.update(kiwoom_quote(*idx["kiwoom"], token))
                base = window_base(hist, idx["name"], now_ts)
                row["win_pct"] = (row["last"] - base) / base * 100 if base else None
                record(hist, idx["name"], now_ts, row["last"])
            else:
                row.update(yahoo_series(idx["yahoo"]))
        except Exception as e:
            row["error"] = str(e)
        row["news"] = recent_news(idx, cutoff) if is_target(row) else []
        rows.append(row)

    if all("error" in r for r in rows):
        print("모든 지수 조회에 실패했습니다.", file=sys.stderr)
        sys.exit(1)

    econ = []
    for e in ECON:
        try:
            econ.append({"name": e["name"], "kind": e["kind"], **yahoo_series(e["yahoo"])})
        except Exception as ex:
            econ.append({"name": e["name"], "kind": e["kind"], "error": str(ex)})

    body = summarize(rows)
    md = build_markdown(now, body, econ)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / f"번개시황뉴스({now:%Y%m%d-%H-%M}).md").write_text(md, encoding="utf-8")
    (OUTPUT_DIR / "latest.md").write_text(md, encoding="utf-8")
    save_hist(hist)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(md + "\n")
    print(md)


if __name__ == "__main__":
    main()
