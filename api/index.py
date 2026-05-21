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


def fetch(url, encoding='euc-kr', timeout=5):
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


def parse_naver_sise(url):
    try:
        table = fetch(url).select_one('table.type_2')
        if not table:
            return []
        stocks = []
        for row in table.select('tr'):
            tds = row.select('td')
            if len(tds) < 7:
                continue
            a = tds[1].select_one('a')
            tm = re.search(r'code=(\d+)', a.get('href', '')) if a else None
            if not tm:
                continue
            price = float(re.sub(r'[^\d]', '', tds[2].get_text(strip=True)) or 0)
            rate = float(re.sub(r'[^\d\.-]', '', tds[4].get_text(strip=True)) or 0)
            val = float(re.sub(r'[^\d]', '', tds[6].get_text(strip=True)) or 0)
            # 등락 방향 판별: 하락 클래스 확인
            rate_cell = tds[3]
            if rate_cell.select_one('span.nv01') or '하락' in rate_cell.get('class', []):
                rate = -abs(rate)
            stocks.append({'ticker': tm.group(1), 'name': a.get_text(strip=True),
                           'price': price, 'rate': rate, 'value': val})
        return stocks
    except Exception:
        return []


def get_market_cap(ticker):
    try:
        soup = fetch(f"https://finance.naver.com/item/main.naver?code={ticker}", 'utf-8', timeout=5)
        ms = soup.select_one('#_market_sum')
        if ms:
            market_sum = re.sub(r'\s+', ' ', ms.get_text(strip=True)).replace('조 ', '조')
            if not market_sum.endswith('억'):
                market_sum += '억'
        else:
            market_sum = "N/A"
        return market_sum
    except Exception:
        return "N/A"


def fetch_market_cap_wrapper(stock):
    return get_market_cap(stock['ticker'])


def get_top10_by_volume():
    urls = [
        "https://finance.naver.com/sise/sise_quant.naver?rankingType=deal_value&sosok=0",
        "https://finance.naver.com/sise/sise_quant.naver?rankingType=deal_value&sosok=1",
    ]
    with ThreadPoolExecutor(max_workers=3) as ex:
        date_f = ex.submit(get_last_trading_date)
        k_f = ex.submit(parse_naver_sise, urls[0])
        kd_f = ex.submit(parse_naver_sise, urls[1])
        final_date = date_f.result()
        combined = k_f.result() + kd_f.result()

    combined.sort(key=lambda x: x['value'], reverse=True)
    top10 = combined[:10]

    with ThreadPoolExecutor(max_workers=10) as ex:
        market_caps = list(ex.map(fetch_market_cap_wrapper, top10))

    for s, ms in zip(top10, market_caps):
        s['market_sum'] = ms

    return top10, final_date


def format_volume_report(stocks, date_str):
    total_value = sum(s['value'] for s in stocks)
    lines = [f"📋 *\\[{date_str}\\] 거래대금 상위 10위*\n"]

    for i, s in enumerate(stocks):
        rate = s['rate']
        rate_str = f"+{rate:.2f}%" if rate >= 0 else f"{rate:.2f}%"
        share = (s['value'] / total_value * 100) if total_value > 0 else 0
        val_str = f"{int(s['value']):,}"

        lines.append(
            f"{i + 1}위🔹 *{s['name']}* ({rate_str})\n"
            f"Cap {s['market_sum']}\n"
            f"거래대금 {val_str}백만\n"
            f"거래대금 차지율 : {share:.1f}%"
        )

    return "\n\n".join(lines)


def send_telegram(chat_id, text, markdown=False):
    payload = {"chat_id": chat_id, "text": text}
    if markdown:
        payload["parse_mode"] = "Markdown"
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload)


@app.route('/', methods=['POST', 'GET'])
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
