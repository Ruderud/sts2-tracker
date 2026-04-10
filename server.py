"""STS2 트래커 WebSocket 서버 - Swift 오버레이 앱에 데이터 제공."""

import asyncio
import json
import time
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from save_parser import parse_save, find_save_file, RunState
from card_db import load_card_db, fuzzy_match
from screen_capture import (
    find_game_window,
    capture_window,
    detect_card_reward_screen,
    extract_card_regions,
    ocr_card_names,
)
from tracker import score_card
from utils import clean_description


# 전역 상태
class TrackerState:
    def __init__(self):
        self.run_state: RunState | None = None
        self.recommendations: list = []
        self.ocr_status: str = "초기화 중..."
        self.window_id: int | None = None
        self.cards_db: list = []
        self.ocr_reader = None
        self.last_mtime: float = 0.0
        self.last_card_screen: bool = False
        self.clients: list[WebSocket] = []
        self._tracking = True

    def to_dict(self) -> dict:
        state = self.run_state
        run_data = None
        if state:
            card_counts: dict[str, int] = {}
            for card in state.deck:
                name = card.display_id
                card_counts[name] = card_counts.get(name, 0) + 1

            run_data = {
                "character": state.character,
                "current_hp": state.current_hp,
                "max_hp": state.max_hp,
                "gold": state.gold,
                "act": state.act + 1,
                "floor": state.floor,
                "seed": state.seed,
                "deck": [{"name": n, "count": c} for n, c in sorted(card_counts.items())],
                "deck_size": len(state.deck),
                "relics": [r.replace("RELIC.", "") for r in state.relics],
            }

        recs = []
        for card, match_pct, game_score in self.recommendations:
            recs.append({
                "name": card["name"],
                "id": card["id"],
                "cost": card.get("cost", "?"),
                "rarity": card.get("rarity", ""),
                "rarity_key": card.get("rarity_key", ""),
                "type": card.get("type", ""),
                "description": clean_description(card.get("description", "")),
                "match_pct": round(match_pct * 100),
                "score": game_score,
            })

        best_idx = -1
        if recs:
            best_idx = max(range(len(recs)), key=lambda i: recs[i]["score"])

        return {
            "run": run_data,
            "recommendations": recs,
            "best_idx": best_idx,
            "ocr_status": self.ocr_status,
            "connected": self.window_id is not None,
        }


tracker = TrackerState()


async def broadcast(data: dict):
    """모든 WebSocket 클라이언트에 데이터 전송."""
    msg = json.dumps(data, ensure_ascii=False)
    disconnected = []
    for ws in tracker.clients:
        try:
            await ws.send_text(msg)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        tracker.clients.remove(ws)


def tracking_loop():
    """백그라운드 트래킹 루프."""
    # OCR 초기화
    import easyocr
    tracker.ocr_status = "OCR 모델 로딩..."
    tracker.ocr_reader = easyocr.Reader(["ko", "en"], gpu=False, verbose=False)
    tracker.ocr_status = "준비 완료"

    save_path = find_save_file()

    while tracker._tracking:
        try:
            # 게임 창 찾기
            if tracker.window_id is None:
                wid = find_game_window()
                if wid:
                    tracker.window_id = wid

            # 세이브 파일 감시
            if save_path is None:
                save_path = find_save_file()
            if save_path and save_path.exists():
                mtime = save_path.stat().st_mtime
                if mtime != tracker.last_mtime:
                    tracker.last_mtime = mtime
                    state = parse_save(save_path)
                    if state:
                        tracker.run_state = state

            # 화면 캡처 + 카드 보상 감지
            if tracker.window_id and tracker.ocr_reader:
                img = capture_window(tracker.window_id)
                if img is not None:
                    is_card_screen = detect_card_reward_screen(img)
                    if is_card_screen and not tracker.last_card_screen:
                        tracker.ocr_status = "카드 인식 중..."
                        regions = extract_card_regions(img)
                        detected = ocr_card_names(regions, tracker.ocr_reader)
                        state = tracker.run_state
                        if state:
                            recs = []
                            for det in detected:
                                if not det.ocr_text:
                                    continue
                                matches = fuzzy_match(det.ocr_text, tracker.cards_db, threshold=0.3)
                                if matches:
                                    card, match_score = matches[0]
                                    game_score = score_card(card, state)
                                    recs.append((card, match_score, game_score))
                            tracker.recommendations = recs
                        tracker.ocr_status = "준비 완료"
                    elif not is_card_screen and tracker.last_card_screen:
                        tracker.recommendations = []
                    tracker.last_card_screen = is_card_screen

            time.sleep(1.5)
        except Exception as e:
            tracker.ocr_status = f"Error: {e}"
            time.sleep(3)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 시작/종료 시 트래킹 루프 관리."""
    tracker.cards_db = load_card_db()
    thread = threading.Thread(target=tracking_loop, daemon=True)
    thread.start()

    # 주기적으로 클라이언트에 상태 전송
    async def push_loop():
        while tracker._tracking:
            if tracker.clients:
                await broadcast(tracker.to_dict())
            await asyncio.sleep(1)

    push_task = asyncio.create_task(push_loop())
    yield
    tracker._tracking = False
    push_task.cancel()


app = FastAPI(lifespan=lifespan)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    tracker.clients.append(websocket)
    try:
        # 즉시 현재 상태 전송
        await websocket.send_text(json.dumps(tracker.to_dict(), ensure_ascii=False))
        while True:
            # 클라이언트 메시지 대기 (ping/명령)
            data = await websocket.receive_text()
            if data == "scan":
                # 수동 스캔
                if tracker.window_id and tracker.ocr_reader:
                    img = capture_window(tracker.window_id)
                    if img is not None:
                        tracker.ocr_status = "수동 스캔 중..."
                        regions = extract_card_regions(img)
                        detected = ocr_card_names(regions, tracker.ocr_reader)
                        state = tracker.run_state
                        if state:
                            recs = []
                            for det in detected:
                                if not det.ocr_text:
                                    continue
                                matches = fuzzy_match(det.ocr_text, tracker.cards_db, threshold=0.3)
                                if matches:
                                    card, match_score = matches[0]
                                    game_score = score_card(card, state)
                                    recs.append((card, match_score, game_score))
                            tracker.recommendations = recs
                        tracker.ocr_status = "준비 완료"
                        await broadcast(tracker.to_dict())
    except WebSocketDisconnect:
        tracker.clients.remove(websocket)


@app.get("/")
async def root():
    return {"status": "STS2 Tracker Server running"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9999, log_level="warning")
