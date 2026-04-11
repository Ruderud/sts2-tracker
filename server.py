"""STS2 트래커 WebSocket 서버 - Swift 오버레이 앱에 데이터 제공."""

import asyncio
import difflib
import json
import re
import time
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from save_parser import parse_save, find_save_file, RunState
from card_db import load_card_db, fuzzy_match
from event_db import flatten_event_pages, load_event_db
from event_recommender import load_event_priors, recommend_event_choices
from map_recommender import recommend_map_route
from opening_choice_recommender import recommend_opening_choices
from shop_recommender import load_potion_db, load_relic_db, recommend_shop_purchases
from screen_capture import (
    find_game_window,
    capture_window,
    detect_card_reward_screen,
    detect_combat_screen,
    detect_map_current_anchor,
    detect_map_node_rows,
    extract_card_regions,
    ocr_card_choice_prompt,
    ocr_combat_hand_cards,
    ocr_event_choices,
    ocr_event_text,
    ocr_card_names,
    ocr_opening_choices,
    ocr_regent_star_count,
    ocr_shop_text_lines,
)
from recommender import build_scoring_context, score_card as score_card_v2
from combat_advisor import generate_combat_advice, recommend_combat_hand
from utils import clean_description, clean_game_text


_CHOICE_COUNT_RE = re.compile(r"(\d+)\s*장")
_NUMBER_RE = re.compile(r"(\d+)")


def _normalize_choice_title(text: str) -> str:
    return clean_description(clean_game_text(text or "")).replace(" ", "").strip().lower()


def _attach_choice_anchors(recommendation: dict | None, choices: list) -> dict | None:
    if not recommendation or not choices:
        return recommendation

    remaining = list(choices)
    for option in recommendation.get("options", []):
        option_title = _normalize_choice_title(option.get("title", ""))
        if not option_title:
            continue

        best_idx = None
        best_ratio = 0.0
        for idx, choice in enumerate(remaining):
            choice_title = _normalize_choice_title(getattr(choice, "title", ""))
            if not choice_title:
                continue
            ratio = difflib.SequenceMatcher(None, option_title, choice_title).ratio()
            if option_title in choice_title or choice_title in option_title:
                ratio = max(ratio, 0.92)
            if ratio > best_ratio:
                best_ratio = ratio
                best_idx = idx

        if best_idx is None or best_ratio < 0.42:
            continue

        matched = remaining.pop(best_idx)
        option["position"] = matched.position
        option["screen_anchor"] = matched.screen_anchor

    unmatched = [option for option in recommendation.get("options", []) if not option.get("screen_anchor")]
    if unmatched and len(remaining) >= len(unmatched):
        remaining_sorted = sorted(
            remaining,
            key=lambda choice: (
                (choice.screen_anchor or {}).get("y", 0.0),
                getattr(choice, "position", 0),
            ),
        )
        fallback_choices = remaining_sorted[-len(unmatched):]
        fallback_choices.sort(
            key=lambda choice: (
                (choice.screen_anchor or {}).get("y", 0.0),
                getattr(choice, "position", 0),
            ),
        )
        for option, matched in zip(unmatched, fallback_choices):
            option["position"] = matched.position
            option["screen_anchor"] = matched.screen_anchor

    return recommendation


def _extract_numbers(text: str) -> list[int]:
    return [int(match) for match in _NUMBER_RE.findall(text or "")]


def _fallback_event_choice_recommendation(choices: list, state: RunState) -> dict | None:
    if len(choices) < 2:
        return None

    options = []
    hp_pct = state.current_hp / max(state.max_hp, 1)
    for idx, choice in enumerate(choices):
        title = clean_game_text(choice.title).strip() or f"선택지 {idx + 1}"
        description = clean_game_text(choice.description).strip()
        text = f"{title}\n{description}"
        numbers = _extract_numbers(text)

        score = 2.0
        reasons: list[str] = []

        if "골드" in text and ("얻" in text or "획득" in text):
            amount = max(numbers) if numbers else 50
            bonus = min(2.2, 0.6 + amount / 70)
            score += bonus
            reasons.append(f"골드 +{bonus:.1f}")

        if "유물" in text and ("얻" in text or "획득" in text):
            bonus = 1.3
            score += bonus
            reasons.append(f"유물 +{bonus:.1f}")

        if "체력" in text and "잃" in text:
            amount = max(numbers) if numbers else 5
            penalty = min(2.6, amount * (0.08 if hp_pct < 0.55 else 0.05))
            score -= penalty
            reasons.append(f"체력비용 -{penalty:.1f}")

        if "최대 체력" in text and "잃" in text:
            penalty = min(2.0, (max(numbers) if numbers else 3) * 0.25)
            score -= penalty
            reasons.append(f"최대체력손실 -{penalty:.1f}")

        if ("서투름" in text or "저주" in text) and ("추가" in text or "덱" in text):
            penalty = 1.9
            score -= penalty
            reasons.append(f"불순물추가 -{penalty:.1f}")

        if "제거" in text and "덱" in text:
            bonus = 1.4
            score += bonus
            reasons.append(f"덱정리 +{bonus:.1f}")

        if not reasons:
            reasons.append("현재화면기준")

        options.append(
            {
                "id": f"OCR_OPTION_{idx}",
                "title": title,
                "description": description,
                "score": round(score, 2),
                "reasons": reasons[:4],
                "position": choice.position,
                "screen_anchor": choice.screen_anchor,
            }
        )

    best_idx = max(range(len(options)), key=lambda i: options[i]["score"])
    return {
        "event_id": "OCR_EVENT_CHOICES",
        "event_name": "이벤트 선택",
        "page_id": "OCR",
        "match_score": 0.55,
        "options": options,
        "best_idx": best_idx,
    }


# 전역 상태
class TrackerState:
    def __init__(self):
        self.run_state: RunState | None = None
        self.recommendations: list = []
        self.event_recommendation: dict | None = None
        self.ocr_status: str = "초기화 중..."
        self.window_id: int | None = None
        self.cards_db: list = []
        self.relics_db: list = []
        self.potions_db: list = []
        self.event_pages: list[dict] = []
        self.event_priors: dict[str, dict] = {}
        self.ocr_reader = None
        self.last_mtime: float = 0.0
        self.last_card_screen: bool = False
        self.last_combat_screen: bool = False
        self.last_event_scan: float = 0.0
        self.last_combat_scan: float = 0.0
        self.combat_hold_until: float = 0.0
        self.last_shop_scan: float = 0.0
        self.combat_advice: list[str] = []
        self.combat_cards: list = []
        self.combat_sequence: list[dict] = []
        self.current_stars: int | None = None
        self.choice_prompt: str = ""
        self.choice_pick_count: int = 1
        self.shop_recommendation: dict | None = None
        self.map_recommendation: dict | None = None
        self.clients: list[WebSocket] = []
        self._tracking = True
        self.last_live_map_refresh: float = 0.0

    def map_tracking_active(self) -> bool:
        return bool(
            self.map_recommendation
            and self.map_recommendation.get("anchor_screen")
            and not self.last_card_screen
            and not self.last_combat_screen
            and not self.event_recommendation
        )

    def to_dict(self) -> dict:
        state = self.run_state
        run_data = None
        if state:
            card_counts: dict[str, int] = {}
            for card in state.deck:
                name = card.display_id
                card_counts[name] = card_counts.get(name, 0) + 1

            run_data = {
                "player_id": state.player_id,
                "build_id": state.build_id,
                "character": state.character,
                "current_hp": state.current_hp,
                "max_hp": state.max_hp,
                "max_energy": state.max_energy,
                "current_stars": self.current_stars,
                "gold": state.gold,
                "act": state.act + 1,
                "floor": state.floor,
                "ascension": state.ascension,
                "game_mode": state.game_mode,
                "modifiers": state.modifiers,
                "seed": state.seed,
                "deck": [{"name": n, "count": c} for n, c in sorted(card_counts.items())],
                "deck_size": len(state.deck),
                "relics": [r.replace("RELIC.", "") for r in state.relics],
                "potions": [p.replace("POTION.", "") for p in state.potions],
                "max_potion_slots": state.max_potion_slots,
                "room_type": state.room_type,
                "room_model_id": state.room_model_id,
                "monster_ids": [m.replace("MONSTER.", "") for m in state.monster_ids],
            }

        recs = []
        for item in self.recommendations:
            position = None
            screen_anchor = None
            if len(item) == 6:
                card, match_pct, game_score, reasons, position, screen_anchor = item
            elif len(item) == 5:
                card, match_pct, game_score, reasons, position = item
            elif len(item) == 4:
                card, match_pct, game_score, reasons = item
            else:
                card, match_pct, game_score = item
                reasons = []
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
                "reasons": reasons,
                "position": position,
                "screen_anchor": screen_anchor,
            })

        best_idx = -1
        if recs:
            best_idx = max(range(len(recs)), key=lambda i: recs[i]["score"])
        reward_best_indices = []
        if recs:
            pick_count = max(1, self.choice_pick_count)
            reward_best_indices = sorted(
                range(len(recs)),
                key=lambda i: (recs[i]["score"], recs[i]["match_pct"]),
                reverse=True,
            )[:pick_count]

        event_data = None
        if self.event_recommendation:
            event_data = {
                "event_id": self.event_recommendation["event_id"],
                "event_name": self.event_recommendation["event_name"],
                "page_id": self.event_recommendation["page_id"],
                "match_pct": round(self.event_recommendation["match_score"] * 100),
                "best_idx": self.event_recommendation["best_idx"],
                "options": [
                    {
                        "id": option["id"],
                        "title": option["title"],
                        "description": option["description"],
                        "score": option["score"],
                        "reasons": option["reasons"],
                        "position": option.get("position"),
                        "screen_anchor": option.get("screen_anchor"),
                    }
                    for option in self.event_recommendation["options"]
                ],
            }

        return {
            "run": run_data,
            "recommendations": recs,
            "best_idx": best_idx,
            "reward_best_indices": reward_best_indices,
            "choice_prompt": self.choice_prompt,
            "choice_pick_count": self.choice_pick_count,
            "event_recommendation": event_data,
            "combat_advice": self.combat_advice,
            "combat_cards": [
                {
                    "name": card["name"],
                    "id": card["id"],
                    "cost": card.get("cost", "?"),
                    "rarity": card.get("rarity", ""),
                    "rarity_key": card.get("rarity_key", ""),
                    "type": card.get("type", ""),
                    "description": clean_description(card.get("description", "")),
                    "match_pct": round(match_pct * 100),
                    "score": game_score,
                    "reasons": reasons,
                    "position": position,
                    "target_label": target_label,
                    "target_reason": target_reason,
                }
                for card, match_pct, game_score, reasons, position, target_label, target_reason in self.combat_cards
            ],
            "combat_best_idx": (
                0
                if self.combat_cards
                else -1
            ),
            "combat_sequence": self.combat_sequence,
            "current_stars": self.current_stars,
            "shop_recommendation": self.shop_recommendation,
            "map_recommendation": self.map_recommendation,
            "ocr_status": self.ocr_status,
            "connected": self.window_id is not None,
            "ui_offsets": _load_ui_offsets(),
        }


def _load_ui_offsets() -> dict:
    import os
    offsets_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ui_offsets.json")
    if os.path.exists(offsets_path):
        with open(offsets_path) as f:
            return json.load(f)
    return {}


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


def score_detected_cards(detected: list, state: RunState) -> list[tuple[dict, float, float, list[str], int]]:
    """현재 덱 분석을 한 번만 해서 OCR 카드 후보들을 점수화."""
    context = build_scoring_context(state, tracker.cards_db)
    recommendations = []
    for det in detected:
        if not det.ocr_text:
            continue
        matches = fuzzy_match(det.ocr_text, tracker.cards_db, threshold=0.3, character=state.character.lower())
        if not matches:
            continue
        card, match_score = matches[0]
        game_score, reasons = score_card_v2(
            card,
            state,
            tracker.cards_db,
            context=context,
        )
        recommendations.append((card, match_score, game_score, reasons, det.position))
    return recommendations


def score_deck_for_removal(state: RunState) -> list[tuple[dict, float, float, list[str], int]]:
    """세이브 파일 덱 기반 제거 추천. OCR 불필요."""
    db_index = {c["id"]: c for c in tracker.cards_db}
    context = build_scoring_context(state, tracker.cards_db)
    scored = []
    for i, card in enumerate(state.deck):
        card_id = card.id.replace("CARD.", "")
        db_card = db_index.get(card_id)
        if not db_card:
            continue
        game_score, reasons = score_card_v2(db_card, state, tracker.cards_db, context=context)
        scored.append((db_card, 1.0, game_score, reasons, i))
    # 낮은 점수 순 정렬 (제거 우선)
    scored.sort(key=lambda x: x[2])
    return scored


def parse_choice_pick_count(prompt: str) -> int:
    match = _CHOICE_COUNT_RE.search(prompt)
    if not match:
        return 1
    try:
        return max(1, min(int(match.group(1)), 5))
    except ValueError:
        return 1


def detect_card_choice_screen(img, state: RunState) -> tuple[str, int, list[tuple[dict, float, float, list[str], int, dict]]]:
    """다중 카드 선택 화면 OCR + 추천."""
    prompt = ocr_card_choice_prompt(img, _lazy_easyocr())
    if "선택" not in prompt or "카드" not in prompt:
        return "", 1, []

    lines = ocr_shop_text_lines(img, _lazy_easyocr())
    if not lines:
        return prompt, parse_choice_pick_count(prompt), []

    context = build_scoring_context(state, tracker.cards_db)
    cluster_centers: list[float] = []

    def resolve_choice_position(cx: float) -> int:
        for idx, center in enumerate(cluster_centers):
            if abs(center - cx) <= 0.08:
                cluster_centers[idx] = (center + cx) / 2
                return idx
        cluster_centers.append(cx)
        cluster_centers.sort()
        return min(range(len(cluster_centers)), key=lambda idx: abs(cluster_centers[idx] - cx))

    best_by_card: dict[str, tuple[dict, float, float, list[str], int, dict]] = {}
    for line in lines:
        text = line.text.strip()
        if len(text) < 2:
            continue
        matches = fuzzy_match(text, tracker.cards_db, threshold=0.58)
        if not matches:
            continue
        card, match_score = matches[0]
        game_score, reasons = score_card_v2(
            card,
            state,
            tracker.cards_db,
            context=context,
        )
        position = resolve_choice_position(line.cx)
        screen_anchor = {
            "x": 0.10 + line.cx * 0.80,
            "y": 0.18 + line.cy * 0.74,
        }
        current = best_by_card.get(card["id"])
        candidate = (card, match_score, game_score, reasons, position, screen_anchor)
        if current is None or (match_score, game_score) > (current[1], current[2]):
            best_by_card[card["id"]] = candidate

    recommendations = sorted(
        best_by_card.values(),
        key=lambda item: (item[2], item[1]),
        reverse=True,
    )
    if len(recommendations) < 4:
        return "", 1, []
    return prompt, parse_choice_pick_count(prompt), recommendations[:10]


def score_detected_combat_cards(img, state: RunState) -> tuple[list[tuple[dict, float, float, list[str], int, str, str]], list[dict]]:
    """전투 손패 OCR + 추천."""
    detected = ocr_combat_hand_cards(img, _lazy_easyocr())
    if not detected:
        return [], []
    available_stars = None
    if state.character == "REGENT":
        available_stars = ocr_regent_star_count(img, _lazy_easyocr())
        tracker.current_stars = available_stars
    return recommend_combat_hand(
        detected,
        state,
        tracker.cards_db,
        fuzzy_match=fuzzy_match,
        available_stars=available_stars,
    )


def detect_event_recommendation(img, state: RunState) -> dict | None:
    """이벤트 화면 OCR + 선택지 추천."""
    # Vision OCR로 이벤트 텍스트 읽기 (EasyOCR보다 빠르고 정확)
    import vision_ocr
    h, w = img.shape[:2]
    event_area = img[int(h * 0.50):int(h * 0.90), int(w * 0.10):int(w * 0.90)]
    ocr_text = vision_ocr.ocr_region(event_area)
    visible_choices = []
    if not ocr_text.strip():
        return None
    recommendation = recommend_event_choices(
        ocr_text,
        state,
        tracker.cards_db,
        event_pages=tracker.event_pages,
        event_priors=tracker.event_priors,
    )
    if recommendation and recommendation["match_score"] >= 0.5:
        return _attach_choice_anchors(recommendation, visible_choices)

    if state.act == 0 and state.floor <= 1:
        opening_choices = ocr_opening_choices(img, _lazy_easyocr())
        if opening_choices:
            return _attach_choice_anchors(
                recommend_opening_choices(
                    opening_choices,
                    state,
                    tracker.cards_db,
                ),
                opening_choices,
            )
    return _fallback_event_choice_recommendation(visible_choices, state)


def is_shop_room(state: RunState | None) -> bool:
    """현재 방이 상점인지 느슨하게 판정."""
    if state is None:
        return False
    room_type = (state.room_type or "").lower()
    room_model = (state.room_model_id or "").lower()
    return (
        "shop" in room_type
        or "merchant" in room_type
        or "shop" in room_model
        or "merchant" in room_model
    )


def detect_shop_recommendation(img, state: RunState) -> dict | None:
    """상점 화면 OCR + 구매 추천."""
    if detect_combat_screen(img):
        return None

    lines = ocr_shop_text_lines(img, _lazy_easyocr())
    if not lines:
        return None
    recommendation = recommend_shop_purchases(
        state,
        lines,
        cards_db=tracker.cards_db,
        relics_db=tracker.relics_db,
        potions_db=tracker.potions_db,
    )
    if not recommendation:
        return None

    # 세이브의 room_type이 비는 경우가 있어서, 실제 상점으로 보일 때만 채택한다.
    confident = [
        item for item in recommendation["items"]
        if item["kind"] == "remove" or item["match_pct"] >= 65
    ]
    priced_lines = sum(1 for line in lines if any(ch.isdigit() for ch in line.text))
    if len(confident) >= 2 and priced_lines >= 2:
        return recommendation
    return None


def refresh_map_recommendation(state: RunState, img=None):
    """현재 세이브 + 화면 기준 맵 추천 상태 갱신."""
    anchor_screen = detect_map_current_anchor(img) if img is not None else None
    node_rows = detect_map_node_rows(img) if img is not None else None
    tracker.map_recommendation = recommend_map_route(
        state,
        tracker.cards_db,
        anchor_screen=anchor_screen,
        node_rows=node_rows,
    )


def refresh_live_map_if_needed(*, force: bool = False):
    """클라이언트가 붙어 있을 때 맵 좌표를 현재 프레임 기준으로 갱신."""
    state = tracker.run_state
    if state is None or tracker.window_id is None:
        return
    if tracker.last_card_screen or tracker.last_combat_screen or tracker.event_recommendation:
        return

    now = time.time()
    if not force and now - tracker.last_live_map_refresh < (1.0 / 60.0):
        return

    img = capture_window(tracker.window_id)
    if img is None:
        return

    refresh_map_recommendation(state, img)
    tracker.last_live_map_refresh = now


def _lazy_easyocr():
    """EasyOCR 지연 로딩 (무거움: ~7GB RAM). 필요할 때만 호출."""
    if tracker.ocr_reader is None:
        import easyocr
        tracker.ocr_status = "EasyOCR 로딩..."
        tracker.ocr_reader = easyocr.Reader(["ko", "en"], gpu=False, verbose=False)
        tracker.ocr_status = "준비 완료"
    return tracker.ocr_reader


def tracking_loop():
    """백그라운드 트래킹 루프."""
    import vision_ocr
    tracker.ocr_status = "Vision OCR 초기화..."
    vision_ocr.warmup()
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
                        refresh_map_recommendation(state)

            # 화면 캡처 + 카드 보상 감지
            if tracker.window_id:
                img = capture_window(tracker.window_id)
                if img is not None:
                    state = tracker.run_state
                    is_card_screen = detect_card_reward_screen(img)
                    choice_prompt = ""
                    choice_pick_count = 1
                    choice_recommendations = []
                    if state and is_card_screen:
                        choice_prompt, choice_pick_count, choice_recommendations = detect_card_choice_screen(img, state)
                    if state and is_card_screen and (is_shop_room(state) or not (state.room_type or "").strip()):
                        shop_candidate = None if choice_recommendations else detect_shop_recommendation(img, state)
                        if shop_candidate:
                            tracker.recommendations = []
                            tracker.combat_advice = []
                            tracker.combat_cards = []
                            tracker.combat_sequence = []
                            tracker.current_stars = None
                            tracker.event_recommendation = None
                            tracker.choice_prompt = ""
                            tracker.choice_pick_count = 1
                            tracker.shop_recommendation = shop_candidate
                            is_card_screen = False

                    if is_card_screen and not tracker.last_card_screen:
                        tracker.ocr_status = "카드 인식 중..."
                        # 제거 화면: 세이브 덱 기반 스코어링 (OCR 불필요)
                        is_removal = "제거" in (choice_prompt or "")
                        if is_removal and state:
                            tracker.recommendations = score_deck_for_removal(state)
                            tracker.choice_prompt = choice_prompt
                            tracker.choice_pick_count = choice_pick_count
                        elif choice_recommendations:
                            tracker.recommendations = choice_recommendations
                            tracker.choice_prompt = choice_prompt
                            tracker.choice_pick_count = choice_pick_count
                        else:
                            regions = extract_card_regions(img)
                            detected = vision_ocr.ocr_card_names(regions)
                            if state:
                                tracker.recommendations = score_detected_cards(detected, state)
                            tracker.choice_prompt = ""
                            tracker.choice_pick_count = 1
                        tracker.combat_advice = []
                        tracker.combat_cards = []
                        tracker.combat_sequence = []
                        tracker.current_stars = None
                        tracker.shop_recommendation = None
                        tracker.event_recommendation = None
                        tracker.ocr_status = "준비 완료"
                    elif not is_card_screen and tracker.last_card_screen:
                        tracker.recommendations = []
                        tracker.choice_prompt = ""
                        tracker.choice_pick_count = 1
                    tracker.last_card_screen = is_card_screen

                    # 전투 화면 감지 + 조언 생성
                    if not is_card_screen:
                        now = time.time()
                        raw_combat = detect_combat_screen(img)
                        if raw_combat:
                            tracker.combat_hold_until = now + 1.0
                        is_combat = raw_combat or now < tracker.combat_hold_until
                        if is_combat:
                            state = tracker.run_state
                            if state and (
                                not tracker.last_combat_screen
                                or (raw_combat and now - tracker.last_combat_scan >= 0.35)
                            ):
                                tracker.combat_advice = generate_combat_advice(
                                    state, tracker.cards_db
                                )
                                tracker.combat_cards, tracker.combat_sequence = score_detected_combat_cards(img, state)
                                tracker.last_combat_scan = now
                            tracker.shop_recommendation = None
                            tracker.event_recommendation = None
                        elif not is_combat and tracker.last_combat_screen:
                            tracker.combat_advice = []
                            tracker.combat_cards = []
                            tracker.combat_sequence = []
                            tracker.current_stars = None
                        tracker.last_combat_screen = is_combat
                        if not is_combat:
                            tracker.combat_advice = []
                            tracker.combat_cards = []
                            tracker.combat_sequence = []
                            tracker.current_stars = None
                            if state:
                                refresh_map_recommendation(state, img)
                                shop_candidate = None
                                room_known = bool((state.room_type or "").strip())
                                if is_shop_room(state) or not room_known:
                                    if time.time() - tracker.last_shop_scan >= 0.8:
                                        tracker.last_shop_scan = time.time()
                                        shop_candidate = detect_shop_recommendation(img, state)
                                    elif tracker.shop_recommendation:
                                        shop_candidate = tracker.shop_recommendation

                                # 이벤트를 먼저 체크 (상점보다 우선)
                                event_candidate = None
                                if time.time() - tracker.last_event_scan >= 2.5:
                                    tracker.last_event_scan = time.time()
                                    event_candidate = detect_event_recommendation(img, state)

                                if event_candidate:
                                    tracker.event_recommendation = event_candidate
                                    tracker.shop_recommendation = None
                                elif shop_candidate:
                                    tracker.recommendations = []
                                    tracker.shop_recommendation = shop_candidate
                                    tracker.event_recommendation = None
                                    tracker.choice_prompt = ""
                                    tracker.choice_pick_count = 1
                                else:
                                    tracker.shop_recommendation = None
                                    tracker.event_recommendation = None
                        else:
                            tracker.shop_recommendation = None
                            tracker.event_recommendation = None
                    else:
                        tracker.last_combat_screen = False
                        tracker.combat_advice = []
                        tracker.combat_cards = []
                        tracker.combat_sequence = []
                        tracker.current_stars = None
                        tracker.choice_prompt = ""
                        tracker.choice_pick_count = 1
                        tracker.shop_recommendation = None
                        tracker.event_recommendation = None

            time.sleep(1.0 / 60.0 if tracker.map_tracking_active() else 0.2)
        except Exception as e:
            tracker.ocr_status = f"Error: {e}"
            time.sleep(3)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 시작/종료 시 트래킹 루프 관리."""
    tracker.cards_db = load_card_db()
    tracker.relics_db = load_relic_db()
    tracker.potions_db = load_potion_db()
    tracker.event_pages = flatten_event_pages(load_event_db())
    tracker.event_priors = load_event_priors()
    thread = threading.Thread(target=tracking_loop, daemon=True)
    thread.start()

    # 주기적으로 클라이언트에 상태 전송
    async def push_loop():
        last_message = ""
        while tracker._tracking:
            if tracker.clients:
                refresh_live_map_if_needed()
                payload = tracker.to_dict()
                message = json.dumps(payload, ensure_ascii=False)
                if message != last_message:
                    last_message = message
                    disconnected = []
                    for ws in tracker.clients:
                        try:
                            await ws.send_text(message)
                        except Exception:
                            disconnected.append(ws)
                    for ws in disconnected:
                        tracker.clients.remove(ws)
            await asyncio.sleep(1.0 / 60.0 if tracker.clients else 0.15)

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
                if tracker.window_id:
                    img = capture_window(tracker.window_id)
                    if img is not None:
                        tracker.ocr_status = "수동 스캔 중..."
                        state = tracker.run_state
                        is_card_screen = detect_card_reward_screen(img)
                        choice_prompt = ""
                        choice_pick_count = 1
                        choice_recommendations = []
                        if state and is_card_screen:
                            choice_prompt, choice_pick_count, choice_recommendations = detect_card_choice_screen(img, state)
                        raw_combat = detect_combat_screen(img) if not is_card_screen else False
                        now = time.time()
                        if raw_combat:
                            tracker.combat_hold_until = now + 1.0
                        is_combat = raw_combat or (not is_card_screen and now < tracker.combat_hold_until)
                        if state and is_card_screen and (is_shop_room(state) or not (state.room_type or "").strip()):
                            tracker.shop_recommendation = None if choice_recommendations else detect_shop_recommendation(img, state)
                            if tracker.shop_recommendation:
                                is_card_screen = False
                        if state and is_card_screen:
                            if choice_recommendations:
                                tracker.recommendations = choice_recommendations
                                tracker.choice_prompt = choice_prompt
                                tracker.choice_pick_count = choice_pick_count
                            else:
                                regions = extract_card_regions(img)
                                detected = vision_ocr.ocr_card_names(regions)
                                tracker.recommendations = score_detected_cards(detected, state)
                                tracker.choice_prompt = ""
                                tracker.choice_pick_count = 1
                            tracker.combat_advice = []
                            tracker.combat_cards = []
                            tracker.combat_sequence = []
                            tracker.current_stars = None
                            tracker.shop_recommendation = None
                            tracker.event_recommendation = None
                            refresh_map_recommendation(state, img)
                        elif state and not is_combat:
                            tracker.recommendations = []
                            tracker.choice_prompt = ""
                            tracker.choice_pick_count = 1
                            tracker.combat_advice = []
                            tracker.combat_cards = []
                            tracker.combat_sequence = []
                            tracker.current_stars = None
                            tracker.shop_recommendation = detect_shop_recommendation(img, state)
                            if tracker.shop_recommendation:
                                tracker.event_recommendation = None
                            else:
                                tracker.event_recommendation = detect_event_recommendation(img, state)
                            refresh_map_recommendation(state, img)
                        elif state and is_combat:
                            tracker.shop_recommendation = None
                            tracker.combat_advice = generate_combat_advice(state, tracker.cards_db)
                            tracker.combat_cards, tracker.combat_sequence = score_detected_combat_cards(img, state)
                        tracker.ocr_status = "준비 완료"
                        await broadcast(tracker.to_dict())
    except WebSocketDisconnect:
        tracker.clients.remove(websocket)


@app.get("/")
async def root():
    return {"status": "STS2 Tracker Server running"}


@app.get("/debug")
async def debug_state():
    """실시간 서버 상태 디버그."""
    d = tracker.to_dict()
    return {
        "connected": d.get("connected"),
        "ocr_status": d.get("ocr_status"),
        "has_run": d.get("run") is not None,
        "recommendations": len(d.get("recommendations", [])),
        "recs_preview": [{"name": r["name"], "score": r["score"]} for r in (d.get("recommendations") or [])[:5]],
        "has_event": d.get("event_recommendation") is not None,
        "event_name": (d.get("event_recommendation") or {}).get("event_name"),
        "combat_advice": len(d.get("combat_advice", [])),
        "has_shop": d.get("shop_recommendation") is not None,
        "choice_prompt": d.get("choice_prompt", ""),
        "choice_pick_count": d.get("choice_pick_count", 1),
        "ui_offsets": d.get("ui_offsets", {}),
        "overlay_clients": len(tracker.clients),
    }


@app.get("/api/screenshot")
async def api_screenshot():
    """현재 게임 화면 스크린샷 반환."""
    import cv2
    from fastapi.responses import Response
    if tracker.window_id is None:
        return Response(content=b"", media_type="image/png", status_code=404)
    img = capture_window(tracker.window_id)
    if img is None:
        return Response(content=b"", media_type="image/png", status_code=404)
    _, buf = cv2.imencode(".png", img)
    return Response(content=buf.tobytes(), media_type="image/png")


@app.post("/api/save-offsets")
async def api_save_offsets(request: Request):
    """UI 오프셋 저장."""
    import json as _json
    import os
    body = await request.json()
    offsets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(offsets_dir, exist_ok=True)
    offsets_path = os.path.join(offsets_dir, "ui_offsets.json")
    with open(offsets_path, "w") as f:
        _json.dump(body, f, indent=2)
    return {"saved": True, "offsets": body}


@app.get("/debug/overlay")
async def debug_overlay():
    """오버레이에 전송되는 실제 데이터 확인."""
    d = tracker.to_dict()
    recs = d.get("recommendations", [])
    prompt = d.get("choice_prompt", "")
    is_removal = "제거" in prompt
    pick_count = d.get("choice_pick_count", 1)

    if is_removal:
        sorted_recs = sorted(recs, key=lambda r: r["score"])
        best = [r["name"] for r in sorted_recs[:pick_count]]
    else:
        sorted_recs = recs
        best_idx = d.get("best_idx", -1)
        best = [recs[best_idx]["name"]] if best_idx >= 0 and best_idx < len(recs) else []

    return {
        "mode": "제거" if is_removal else "보상",
        "choice_prompt": prompt,
        "pick_count": pick_count,
        "best_picks": best,
        "cards_in_order": [{"name": r["name"], "score": r["score"]} for r in sorted_recs],
    }


@app.get("/calibrate")
async def calibrate_page():
    """UI 위치 조정 페이지."""
    import os
    calibrate_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibrate.html")
    with open(calibrate_path, encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(content=html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9999, log_level="warning")
