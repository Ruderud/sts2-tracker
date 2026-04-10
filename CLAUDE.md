# STS2 Tracker

## 프로젝트 개요
Slay the Spire 2 실시간 트래커 앱 (macOS). 게임 화면 OCR + 세이브 파싱으로 카드 보상/전투 추천을 투명 오버레이로 표시.

## 기술 스택
- **Python 3.14** — 백엔드 (OCR, 추천 엔진, WebSocket 서버)
- **Swift** — 투명 오버레이 앱 (WKWebView + Quartz 게임 창 추적)
- **EasyOCR** — 한국어 게임 폰트 인식 (Tesseract는 인식 실패로 기각)
- **FastAPI + WebSocket** — Python↔Swift 실시간 통신
- **Spire Codex API** — 카드/유물 데이터 (576장, 한국어)
- **OpenCV** — 이미지 전처리, 밝기 분석으로 카드 영역 동적 탐지

## 아키텍처
```
[Python 서버 (server.py :9999)]
├── save_parser.py    — current_run.save → RunState (캐릭터/HP/덱/유물)
├── screen_capture.py — Quartz 화면 캡처 + 배너/카드/전투 화면 감지
├── card_db.py        — Spire Codex API 카드 DB + 이름+설명 퍼지 매칭
├── recommender.py    — 덱 분석 + 시너지/티어/필요도 기반 카드 스코어링
├── combat_advisor.py — 전투 중 덱 기반 전략 조언
├── tracker.py        — CLI 트래커 (레거시, app.py로 대체됨)
└── utils.py          — 게임 텍스트 태그 정리

[Swift 오버레이 (overlay/STS2Overlay.swift)]
├── WebSocket 클라이언트 → Python 서버 연결
├── 투명 WKWebView — HTML/CSS/JS로 추천 UI 렌더링
├── 게임 창 추적 (60fps, CGWindowListCopyWindowInfo)
├── 메뉴바 아이콘 (♠) — 종료/투명도/스캔/표시토글
└── 콘텐츠 높이 자동 조정

[macOS 앱 (STS2Tracker.app)]
└── start.sh → Python 서버 + Swift 오버레이 동시 실행
```

## 게임 경로
- 앱: `~/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/`
- 세이브: `~/Library/Application Support/SlayTheSpire2/steam/<STEAM_ID>/` (자동 탐지)
- 게임 PCK 암호화됨 (flags=2) → 카드 데이터 직접 추출 불가, Spire Codex API 사용

## 실행
```bash
# 방법 1: 바탕화면 앱 더블클릭
~/Desktop/STS2Tracker.app

# 방법 2: 터미널
cd ~/Desktop/Coding/sts2-tracker && ./start.sh

# 방법 3: 개별 실행
source .venv/bin/activate
python server.py &          # 백엔드
overlay/STS2Overlay &       # 오버레이 (사전 컴파일 필요)
```

## 오버레이 빌드
```bash
cd overlay && swiftc -o STS2Overlay STS2Overlay.swift -framework Cocoa -framework WebKit -framework CoreGraphics
```

## 테스트
```bash
source .venv/bin/activate
python card_db.py         # 카드 DB + 퍼지 매칭 테스트
python save_parser.py     # 세이브 파싱 테스트
python screen_capture.py  # 화면 캡처 + OCR 테스트
python recommender.py     # 추천 엔진 + 덱 분석 테스트
python combat_advisor.py  # 전투 어드바이저 테스트
```

## 기술 결정사항
- Tesseract → EasyOCR: 게임 폰트 인식 실패
- 카드 이름만 OCR → 전체 텍스트(이름+설명) 합쳐서 퍼지 매칭
- 카드 선택지는 세이브/로그에 없음 → 화면 OCR 필수
- 카드 영역 좌표: 하드코딩 → 밝기 프로파일 기반 동적 탐지 (해상도 적응)
- 짧은 카드명(2-3자) 오탐 방지: 위치 기반 매칭 전략

## 주의사항
- macOS 전용 (Quartz/AppKit/CGWindowList 의존)
- 화면 캡처 권한 필요 (시스템 설정 > 개인정보 > 화면 및 시스템 오디오 녹음)
- EasyOCR 첫 실행 시 모델 다운로드 (~100MB), CPU 모드 초기화 ~10초
