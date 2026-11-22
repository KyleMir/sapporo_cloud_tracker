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

# Google Flights 조회 URL (성인 4인, 편도, 직항, 한국/한국어/KRW)
GF_URLS = {
    "dep_0106": "https://www.google.com/travel/flights?q=Flights%20from%20ICN%20to%20CTS%20on%202027-01-06%20oneway%20nonstop%204%20adults&hl=ko&gl=KR&curr=KRW",
    "ret_0110": "https://www.google.com/travel/flights?q=Flights%20from%20HKD%20to%20ICN%20on%202027-01-10%20oneway%20nonstop%204%20adults&hl=ko&gl=KR&curr=KRW",
    "dep_0107": "https://www.google.com/travel/flights?q=Flights%20from%20ICN%20to%20CTS%20on%202027-01-07%20oneway%20nonstop%204%20adults&hl=ko&gl=KR&curr=KRW",
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
MIN_HIKE_KRW = 4000


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
                    await asyncio.sleep(1.5)
                    # 숨겨진 '더 많은 항공편' / '더보기'가 있으면 펼쳐서 이스타항공/에어프레미아 누락 방지
                    try:
                        more_btn = page.locator("button:has-text('더 많은 항공편'), button:has-text('항공편 더보기'), button:has-text('more flights')").first
                        if await more_btn.is_visible(timeout=1200):
                            await more_btn.click()
                            await asyncio.sleep(1.5)
                    except Exception:
                        pass
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


def pick_airline(flights: list, keywords: tuple):
    for f in flights:
        name = f.get("airline", "").lower()
        if any(k in name for k in keywords):
            return f
    return None


def build_pair_summary(dep_f, ret_f, label: str) -> dict:
    if not dep_f or not ret_f:
        return {"label": label, "available": False}
    rt_4p = dep_f["price_4p"] + ret_f["price_4p"]
    rt_1p = round(rt_4p / 4)
    grand_4p = rt_4p + TRANSPORT_4P_TOTAL
    return {
        "label": label,
        "available": True,
        "dep_time": dep_f["dep_time"],
        "dep_1p": dep_f["price"],
        "dep_4p": dep_f["price_4p"],
        "ret_time": ret_f["dep_time"],
        "ret_1p": ret_f["price"],
        "ret_4p": ret_f["price_4p"],
        "rt_1p": rt_1p,
        "rt_4p": rt_4p,
        "grand_4p": grand_4p,
    }


def format_pair_line(pair: dict) -> str:
    if not pair.get("available"):
        if "이스타" in pair.get("label", ""):
            return f"■ [{pair['label']}] 클라우드(구글) 미노출 ➔ 아래 네이버 버튼에서 확인(약 31.9만+24.9만)"
        return f"■ [{pair['label']}] 직항 매진/조회 불가"
    return (
        f"■ [{pair['label']}] (구글 정가 기준 · 네이버가 보통 1~2만원 더 저렴)\n"
        f"   가는편({pair['dep_time']}) {pair['dep_1p']:,}원 + 오는편({pair['ret_time']}) {pair['ret_1p']:,}원\n"
        f"   ➔ 1인 왕복 {pair['rt_1p']:,}원 | 4인 왕복 {pair['rt_4p']:,}원 | 총경비 {pair['grand_4p']:,}원"
    )


def build_summary_text(dep_res: dict, ret_res: dict, dep07_res: dict, pairs: dict) -> str:
    dep_list = dep_res.get("flights", [])
    ret_list = ret_res.get("flights", [])
    dep07_list = dep07_res.get("flights", [])

    lines = ["[지정 항공사 집중 감시 · 1/6(수)~1/10(일) Google Flights 기준]"]
    lines.append(format_pair_line(pairs["airpremia_jeju"]))
    lines.append(format_pair_line(pairs["eastar_jeju"]))
    lines.append(f"• 4인 부대교통비 포함액: {TRANSPORT_4P_TOTAL:,}원 (JR특급+국내공항버스/KTX)")

    if dep_list and ret_list:
        dep_low = dep_list[0]
        ret_low = ret_list[0]
        rt_low_4p = dep_low["price_4p"] + ret_low["price_4p"]
        lines.append("")
        lines.append(
            f"[전체 최저가 참고] {dep_low['airline']}({dep_low['dep_time']}) + {ret_low['airline']} ➔ 1인 {round(rt_low_4p / 4):,}원 / 4인 {rt_low_4p:,}원 (총 {rt_low_4p + TRANSPORT_4P_TOTAL:,}원)"
        )

    if dep07_list and ret_list:
        d07 = dep07_list[0]
        rt07_4p = d07["price_4p"] + ret_list[0]["price_4p"]
        lines.append(
            f"[3박4일 1/7~1/10 참고] {d07['airline']}({d07['dep_time']}) 1인 {d07['price']:,}원 ➔ 4인 총경비 {rt07_4p + TRANSPORT_4P_TOTAL:,}원"
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

    dep_flights = dep_res.get("flights", [])
    ret_flights = ret_res.get("flights", [])
    print("ALL_DEP_0106:", [(f["airline"], f["dep_time"], f["price"]) for f in dep_flights])

    ap_dep = pick_airline(dep_flights, ("에어프레미아", "air premia"))
    es_dep = pick_airline(dep_flights, ("이스타", "eastar"))
    jeju_ret = pick_airline(ret_flights, ("제주", "jeju"))

    curr_pairs = {
        "airpremia_jeju": build_pair_summary(ap_dep, jeju_ret, "에어프레미아+제주항공"),
        "eastar_jeju": build_pair_summary(es_dep, jeju_ret, "이스타항공+제주항공"),
    }

    curr_avail = dep_res["status"] == "ok" and ret_res["status"] == "ok"
    curr_dep_low = dep_flights[0] if dep_flights else None
    curr_ret_low = ret_flights[0] if ret_flights else None
    curr_4p_rt = (curr_dep_low["price_4p"] + curr_ret_low["price_4p"]) if curr_avail else None
    curr_1p_rt = round(curr_4p_rt / 4) if curr_4p_rt else None

    prev_avail = state.get("is_available")
    prev_1p_rt = state.get("rt_1p_lowest")
    prev_pairs = state.get("pairs", {})

    summary = build_summary_text(dep_res, ret_res, dep07_res, curr_pairs)
    alert_title = None
    alert_msg = None
    alert_prio = "high"
    alert_tags = ["airplane"]

    # 지정 항공사 조합 변동 체크 (에어프레미아+제주항공, 이스타항공+제주항공)
    pair_events = []
    for p_key in ("airpremia_jeju", "eastar_jeju"):
        cur_p = curr_pairs[p_key]
        old_p = prev_pairs.get(p_key)
        if not old_p:
            continue
        if old_p.get("available") and not cur_p.get("available"):
            pair_events.append(("SOLDOUT", cur_p["label"], old_p["rt_1p"], None, 0))
        elif not old_p.get("available") and cur_p.get("available"):
            pair_events.append(("RESTOCK", cur_p["label"], None, cur_p["rt_1p"], 0))
        elif old_p.get("available") and cur_p.get("available"):
            diff_p = cur_p["rt_1p"] - old_p["rt_1p"]
            if diff_p <= -MIN_DROP_KRW:
                pair_events.append(("DROP", cur_p["label"], old_p["rt_1p"], cur_p["rt_1p"], -diff_p))
            elif diff_p >= MIN_HIKE_KRW:
                pair_events.append(("HIKE", cur_p["label"], old_p["rt_1p"], cur_p["rt_1p"], diff_p))

    if prev_avail is None or not prev_pairs:
        alert_title = "🎯 [집중 감시 가동] 에어프레미아·이스타 + 제주항공 트래킹 시작"
        alert_msg = f"요청하신 [에어프레미아+제주항공] 및 [이스타항공+제주항공] 조합 추적을 시작했습니다.\n\n{summary}"
        alert_tags = ["rocket", "airplane"]
    elif pair_events:
        ev_type, ev_lbl, ev_old, ev_new, ev_diff = pair_events[0]
        alert_prio = "urgent"
        ev_lines = []
        for t, lbl, o, n, d in pair_events:
            if t == "DROP":
                ev_lines.append(f"🎉 [{lbl}] 1인 {o:,}원 ➔ {n:,}원 (-{d:,}원 / 4인 -{d*4:,}원 인하!)")
            elif t == "HIKE":
                ev_lines.append(f"⚠️ [{lbl}] 1인 {o:,}원 ➔ {n:,}원 (+{d:,}원 / 4인 +{d*4:,}원 상승!)")
            elif t == "SOLDOUT":
                ev_lines.append(f"🚨 [{lbl}] 직항 좌석이 매진되었습니다!")
            elif t == "RESTOCK":
                ev_lines.append(f"🔔 [{lbl}] 취소표 재오픈! (1인 {n:,}원)")
        if ev_type == "DROP":
            alert_title = f"🎉 [{ev_lbl}] 가격 인하! (-{ev_diff:,}원)"
            alert_tags = ["tada", "chart_with_downwards_trend"]
        elif ev_type == "HIKE":
            alert_title = f"⚠️ [{ev_lbl}] 가격 상승/싼표 소진! (+{ev_diff:,}원)"
            alert_tags = ["warning", "chart_with_upwards_trend"]
        elif ev_type == "SOLDOUT":
            alert_title = f"🚨 [{ev_lbl}] 좌석 매진!"
            alert_tags = ["sos", "x"]
        else:
            alert_title = f"🔔 [{ev_lbl}] 취소표 재입고!"
            alert_tags = ["bell", "sparkles"]
        alert_msg = "\n".join(ev_lines) + "\n\n" + summary
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
    # 진에어 등 전체 최저가 단독 변동으로는 클라우드 오탐/혼선 방지를 위해 알림 생략 (에어프레미아+제주항공만 알림)

    if force_notify and not alert_title:
        alert_title = "🔥 [수동 확인] 에어프레미아·이스타 + 제주항공 실시간 현황"
        alert_msg = f"클라우드에서 조회된 지정 항공사 조합 현황입니다.\n\n{summary}"

    if alert_title:
        send_ntfy(alert_title, alert_msg, priority=alert_prio, tags=alert_tags, actions=actions)
    else:
        print(f"No significant change. AirPremia={curr_pairs['airpremia_jeju'].get('rt_1p')}, Eastar={curr_pairs['eastar_jeju'].get('rt_1p')}")

    hist = state.get("history", [])
    hist.append({
        "at": now_str,
        "ap_jeju_1p": curr_pairs["airpremia_jeju"].get("rt_1p"),
        "ap_jeju_grand4p": curr_pairs["airpremia_jeju"].get("grand_4p"),
        "lowest_1p": curr_1p_rt,
    })

    state.update({
        "updated_at": now_str,
        "source": "google_flights",
        "is_available": curr_avail,
        "pairs": curr_pairs,
        "rt_1p_lowest": curr_1p_rt,
        "rt_4p_lowest": curr_4p_rt,
        "grand_4p_lowest": (curr_4p_rt + TRANSPORT_4P_TOTAL) if curr_4p_rt else None,
        "dep_0106_top3": dep_flights[:3],
        "ret_0110_top2": ret_flights[:2],
        "dep_0107_top2": dep07_res.get("flights", [])[:2],
        "history": hist[-100:],
    })
    save_state(state)


if __name__ == "__main__":
    asyncio.run(main())
