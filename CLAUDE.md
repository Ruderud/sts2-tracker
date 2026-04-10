# STS2 Tracker

## 프로젝트 개요
Slay the Spire 2 실시간 트래커 앱. 세이브 파일 파싱 + 화면 OCR로 카드 보상을 인식하고 최적 선택을 추천.

## 기술 스택
- Python 3.14 (macOS)
- Quartz/AppKit (pyobjc) - 게임 창 캡처
- EasyOCR - 한국어 카드 텍스트 인식
- OpenCV - 이미지 전처리
- Spire Codex API - 카드/유물 데이터

## 아키텍처
```
save_parser.py   - 세이브 파일(JSON) → RunState (캐릭터/HP/덱/유물)
card_db.py       - Spire Codex API 카드 DB + 퍼지 매칭 (이름+설명)
screen_capture.py - Quartz 화면 캡처 + 카드 보상 화면 감지 + EasyOCR
tracker.py       - 메인 루프 (세이브 감시 + 캡처 + 추천)
```

## 게임 경로
- 앱: `~/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/`
- 세이브: `~/Library/Application Support/SlayTheSpire2/steam/<STEAM_ID>/` (자동 탐지)
- modded 세이브: 위 경로 + `modded/profile1/saves/current_run.save`

## 실행
```bash
source .venv/bin/activate
python tracker.py
```

## 테스트 방법
```bash
python card_db.py       # 카드 DB + 퍼지 매칭 테스트
python save_parser.py   # 세이브 파싱 테스트
python screen_capture.py # 화면 캡처 + OCR 테스트
```

## 주의사항
- macOS 전용 (Quartz/AppKit 의존)
- 화면 캡처 권한 필요 (시스템 설정 > 개인정보 > 화면 및 시스템 오디오 녹음)
- EasyOCR 첫 실행 시 모델 다운로드 (~100MB)
- 게임 창 해상도/위치에 따라 카드 영역 좌표 조정 필요할 수 있음
