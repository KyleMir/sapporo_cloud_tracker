# 삿포로 가족여행 24시간 클라우드 항공권 트래커

맥북 전원이 꺼져 있어도 **GitHub Actions 클라우드 서버**에서 15분마다 자동으로 네이버 항공권을 조회하고, 가격 변동·매진·취소표 발생 시 **아이폰 `ntfy` 앱**으로 즉시 푸시 알림을 보냅니다.

## 감시 기준
- **보유 카드 실결제 전용 (`&cardBenefit=0`)**: KB국민 쿠팡와우 / 신한 / 일반결제 기준
- **우선 타깃**: `1/6(수) 인천➔삿포로` IN / `1/10(일) 하코다테➔인천` OUT (4박 5일, 성인 4인)
- **비교 타깃**: `1/7(목) 인천➔삿포로` IN / `1/10(일) 하코다테➔인천` OUT (3박 4일, 성인 4인)
- **알림 채널**: `ntfy.sh/kylenoti_CE240803`

## GitHub 배포 순서 (1분 컷)
1. GitHub에서 새 저장소(예: `sapporo-cloud-tracker`)를 **Public(무제한 무료)** 으로 생성합니다. *(※ Private 저장소는 월 2,000분 무료 한도가 있습니다.)*
2. 아래 명령어로 이 폴더를 업로드합니다:
   ```bash
   cd /Users/kylebs/sapporo-cloud-tracker
   git remote add origin https://github.com/본인아이디/sapporo-cloud-tracker.git
   git push -u origin main
   ```
3. GitHub 저장소 상단 **Actions** 탭 ➔ **Sapporo 24/7 Flight Tracker** ➔ **[Run workflow]** 버튼을 한 번 눌러 테스트하면 즉시 아이폰 `ntfy` 앱으로 첫 가동 알림이 도착하며, 이후 15분마다 24시간 자동 감시됩니다.
