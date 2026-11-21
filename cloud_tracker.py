"""
24시간 무인 클라우드 항공권 트래커 (GitHub Actions + ntfy iPhone 푸시 알림)
- 보유 카드(KB국민 쿠팡와우 / 신한 / 모든 결제수단) 실결제 기준(&cardBenefit=0) 전용
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

# 감시 대상 URL (보유 카드 실결제 기준: cardBenefit=0 고정)
URLS = {
    "dep_0106": "https://flight.naver.com/flights/international/ICN-CTS-20270106?adult=4&isDirect=true&fareType=Y&cardBenefit=0",
    "ret_0110": "https://flight.naver.com/flights/international/HKD-ICN-20270110?adult=4&isDirect=true&fareType=Y&cardBenefit=0",
    "dep_0107": "https://flight.naver.com/flights/international/ICN-CTS-20270107?adult=4&isDirect=true&fareType=Y&cardBenefit=0",
    "multi_0106": "https://flight.naver.com/flights/international/ICN-CTS-20270106/HKD-ICN-20270110?adult=4&isDirect=true&fareType=Y&cardBenefit=0",
}

# 4인 부대교통비 고정 단가 (2026년 공식 기준)
# 대구 3인 왕복(우등버스/KTX 43,500*2*3=261,000) + 서울 1인 왕복(17,000*2=34,000)
# + 일본 JR 특급 호쿠토(삿포로->하코다테 96,000*4=384,000) + 기타 연결 49,200 = 728,200원
TRANSPORT_4P_TOTAL = 728200
TRANSPORT_1P_AVG = 182050

# 알림 임계값 (1인 기준)
MIN_DROP_KRW = 3000
MIN_HIKE_KRW = 5000

SKIP_AIRLINE_TOKENS = (
    "CO2", "이벤트혜택", "예약 많은", "많이 찾는", "최저가", "최단 비행시간", "공동운항", "인기 항공편"
)


def parse_flights_from_text(body_text: str) -> dict:
    """네이버 항공권 body.innerText에서 직항·실결제 항공편 목록을 파싱합니다."""
    if not body_text or len(body_text.strip()) < 200:
        return {"status": "error", "reason": "empty_or_blocked", "flights": []}

    if "검색된 항공편이 없습니다" in body_text or "조건에 맞는 항공편이 없습니다" in body_text:
        return {"status": "soldout", "flights": []}

    if "직항" not in body_text and "항공편 선택" not in body_text and "인천" not in body_text:
        return {"status": "error", "reason": "unexpected_page_content", "flights": []}

    lines = [l.strip() for l in body_text.splitlines() if l.strip()]
    flights = []
    seen = set()

    time_airport_re = re.compile(r"^(\d{2}:\d{2})([A-Z]{3})$")
    num_re = re.compile(r"^([\d,]+)$")
    inline_price_re = re.compile(r"([\d,]+)\s*원~")

    for i, line in enumerate(lines):
        m_dep = time_airport_re.match(line)
        if not m_dep:
            continue
        dep_time, dep_code = m_dep.group(1), m_dep.group(2)

        # 바로 다음 줄이 도착 시간/공항인지 확인
        if i + 2 >= len(lines):
            continue
        m_arr = time_airport_re.match(lines[i + 1])
        if not m_arr:
            continue
        arr_time, arr_code = m_arr.group(1), m_arr.group(2)

        # 직항 여부 체크
        duration_line = lines[i + 2]
        if "직항" not in duration_line or "경유" in duration_line:
            continue

        # 항공사명 역탐색 (최대 5줄 위)
        airline = "직항"
        for b in range(i - 1, max(-1, i - 6), -1):
            cand = lines[b]
            if any(tok in cand for tok in SKIP_AIRLINE_TOKENS) or "원~" in cand or "적립" in cand:
                continue
            if len(cand) >= 2:
                airline = cand
                break

        # 최종 실결제가 탐색 ('편도'/'왕복'/'다구간' 바로 뒤 숫자 우선)
        price = None
        for f in range(i + 3, min(len(lines), i + 12)):
            cur = lines[f]
            if time_airport_re.match(cur):
                break
            if cur in ("편도", "왕복", "다구간") and f + 1 < len(lines):
                m_num = num_re.match(lines[f + 1])
                if m_num:
                    price = int(m_num.group(1).replace(",", ""))
                    break
                m_inl = inline_price_re.search(lines[f + 1])
                if m_inl:
                    price = int(m_inl.group(1).replace(",", ""))
                    break
            elif cur.startswith("편도 ") or cur.startswith("왕복 "):
                m_inl = inline_price_re.search(cur)
                if m_inl:
                    price = int(m_inl.group(1).replace(",", ""))
                    break

        if price and price > 30000:
            key = (airline, dep_time, dep_code, arr_code, price)
            if key not in seen:
                seen.add(key)
                flights.append({
                    "airline": airline,
                    "dep_time": dep_time,
                    "arr_time": arr_time,
                    "route": f"{dep_code}->{arr_code}",
                    "price": price,
                })

    if not flights:
        return {"status": "soldout", "flights": []}

    flights.sort(key=lambda x: x["price"])
    return {"status": "ok", "flights": flights}


async def fetch_all_routes() -> dict:
    """Playwright 헤드리스 크롬으로 모든 대상 노선을 스크래핑합니다."""
    from playwright.async_api import async_playwright
    results = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1440, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        targets = ["dep_0106", "ret_0110", "dep_0107"]
        for key in targets:
            url = URLS[key]
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=35000)
                # 항공권 리스트 렌더링 대기 (최대 14초)
                try:
                    await page.wait_for_selector("text=/직항|검색된 항공편이 없습니다/", timeout=14000)
                    await asyncio.sleep(2.5)
                except Exception:
                    await asyncio.sleep(4.0)

                body_text = await page.inner_text("body")
                results[key] = parse_flights_from_text(body_text)
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
        "click": URLS["multi_0106"],
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

    lines = ["[보유카드(KB쿠팡와우/신한/모든결제) 실결제 기준]"]
    if dep_list and ret_list:
        dep_low = dep_list[0]
        dep_morn = pick_morning_best(dep_list)
        ret_low = ret_list[0]

        rt_low_1p = dep_low["price"] + ret_low["price"]
        rt_low_4p = rt_low_1p * 4
        grand_low_4p = rt_low_4p + TRANSPORT_4P_TOTAL

        lines.append(
            f"■ 1/6 가는편 최저: {dep_low['airline']}({dep_low['dep_time']}) {dep_low['price']:,}원"
        )
        if dep_morn and dep_morn["airline"] != dep_low["airline"]:
            lines.append(
                f"■ 1/6 오전 선호: {dep_morn['airline']}({dep_morn['dep_time']}) {dep_morn['price']:,}원"
            )
        lines.append(
            f"■ 1/10 오는편: {ret_low['airline']}({ret_low['dep_time']}) {ret_low['price']:,}원"
        )
        lines.append("")
        lines.append(f"• 1인 순수 왕복: {rt_low_1p:,}원")
        lines.append(f"• 4인 순수 왕복: {rt_low_4p:,}원")
        lines.append(f"• 4인 부대교통비: {TRANSPORT_4P_TOTAL:,}원 (JR특급+국내공항버스/KTX)")
        lines.append(f"• 4인 최종 실총경비: {grand_low_4p:,}원 (1인 {grand_low_4p // 4:,}원)")

        if dep_morn and dep_morn["airline"] != dep_low["airline"]:
            rt_morn_4p = (dep_morn["price"] + ret_low["price"]) * 4
            lines.append(
                f"  (오전출발 선택 시 4인 항공 {rt_morn_4p:,}원 / 총경비 {rt_morn_4p + TRANSPORT_4P_TOTAL:,}원)"
            )
    else:
        lines.append("• 1/6~1/10 직항: 현재 매진 또는 조회 불가")

    if dep07_list and ret_list:
        d07 = dep07_list[0]
        rt07_4p = (d07["price"] + ret_list[0]["price"]) * 4
        lines.append("")
        lines.append(
            f"[참고 3박4일 1/7~1/10] {d07['airline']}({d07['dep_time']}) {d07['price']:,}원 ➔ 4인 총경비 {rt07_4p + TRANSPORT_4P_TOTAL:,}원"
        )

    return "\n".join(lines)


async def main():
    force_notify = "--force-notify" in sys.argv or os.environ.get("FORCE_NOTIFY", "").lower() == "true"
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] Starting cloud scan...")

    results = await fetch_all_routes()
    dep_res = results["dep_0106"]
    ret_res = results["ret_0110"]
    dep07_res = results["dep_0107"]

    print("dep_0106:", dep_res["status"], len(dep_res.get("flights", [])), dep_res.get("reason", ""))
    print("ret_0110:", ret_res["status"], len(ret_res.get("flights", [])), ret_res.get("reason", ""))
    print("dep_0107:", dep07_res["status"], len(dep07_res.get("flights", [])), dep07_res.get("reason", ""))

    state = load_state()
    actions = [
        {"action": "view", "label": "1/6 가는편(인천➔삿포로)", "url": URLS["dep_0106"]},
        {"action": "view", "label": "1/10 오는편(하코다테➔인천)", "url": URLS["ret_0110"]},
        {"action": "view", "label": "다구간 결합예매", "url": URLS["multi_0106"]},
    ]

    # 스크래핑 오류(봇 차단/네트워크 장애) 체크
    if dep_res["status"] == "error" or ret_res["status"] == "error":
        err_count = state.get("consecutive_errors", 0) + 1
        state["consecutive_errors"] = err_count
        state["last_error_at"] = now_str
        state["last_error_msg"] = f"dep={dep_res.get('reason')}, ret={ret_res.get('reason')}"
        save_state(state)
        print(f"Scrape error (#{err_count}): {state['last_error_msg']}")
        if force_notify or err_count == 3:
            send_ntfy(
                title="⚠️ [클라우드 트래커] 네이버 항공권 조회 차단/오류 점검 필요",
                message=f"클라우드 서버에서 3회 연속 조회에 실패했습니다.\n원인: {state['last_error_msg']}\n시각: {now_str}",
                priority="default",
                tags=["warning"],
                actions=actions,
            )
        return

    state["consecutive_errors"] = 0

    curr_avail = dep_res["status"] == "ok" and ret_res["status"] == "ok"
    curr_dep_low = dep_res["flights"][0] if dep_res["status"] == "ok" else None
    curr_ret_low = ret_res["flights"][0] if ret_res["status"] == "ok" else None
    curr_1p_rt = (curr_dep_low["price"] + curr_ret_low["price"]) if curr_avail else None

    prev_avail = state.get("is_available")
    prev_1p_rt = state.get("rt_1p_lowest")

    summary = build_summary_text(dep_res, ret_res, dep07_res)
    alert_title = None
    alert_msg = None
    alert_prio = "high"
    alert_tags = ["airplane"]

    if prev_avail is None:
        # 첫 클라우드 스캔 가동 완료 알림
        alert_title = "🚀 [24시간 클라우드 트래커] 첫 가동 완료 (1/6~1/10)"
        alert_msg = f"맥북이 꺼져 있어도 24시간 감시를 시작했습니다.\n\n{summary}"
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
        alert_title = "🔥 [수동 확인] 1/6~1/10 홋카이도 실결제 현황"
        alert_msg = f"요청하신 실시간 클라우드 조회 결과입니다.\n\n{summary}"

    if alert_title:
        send_ntfy(alert_title, alert_msg, priority=alert_prio, tags=alert_tags, actions=actions)
    else:
        print(f"No significant price change (1p RT: {curr_1p_rt} vs prev {prev_1p_rt}).")

    # 상태 저장
    state.update({
        "updated_at": now_str,
        "is_available": curr_avail,
        "rt_1p_lowest": curr_1p_rt,
        "rt_4p_lowest": (curr_1p_rt * 4) if curr_1p_rt else None,
        "grand_4p_lowest": (curr_1p_rt * 4 + TRANSPORT_4P_TOTAL) if curr_1p_rt else None,
        "dep_0106_top3": dep_res.get("flights", [])[:3],
        "ret_0110_top2": ret_res.get("flights", [])[:2],
        "dep_0107_top2": dep07_res.get("flights", [])[:2],
    })
    save_state(state)


if __name__ == "__main__":
    asyncio.run(main())
