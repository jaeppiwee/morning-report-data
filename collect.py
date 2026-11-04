"""아침 리포트용 공개 데이터를 모아 data/latest.json에 저장합니다.

값을 추정하지 않습니다. 가져오지 못한 항목은 status를 "error"로 남깁니다.
"""
import datetime as dt
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; morning-report-data/1.0)"
TIMEOUT = 30

STOCKS = {
    "E1": "017940",
    "LS네트웍스": "000680",
    "LS증권": "078020",
    "LS": "006260",
    "LS ELECTRIC": "010120",
    "LS에코에너지": "229640",
    "LS마린솔루션": "060370",
    "가온전선": "000500",
    "LS머트리얼즈": "417200",
    "예스코홀딩스": "015360",
}
INDEXES = {"코스피": "KOSPI", "코스닥": "KOSDAQ"}
DISCLOSURE_CODES = set(STOCKS.values()) | {"018670"}  # SK가스 포함


def fetch(url, encoding="utf-8"):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        return res.read().decode(encoding, errors="replace")


def clean_error(exc):
    """공개 로그에 인증키가 남지 않도록 오류 문구를 정리합니다."""
    text = f"{type(exc).__name__}: {exc}"
    return re.sub(r"(crtfc_key|code)=[^&\s]+", r"\1=***", text)[:200]


def to_number(text):
    return float(str(text).replace(",", "").strip())


def parse_naver_chart(xml_text):
    """네이버 차트 XML을 오래된 날짜부터 [(YYYY-MM-DD, 종가)]로 바꿉니다."""
    rows = []
    for item in ET.fromstring(xml_text).iter("item"):
        parts = item.attrib["data"].split("|")
        day = parts[0]
        rows.append((f"{day[:4]}-{day[4:6]}-{day[6:8]}", to_number(parts[4])))
    return rows


def quote(symbol):
    url = (
        "https://fchart.stock.naver.com/sise.nhn?"
        f"symbol={symbol}&timeframe=day&count=5&requestType=0"
    )
    rows = parse_naver_chart(fetch(url, "euc-kr"))
    if len(rows) < 2:
        raise ValueError("시세 행이 2개 미만입니다")
    (_, prev_close), (date, close) = rows[-2], rows[-1]
    if close <= 0 or prev_close <= 0:
        raise ValueError("시세 값이 0 이하입니다")
    change = close - prev_close
    return {
        "status": "ok",
        "date": date,
        "close": close,
        "prev_close": prev_close,
        "change": round(change, 2),
        "change_pct": round(change / prev_close * 100, 2),
        "source": f"https://finance.naver.com/item/sise.naver?code={symbol}",
    }


def parse_smbs(xml_text):
    """서울외국환중개 XML의 label/value 쌍을 날짜순으로 반환합니다."""
    rows = re.findall(r"label=['\"]([\d.]+)['\"][^>]*?value=['\"]([\d,.]+)['\"]", xml_text)
    return [(label, to_number(value)) for label, value in rows]


def fx(today):
    start = (today - dt.timedelta(days=10)).isoformat()
    params = urllib.parse.quote(f"USD_{start}_{today.isoformat()}")
    url = f"http://www.smbs.biz/ExRate/StdExRate_xml.jsp?arr_value={params}"
    rows = parse_smbs(fetch(url, "euc-kr"))
    if not rows:
        raise ValueError("매매기준율 행을 찾지 못했습니다")
    label, value = rows[-1]
    result = {
        "status": "ok",
        "name": "USD/KRW 매매기준율",
        "notice_date": label,
        "value": value,
        "source": "http://www.smbs.biz/ExRate/StdExRate.jsp",
    }
    if len(rows) > 1:
        result["prev_value"] = rows[-2][1]
        result["change"] = round(value - rows[-2][1], 2)
    return result


def diesel():
    key = os.environ.get("OPINET_API_KEY")
    if not key:
        raise ValueError("OPINET_API_KEY가 설정되지 않았습니다")
    data = json.loads(fetch(f"https://www.opinet.co.kr/api/avgAllPrice.do?out=json&code={key}"))
    for row in data["RESULT"]["OIL"]:
        if row.get("PRODCD") == "D047":
            return {
                "status": "ok",
                "date": row["TRADE_DT"],
                "value": to_number(row["PRICE"]),
                "change": to_number(row["DIFF"]),
                "source": "https://www.opinet.co.kr",
            }
    raise ValueError("경유 항목을 찾지 못했습니다")


def parse_term_sofr(html):
    rows = re.findall(r"(\d{2}-\d{2}-\d{4}).{0,200}?(\d+\.\d{3,6})\s*(?:&nbsp;)?\s*%", html, re.S)
    seen = {}
    for day, value in rows:
        seen.setdefault(f"{day[6:]}-{day[:2]}-{day[3:5]}", float(value))
    return sorted(seen.items())


def term_sofr():
    url = "https://www.global-rates.com/en/interest-rates/cme-term-sofr/2/term-sofr-interest-3-months/"
    rows = parse_term_sofr(fetch(url))
    if len(rows) < 2:
        raise ValueError("Term SOFR 행을 찾지 못했습니다")
    (_, prev), (date, value) = rows[-2], rows[-1]
    return {
        "status": "ok",
        "name": "CME Term SOFR 3M",
        "date": date,
        "value": value,
        "prev_value": prev,
        "change": round(value - prev, 5),
        "source": url,
    }


def disclosures(today):
    key = os.environ.get("OPENDART_API_KEY")
    if not key:
        raise ValueError("OPENDART_API_KEY가 설정되지 않았습니다")
    start = (today - dt.timedelta(days=3)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    found, page, total_page = [], 1, 1
    while page <= min(total_page, 50):
        query = urllib.parse.urlencode(
            {"crtfc_key": key, "bgn_de": start, "end_de": end, "page_no": page, "page_count": 100}
        )
        data = json.loads(fetch(f"https://opendart.fss.or.kr/api/list.json?{query}"))
        if data.get("status") == "013":
            break
        if data.get("status") != "000":
            raise ValueError(f"OpenDART 응답 코드 {data.get('status')}")
        total_page = int(data.get("total_page", 1))
        for row in data.get("list", []):
            if row.get("stock_code") in DISCLOSURE_CODES:
                found.append(
                    {
                        "company": row.get("corp_name"),
                        "stock_code": row.get("stock_code"),
                        "report": (row.get("report_nm") or "").strip(),
                        "date": row.get("rcept_dt"),
                        "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={row.get('rcept_no')}",
                    }
                )
        page += 1
    return {"status": "ok", "from": start, "to": end, "items": found}


def safe(func, *args):
    try:
        return func(*args)
    except Exception as exc:  # 한 항목이 실패해도 나머지는 계속 수집합니다.
        print(f"[실패] {func.__name__}{args if func is quote else ''}: {clean_error(exc)}", file=sys.stderr)
        return {"status": "error", "error": clean_error(exc)}


def main():
    now = dt.datetime.now(KST)
    today = now.date()
    result = {
        "collected_at": now.isoformat(timespec="minutes"),
        "stocks": {name: safe(quote, code) for name, code in STOCKS.items()},
        "indexes": {name: safe(quote, code) for name, code in INDEXES.items()},
        "fx": safe(fx, today),
        "diesel": safe(diesel),
        "term_sofr_3m": safe(term_sofr),
        "disclosures": safe(disclosures, today),
    }
    path = Path("data/latest.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v.get("status") for k, v in result.items() if isinstance(v, dict) and "status" in v}, ensure_ascii=False))


if __name__ == "__main__":
    main()
