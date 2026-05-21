from flask import Flask, request, jsonify
import requests, os, re, datetime
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}
DAYS = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
PROCESSED_UPDATES = []


def fetch(url, encoding='euc-kr', timeout=8):
    res = requests.get(url, headers=HEADERS, timeout=timeout)
    res.encoding = encoding
    return BeautifulSoup(res.text, 'html.parser')


def get_last_trading_date():
    try:
        text = fetch("https://finance.naver.com/item/main.naver?code=005930", 'utf-8').select_one('em.date').get_text(strip=True)
        y, mo, d = map(int, re.search(r'(\d{4})\.(\d{2})\.(\d{2})', text).groups())
        dt = datetime.date(y, mo, d)
        return f"{y}년 {mo:02d}월 {d:02d}일 {DAYS[dt.weekday()]}"
    except Exception:
        t = datetime.date.today()
        return f"{t.year}년 {t.month:02d}월 {t.day:02d}일 {DAYS[t.weekday()]}"


def format_deal_value(val_baekman):
    """백만원 단위 거래대금을 '10조6600억' 형식으로 변환. 1조 미만이면 조 생략."""
    eok = round(val_baekman / 10000) * 100  # 100억 단위 반올림
    jo = eok // 10000
    rem = eok % 10000
    if jo > 0:
        return f"{jo}조{rem}억"
    return f"{rem}억"


def format_market_cap(val_eok):
    v = int(val_eok)
    jo = v // 10000
    eok = v % 10000
    if jo > 0 and eok > 0:
        return f"{jo:,}조{eok:,}억"
    elif jo > 0:
        return f"{jo:,}조"
    return f"{eok:,}억"


def parse_quant_page(url):
    """
    sise_quant 페이지 파싱 (거래량 기준 정렬).
    tds: [2]현재가 [4]등락률 [6]거래대금(실제) [9]시가총액(억)
    """
    try:
        table = fetch(url).select_one('table.type_2')
        if not table:
            return []
        stocks = []
        for row in table.select('tr'):
            tds = row.select('td')
            if len(tds) < 10:
                continue
            a = tds[1].select_one('a')
            tm = re.search(r'code=(\d+)', a.get('href', '')) if a else None
            if not tm:
                continue
            price = float(re.sub(r'[^\d]', '', tds[2].get_text(strip=True)) or 0)
            rate = float(re.sub(r'[^\d\.-]', '', tds[4].get_text(strip=True)) or 0)
            if '하락' in tds[3].get_text():
                rate = -abs(rate)
            val = float(re.sub(r'[^\d]', '', tds[6].get_text(strip=True)) or 0)
            cap_raw = float(re.sub(r'[^\d]', '', tds[9].get_text(strip=True)) or 0)
            stocks.append({
                'ticker': tm.group(1), 'name': a.get_text(strip=True),
                'price': price, 'rate': rate, 'value': val,
                'market_sum': format_market_cap(cap_raw) if cap_raw else "N/A",
                'actual': True,
            })
        return stocks
    except Exception:
        return []


def parse_market_sum_page(url):
    """
    sise_market_sum 페이지 파싱 (시가총액 기준 정렬).
    tds: [2]현재가 [4]등락률 [6]시가총액(억) [9]거래량
    거래대금 = price × volume / 1,000,000 (백만원 단위 추정값)
    """
    try:
        table = fetch(url).select_one('table.type_2')
        if not table:
            return []
        stocks = []
        for row in table.select('tr'):
            tds = row.select('td')
            if len(tds) < 10:
                continue
            a = tds[1].select_one('a')
            tm = re.search(r'code=(\d+)', a.get('href', '')) if a else None
            if not tm:
                continue
            price = float(re.sub(r'[^\d]', '', tds[2].get_text(strip=True)) or 0)
            rate = float(re.sub(r'[^\d\.-]', '', tds[4].get_text(strip=True)) or 0)
            if '하락' in tds[3].get_text():
                rate = -abs(rate)
            cap_raw = float(re.sub(r'[^\d]', '', tds[6].get_text(strip=True)) or 0)
            volume = float(re.sub(r'[^\d]', '', tds[9].get_text(strip=True)) or 0)
            est_val = price * volume / 1_000_000
            stocks.append({
                'ticker': tm.group(1), 'name': a.get_text(strip=True),
                'price': price, 'rate': rate, 'value': est_val,
                'market_sum': format_market_cap(cap_raw) if cap_raw else "N/A",
                'actual': False,
            })
        return stocks
    except Exception:
        return []


def get_actual_deal_value(ticker):
    """개별 종목 페이지의 <dd>거래대금 N백만</dd> 에서 실제 거래대금 추출"""
    try:
        soup = fetch(f"https://finance.naver.com/item/main.naver?code={ticker}", 'utf-8', timeout=8)
        for dd in soup.select('dd'):
            text = dd.get_text(strip=True)
            if text.startswith('거래대금'):
                val = float(re.sub(r'[^\d]', '', text) or 0)
                return val
    except Exception:
        pass
    return None


def get_top10_by_volume():
    quant_urls = (
        [f"https://finance.naver.com/sise/sise_quant.naver?sosok=0&page={p}" for p in range(1, 3)] +
        [f"https://finance.naver.com/sise/sise_quant.naver?sosok=1&page={p}" for p in range(1, 3)]
    )
    msum_urls = [
        "https://finance.naver.com/sise/sise_market_sum.naver?sosok=0&page=1",
        "https://finance.naver.com/sise/sise_market_sum.naver?sosok=0&page=2",
        "https://finance.naver.com/sise/sise_market_sum.naver?sosok=1&page=1",
        "https://finance.naver.com/sise/sise_market_sum.naver?sosok=1&page=2",
    ]

    with ThreadPoolExecutor(max_workers=12) as ex:
        date_f = ex.submit(get_last_trading_date)
        quant_results = list(ex.map(parse_quant_page, quant_urls))
        msum_results = list(ex.map(parse_market_sum_page, msum_urls))
        final_date = date_f.result()

    # sise_quant(실제 거래대금) 우선, sise_market_sum(추정값)으로 보완
    stock_map = {}
    for stocks in msum_results:
        for s in stocks:
            if s['ticker'] not in stock_map:
                stock_map[s['ticker']] = s
    for stocks in quant_results:
        for s in stocks:
            stock_map[s['ticker']] = s  # 실제값으로 덮어쓰기

    combined = sorted(stock_map.values(), key=lambda x: x['value'], reverse=True)
    candidates = combined[:15]

    # 추정값만 있는 종목의 실제 거래대금 보정
    needs_actual = [s for s in candidates if not s['actual']]
    if needs_actual:
        with ThreadPoolExecutor(max_workers=len(needs_actual)) as ex:
            actuals = list(ex.map(lambda s: get_actual_deal_value(s['ticker']), needs_actual))
        for s, actual_val in zip(needs_actual, actuals):
            if actual_val:
                s['value'] = actual_val
                s['actual'] = True

    candidates.sort(key=lambda x: x['value'], reverse=True)
    return candidates[:10], final_date


def format_volume_report(stocks, date_str):
    total_value = sum(s['value'] for s in stocks)
    lines = [f"📋 *\\[{date_str}\\] 거래대금 상위 10위*\n"]

    for i, s in enumerate(stocks):
        rate = s['rate']
        rate_str = f"+{rate:.2f}%" if rate >= 0 else f"{rate:.2f}%"
        share = (s['value'] / total_value * 100) if total_value > 0 else 0
        lines.append(
            f"{i + 1}위🔹 *{s['name']}* ({rate_str})\n"
            f"Cap {s['market_sum']}\n"
            f"거래대금 {format_deal_value(s['value'])}\n"
            f"거래대금 차지율 : {share:.1f}%"
        )

    return "\n\n".join(lines)


def send_telegram(chat_id, text, markdown=False):
    payload = {"chat_id": chat_id, "text": text}
    if markdown:
        payload["parse_mode"] = "Markdown"
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload)


@app.route('/', methods=['POST', 'GET'])
@app.route('/api', methods=['POST', 'GET'])
@app.route('/api/', methods=['POST', 'GET'])
def telegram_webhook():
    if request.method == 'GET':
        return "Stock Volume Screener is running!"

    update = request.get_json() or {}
    uid = update.get("update_id")
    if uid:
        if uid in PROCESSED_UPDATES:
            return jsonify({"status": "ignored_duplicate"})
        PROCESSED_UPDATES.append(uid)
        if len(PROCESSED_UPDATES) > 50:
            PROCESSED_UPDATES.pop(0)

    msg = update.get("message", {})
    text = msg.get("text")
    chat_id = msg.get("chat", {}).get("id")

    if text == '/check' and chat_id:
        send_telegram(chat_id, "⏳ 분석 중...")
        stocks, date_str = get_top10_by_volume()
        body = format_volume_report(stocks, date_str)
        send_telegram(chat_id, body, markdown=True)

    return jsonify({"status": "success"})
