# STS2 Tracker

Slay the Spire 2 실시간 카드 추천 트래커 (macOS)

## 기능
- 세이브 파일 실시간 감시 (캐릭터/HP/덱/유물 상태)
- 게임 화면 캡처 + OCR로 카드 보상 선택지 인식
- 카드 DB 기반 퍼지 매칭 (한국어 지원)
- 간단한 점수 기반 카드 추천

## 설치

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

macOS 시스템 설정에서 터미널에 **화면 녹화 권한** 부여 필요.

## 사용법

```bash
source .venv/bin/activate
python tracker.py
```

게임 실행 중 트래커를 켜면:
1. 세이브 파일 변경 감지 → 현재 런 상태 표시
2. 카드 보상 화면 진입 → OCR로 카드 인식 → 추천 표시

## 로드맵

### Phase 1: 기본 트래커 (현재) ✅
- [x] 세이브 파일 파싱 (캐릭터/HP/덱/유물)
- [x] Quartz 화면 캡처
- [x] EasyOCR 한국어 카드 인식
- [x] Spire Codex API 카드 DB (576장)
- [x] 이름+설명 퍼지 매칭
- [x] 기본 점수 기반 추천

### Phase 2: 추천 엔진 고도화
- [ ] 캐릭터별 아키타입 분석 (덱 방향성 판단)
- [ ] 유물-카드 시너지 반영
- [ ] 현재 액트/층수별 가중치
- [ ] 에너지 커브 분석
- [ ] 유물 DB 추가 (Spire Codex API)
- [ ] 카드 업그레이드 고려

### Phase 3: UI 개선
- [ ] 터미널 TUI (Rich/Textual)
- [ ] 오버레이 또는 별도 창
- [ ] 카드 이미지 표시
- [ ] 실시간 덱 시각화

### Phase 4: 고급 기능
- [ ] 시드 기반 카드 보상 예측 (RNG 역공학)
- [ ] 전투 시뮬레이션 (카드 가치 정밀 평가)
- [ ] 맵 경로 최적화 추천
- [ ] 상점 구매 추천
- [ ] 런 통계/히스토리

### Phase 5: 데스크탑 앱
- [ ] Swift 네이티브 앱 (ScreenCaptureKit 스트리밍)
- [ ] 메뉴바 앱으로 상주
- [ ] 게임 오버레이 (투명 창)
