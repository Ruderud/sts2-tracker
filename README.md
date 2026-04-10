# STS2 Tracker

Slay the Spire 2 실시간 카드 추천 트래커 (macOS)

게임 화면 위에 투명 오버레이로 카드 보상 추천 + 전투 조언을 표시합니다.

## 기능
- **카드 보상 추천** — OCR로 카드 인식 → 덱 분석 기반 최적 카드 추천
  - 카드 티어 + 시너지 + 덱 필요도(피해/방어/드로우/스케일링) 반영
  - ★ 시너지, Exhaust 덱압축, 에너지 효율 분석
  - 넘기기(skip) 옵션 포함
- **전투 어드바이저** — 전투 화면에서 덱 기반 전략 팁 표시
- **투명 오버레이** — 게임 창에 붙어서 60fps로 따라다님
- **메뉴바 아이콘** — ♠ 아이콘으로 투명도 조절/종료
- **세이브 실시간 감시** — 방 이동/카드 획득 시 자동 업데이트

## 설치

```bash
git clone https://github.com/Ruderud/sts2-tracker.git
cd sts2-tracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Swift 오버레이 빌드
cd overlay
swiftc -o STS2Overlay STS2Overlay.swift -framework Cocoa -framework WebKit -framework CoreGraphics
```

macOS 시스템 설정에서 터미널에 **화면 녹화 권한** 부여 필요.

## 사용법

```bash
./start.sh
```

또는 바탕화면의 **STS2Tracker.app** 더블클릭.

## 로드맵

### Phase 1: 기본 트래커 ✅
- [x] 세이브 파일 파싱 (캐릭터/HP/덱/유물)
- [x] Quartz 화면 캡처
- [x] EasyOCR 한국어 카드 인식
- [x] Spire Codex API 카드 DB (576장)
- [x] 이름+설명 퍼지 매칭
- [x] 해상도 적응형 카드 영역 탐지

### Phase 2: 추천 엔진 ✅
- [x] 덱 분석 (공격/스킬/파워 비율, ★시너지, 에너지 커브)
- [x] 필요도 평가 (피해/방어/드로우/스케일링)
- [x] 카드 티어 + 시너지 스코어링
- [x] 중복/덱크기 패널티, 넘기기(skip) 옵션
- [ ] 유물-카드 시너지 반영
- [ ] 유물 DB 추가 (Spire Codex API)
- [ ] 카드 업그레이드 고려
- [ ] 다른 캐릭터 티어 리스트

### Phase 3: UI ✅
- [x] Swift 투명 오버레이 (게임 창 추적 60fps)
- [x] 메뉴바 아이콘 (♠) + 투명도 조절
- [x] WebSocket 실시간 통신
- [x] 콘텐츠 높이 자동 조정
- [x] 화면 재인식 버튼
- [x] macOS 앱 번들 (.app)
- [ ] 카드 이미지 표시

### Phase 4: 전투 지원 (진행 중)
- [x] 전투 화면 감지
- [x] 덱 기반 전투 조언 (기본)
- [ ] 손패 카드 OCR
- [ ] 적 HP/인텐트 인식
- [ ] 카드 사용 순서 추천
- [ ] 턴 시뮬레이션

### Phase 5: 고급 기능
- [ ] 시드 기반 카드 보상 예측 (RNG 역공학)
- [ ] 맵 경로 최적화 추천
- [ ] 상점 구매 추천
- [ ] 런 통계/히스토리
- [ ] ScreenCaptureKit 스트리밍 (프레임 단위 캡처)
