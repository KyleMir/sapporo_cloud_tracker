"""
24시간 무인 클라우드 항공권 트래커 (GitHub Actions + Google Flights + ntfy iPhone 푸시 알림)
- 해외 클라우드 IP 차단 없는 Google Flights로 실시간 직항 운임(4인 기준) 조회
- iPhone 알림 예매 버튼은 보유 카드 실결제 기준(&cardBenefit=0) 네이버 항공권 링크로 직접 연결
- 1/6(수)~1/10(일) 4박5일 최우선 감시 (1/7 목 출발 3박4일 비교 참고 포함)
"""
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
import requests

KST = timezone(timedelta(hours=9))
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloud_state.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "kylenoti_CE240803")

# Google Flights 조회 URL (성인 4인, 편도, 직항, 한국어/KRW)
GF_URLS = {
    "dep_0106": "https://www.google.com/travel/flights?q=Flights%20from%20ICN%20to%20CTS%20on%202027-01-06%20oneway%20nonstop%204%20adults&hl=ko&curr=KRW",
    "ret_0110": "https://www.google.com/travel/flights?q=Flights%20from%20HKD%20to%20ICN%20on%202027-01-10%20oneway%20nonstop%204%20adults&hl=ko&curr=KRW",
    "dep_0107": "https://www.google.com/travel/flights?q=Flights%20from%20ICN%20to%20CTS%20on%202027-01-07%20oneway%20nonstop%204%20adults&hl=ko&curr=KRW",
}

# 예매용 네이버 항공권 직통 링크 (보유 카드 실결제 기준: &cardBenefit=0)
NAVER_URLS = {
    "dep_0106": "https://flight.naver.com/flights/international/ICN-CTS-20270106?adult=4&isDirect=true&fareType=Y&cardBenefit=0",
    "ret_0110": "https://flight.naver.com/flights/international/HKD-ICN-20270110?adult=4&isDirect=true&fareType=Y&cardBenefit=0",
    "multi_0106": "https://flight.naver.com/flights/international/ICN-CTS-20270106/HKD-ICN-20270110?adult=4&isDirect=true&fareType=Y&cardBenefit=0",
}

# 4인 부대교통비 고정 단가 (2026년 공식 기준)
# 대구 3인 왕복(우등버스/KTX 43,500*2*3=261,000) + 서울 1인 왕복(17,000*2=34,000)
# + 일본 JR 특급 호쿠토(삿포로->하코다테 96,000*4=384,000) + 기타 연결 49,200 = 728,200원
TRANSPORT_4P_TOTAL = 728200

# 알림 임계값 (1인 기준)
MIN_DROP_KRW = 3000
MIN_HIKE_KRW = 5000


def kr_time_to_24h(t_str: str) -> str:
    """'오전 7:40' 또는 '오후 2:10'을 '07:40' / '14:10' 형식으로 변환합니다."""
    m = re.match(r"^(오전|오후)\s*(\d{1,2}):(\d{2})$", t_str.strip())
    if not m:
        m2 = re.match(r"^(\d{1,2}):(\d{2})$", t_str.strip())
        if m2:
            return f"{int(m2.group(1)):02d}:{m2.group(2)}"
        return t_str.strip()
    ampm, hh, mm = m.group(1), int(m.group(2)), m.group(3)
    if ampm == "오후" and hh < 12:
        hh += 12
    elif ampm == "오전" and hh == 12:
        hh = 0
    return f"{hh:02d}:{mm}"


def parse_google_flights_from_text(body_text: str) -> dict:
    """Google Flights body.innerText에서 직항 편도 항공편(4인 총액 및 1인 단가)을 추출합니다."""
    if not body_text or len(body_text.strip()) < 200:
        return {"status": "error", "reason": "empty_or_blocked", "flights": []}

    if (
        "일치하는 검색결과가 없습니다" in body_text
        or "검색결과가 없습니다" in body_text
        or "No results returned" in body_text
    ):
        return {"status": "soldout", "flights": []}

    if "항공편 검색" not in body_text and "Flights" not in body_text:
        snippet = " | ".join([l.strip() for l in body_text.splitlines() if l.strip()][:15])[:250]
        return {"status": "error", "reason": f"unexpected_page: {snippet}", "flights": []}

    lines = [l.strip() for l in body_text.splitlines() if l.strip()]
    flights = []
    seen = set()

    time_re = re.compile(r"^(?:(?:오전|오후)\s*)?\d{1,2}:\d{2}$")
    route_re = re.compile(r"^[A-Z]{3}[–-][A-Z]{3}$")
    price_re = re.compile(r"^₩\s*([\d,]+)$")

    is_4_adults = ("성인 4명" in body_text) or ("\n4\n" in body_text) or ("4 adults" in body_text)

    for i, line in enumerate(lines):
        if not time_re.match(line):
            continue
        # 다음 줄이 '–' 이고 그 다음 줄이 도착 시간인지 확인
        if i + 6 >= len(lines):
            continue
        if lines[i + 1] not in ("–", "-") or not time_re.match(lines[i + 2]):
            continue

        dep_time = kr_time_to_24h(line)
        airline = lines[i + 3]

        # 이후 6줄 이내에서 직항(Nonstop) 여부 및 요금 확인
        is_direct = False
        has_layover = False
        route_str = ""
        price_raw = None

        for k in range(i + 4, min(len(lines), i + 12)):
            cur = lines[k]
            if time_re.match(cur):
                break
            if route_re.match(cur):
                route_str = cur
            if cur == "직항" or cur == "Nonstop":
                is_direct = True
            if "경유" in cur or "stop" in cur.lower():
                has_layover = True
            m_p = price_re.match(cur)
            if m_p:
                price_raw = int(m_p.group(1).replace(",", ""))
                break

        if is_direct and not has_layover and price_raw and price_raw > 100000:
            price_4p = price_raw if is_4_adults else price_raw * 4
            price_1p = round(price_4p / 4)
            key = (airline, dep_time, route_str, price_4p)
            if key not in seen:
                seen.add(key)
                flights.append({
                    "airline": airline,
                    "dep_time": dep_time,
                    "route": route_str,
                    "price": price_1p,
                    "price_4p": price_4p,
                })

    if not flights:
        snippet = " | ".join(lines[:20])[:280]
        return {"status": "error", "reason": f"no_flight_cards_parsed: {snippet}", "flights": []}

    flights.sort(key=lambda x: x["price_4p"])
    return {"status": "ok", "flights": flights}


async def fetch_all_routes() -> dict:
    """Playwright 헤드리스 크롬으로 Google Flights 직항 노선을 조회합니다."""
    from playwright.async_api import async_playwright
    results = {}
    async with async_playwright() as p:
        launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True, args=launch_args)
        except Exception:
            browser = await p.chromium.launch(headless=True, args=launch_args)

        context = await browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1440, "height": 1000},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        for key in ["dep_0106", "ret_0110", "dep_0107"]:
            url = GF_URLS[key]
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=35000)
                # 혹시 Google 동의(Consent) 창이 뜨면 수락 처리
                try:
                    consent_btn = page.locator("button:has-text('모두 수락'), button:has-text('Accept all')").first
                    if await consent_btn.is_visible(timeout=2000):
                        await consent_btn.click()
                        await asyncio.sleep(2.0)
                except Exception:
                    pass

                # 검색 결과 요금('₩') 또는 '검색결과가 없습니다'가 뜰 때까지 최대 20초 대기
                try:
                    await page.wait_for_function(
                        """() => {
                            const t = document.body ? document.body.innerText : '';
                            return (t.includes('₩') && (t.includes('직항') || t.includes('Nonstop')))
                                || t.includes('검색결과가 없습니다')
                                || t.includes('No results returned');
                        }""",
                        timeout=20000,
                    )
                    await asyncio.sleep(2.0)
                except Exception:
                    await asyncio.sleep(2.5)

                body_text = await page.inner_text("body")
                res = parse_google_flights_from_text(body_text)
                if res["status"] != "ok":
                    print(f"[{key}] DEBUG BODY ({len(body_text)} chars):\n{body_text[:1500]}")
                results[key] = res
            except Exception as exc:
                results[key] = {"status": "error", "reason": str(exc)[:150], "flights": []}
            finally:
                await page.close()

        await browser.close()
    return results


def send_ntfy(title: str, message: str, priority: str = "high", tags: list = None, actions: list = None):
    """ntfy.sh JSON API를 통해 아이폰으로 푸시 알림을 발송합니다."""
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": 4 if priority == "high" else (5 if priority == "urgent" else 3),
        "tags": tags or ["airplane"],
        "click": NAVER_URLS["multi_0106"],
        "actions": actions or [],
    }
    resp = requests.post("https://ntfy.sh", json=payload, timeout=15)
    resp.raise_for_status()
    print(f"[ntfy] Sent: {title} (HTTP {resp.status_code})")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def pick_morning_best(flights: list):
    morning = [f for f in flights if f["dep_time"] <= "12:00"]
    return morning[0] if morning else None


def build_summary_text(dep_res: dict, ret_res: dict, dep07_res: dict) -> str:
    dep_list = dep_res.get("flights", [])
    ret_list = ret_res.get("flights", [])
    dep07_list = dep07_res.get("flights", [])

    lines = ["[Google Flights 순수 직항 실시간 (네이버 실결제 기준 동일)]"]
    if dep_list and ret_list:
        dep_low = dep_list[0]
        dep_morn = pick_morning_best(dep_list)
        ret_low = ret_list[0]

        rt_low_4p = dep_low["price_4p"] + ret_low["price_4p"]
        rt_low_1p = round(rt_low_4p / 4)
        grand_low_4p = rt_low_4p + TRANSPORT_4P_TOTAL

        lines.append(
            f"■ 1/6 가는편 최저: {dep_low['airline']}({dep_low['dep_time']}) 1인 {dep_low['price']:,}원 (4인 {dep_low['price_4p']:,}원)"
        )
        if dep_morn and (dep_morn["airline"] != dep_low["airline"] or dep_morn["dep_time"] != dep_low["dep_time"]):
            lines.append(
                f"■ 1/6 오전 선호: {dep_morn['airline']}({dep_morn['dep_time']}) 1인 {dep_morn['price']:,}원 (4인 {dep_morn['price_4p']:,}원)"
            )
        lines.append(
            f"■ 1/10 오는편: {ret_low['airline']}({ret_low['dep_time']}) 1인 {ret_low['price']:,}원 (4인 {ret_low['price_4p']:,}원)"
        )
        lines.append("")
        lines.append(f"• 1인 순수 왕복: {rt_low_1p:,}원")
        lines.append(f"• 4인 순수 왕복: {rt_low_4p:,}원")
        lines.append(f"• 4인 부대교통비: {TRANSPORT_4P_TOTAL:,}원 (JR특급+국내공항버스/KTX)")
        lines.append(f"• 4인 최종 실총경비: {grand_low_4p:,}원 (1인 {round(grand_low_4p / 4):,}원)")

        if dep_morn and (dep_morn["airline"] != dep_low["airline"] or dep_morn["dep_time"] != dep_low["dep_time"]):
            rt_morn_4p = dep_morn["price_4p"] + ret_low["price_4p"]
            lines.append(
                f"  (오전출발 시 4인 항공 {rt_morn_4p:,}원 / 총경비 {rt_morn_4p + TRANSPORT_4P_TOTAL:,}원)"
            )
    else:
        lines.append("• 1/6~1/10 직항: 현재 매진 또는 조회 불가")

    if dep07_list and ret_list:
        d07 = dep07_list[0]
        rt07_4p = d07["price_4p"] + ret_list[0]["price_4p"]
        lines.append("")
        lines.append(
            f"[참고 3박4일 1/7~1/10] {d07['airline']}({d07['dep_time']}) 1인 {d07['price']:,}원 ➔ 4인 총경비 {rt07_4p + TRANSPORT_4P_TOTAL:,}원"
        )

    return "\n".join(lines)


async def main():
    force_notify = "--force-notify" in sys.argv or os.environ.get("FORCE_NOTIFY", "").lower() == "true"
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] Starting Google Flights cloud scan...")

    results = await fetch_all_routes()
    dep_res = results["dep_0106"]
    ret_res = results["ret_0110"]
    dep07_res = results["dep_0107"]

    print("dep_0106:", dep_res["status"], len(dep_res.get("flights", [])), dep_res.get("reason", ""))
    print("ret_0110:", ret_res["status"], len(ret_res.get("flights", [])), ret_res.get("reason", ""))
    print("dep_0107:", dep07_res["status"], len(dep07_res.get("flights", [])), dep07_res.get("reason", ""))

    state = load_state()
    actions = [
        {"action": "view", "label": "1/6 가는편(네이버 실결제)", "url": NAVER_URLS["dep_0106"]},
        {"action": "view", "label": "1/10 오는편(네이버 실결제)", "url": NAVER_URLS["ret_0110"]},
        {"action": "view", "label": "다구간 결합(네이버)", "url": NAVER_URLS["multi_0106"]},
    ]

    if dep_res["status"] == "error" or ret_res["status"] == "error":
        err_count = state.get("consecutive_errors", 0) + 1
        state["consecutive_errors"] = err_count
        state["last_error_at"] = now_str
        state["last_error_msg"] = f"dep={dep_res.get('reason')}, ret={ret_res.get('reason')}"
        save_state(state)
        print(f"Scrape error (#{err_count}): {state['last_error_msg']}")
        if force_notify or err_count == 3:
            send_ntfy(
                title="⚠️ [클라우드 트래커] Google Flights 조회 오류 점검 필요",
                message=f"클라우드 조회 중 오류가 발생했습니다 (#{err_count}).\n원인: {state['last_error_msg'][:180]}",
                priority="default",
                tags=["warning"],
                actions=actions,
            )
        return

    state["consecutive_errors"] = 0

    curr_avail = dep_res["status"] == "ok" and ret_res["status"] == "ok"
    curr_dep_low = dep_res["flights"][0] if dep_res["status"] == "ok" else None
    curr_ret_low = ret_res["flights"][0] if ret_res["status"] == "ok" else None
    curr_4p_rt = (curr_dep_low["price_4p"] + curr_ret_low["price_4p"]) if curr_avail else None
    curr_1p_rt = round(curr_4p_rt / 4) if curr_4p_rt else None

    prev_avail = state.get("is_available")
    prev_1p_rt = state.get("rt_1p_lowest")

    summary = build_summary_text(dep_res, ret_res, dep07_res)
    alert_title = None
    alert_msg = None
    alert_prio = "high"
    alert_tags = ["airplane"]

    if prev_avail is None:
        alert_title = "🚀 [24시간 클라우드 트래커] Google Flights 감시 가동!"
        alert_msg = f"맥북이 꺼져 있어도 클라우드에서 정상 가격을 조회했습니다.\n\n{summary}"
        alert_tags = ["rocket", "airplane"]
    elif prev_avail and not curr_avail:
        alert_title = "🚨 [전석 매진] 1/6~1/10 직항 좌석 소진!"
        alert_prio = "urgent"
        alert_tags = ["sos", "x"]
        alert_msg = f"1/6(수)~1/10(일) 4인 직항 좌석이 매진되었습니다.\n취소표 발생 시 즉시 알려드립니다.\n\n{summary}"
    elif not prev_avail and curr_avail:
        alert_title = "🔔 [취소표 출현] 1/6~1/10 직항 좌석 재오픈!"
        alert_prio = "urgent"
        alert_tags = ["bell", "sparkles"]
        alert_msg = f"매진되었던 직항 좌석이 다시 열렸습니다!\n\n{summary}"
    elif curr_avail and prev_1p_rt is not None:
        diff = curr_1p_rt - prev_1p_rt
        if diff <= -MIN_DROP_KRW:
            save_1p = -diff
            alert_title = f"🎉 [가격 인하] 1/6~1/10 왕복 1인 -{save_1p:,}원!"
            alert_prio = "urgent"
            alert_tags = ["tada", "chart_with_downwards_trend"]
            alert_msg = (
                f"실결제 최저가가 내려갔습니다!\n"
                f"• 1인 왕복: {prev_1p_rt:,}원 ➔ {curr_1p_rt:,}원 (-{save_1p:,}원)\n"
                f"• 4인 가족 절약: -{save_1p * 4:,}원\n\n{summary}"
            )
        elif diff >= MIN_HIKE_KRW:
            alert_title = f"⚠️ [싼 표 소진] 1/6~1/10 왕복 1인 +{diff:,}원 상승"
            alert_prio = "urgent"
            alert_tags = ["warning", "chart_with_upwards_trend"]
            alert_msg = (
                f"기존 최저가 좌석이 팔려 가격이 올랐습니다!\n"
                f"• 1인 왕복: {prev_1p_rt:,}원 ➔ {curr_1p_rt:,}원 (+{diff:,}원)\n"
                f"• 4인 가족 인상분: +{diff * 4:,}원\n\n{summary}"
            )

    if force_notify and not alert_title:
        alert_title = "🔥 [수동 확인] 1/6~1/10 홋카이도 클라우드 실시간 현황"
        alert_msg = f"클라우드에서 정상 조회된 실시간 항공권 가격입니다.\n\n{summary}"

    if alert_title:
        send_ntfy(alert_title, alert_msg, priority=alert_prio, tags=alert_tags, actions=actions)
    else:
        print(f"No significant price change (1p RT: {curr_1p_rt} vs prev {prev_1p_rt}).")

    state.update({
        "updated_at": now_str,
        "source": "google_flights",
        "is_available": curr_avail,
        "rt_1p_lowest": curr_1p_rt,
        "rt_4p_lowest": curr_4p_rt,
        "grand_4p_lowest": (curr_4p_rt + TRANSPORT_4P_TOTAL) if curr_4p_rt else None,
        "dep_0106_top3": dep_res.get("flights", [])[:3],
        "ret_0110_top2": ret_res.get("flights", [])[:2],
        "dep_0107_top2": dep07_res.get("flights", [])[:2],
    })
    save_state(state)


if __name__ == "__main__":
    asyncio.run(main())
