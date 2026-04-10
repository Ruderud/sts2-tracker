"""STS2 트래커 메인 - 세이브 감시 + 화면 캡처 + 카드 추천."""

import time
import sys
from pathlib import Path

from save_parser import parse_save, find_save_file, RunState
from card_db import load_card_db, fuzzy_match
from screen_capture import find_game_window, capture_window, detect_card_reward_screen, extract_card_regions, ocr_card_names


def print_run_state(state: RunState):
    """현재 런 상태 출력."""
    print(f"\n{'='*50}")
    print(f"  {state.character} | HP: {state.current_hp}/{state.max_hp} | Gold: {state.gold}")
    print(f"  Act {state.act + 1} | Floor {state.floor} | Seed: {state.seed}")
    print(f"{'='*50}")
    print(f"\n  Deck ({len(state.deck)} cards):")
    # 카드별 카운트
    card_counts: dict[str, int] = {}
    for card in state.deck:
        name = card.display_id
        card_counts[name] = card_counts.get(name, 0) + 1
    for name, count in sorted(card_counts.items()):
        suffix = f" x{count}" if count > 1 else ""
        print(f"    {name}{suffix}")

    print(f"\n  Relics ({len(state.relics)}):")
    for relic in state.relics:
        print(f"    {relic.replace('RELIC.', '')}")


def score_card(card: dict, state: RunState) -> float:
    """카드 점수 계산 (간단한 휴리스틱)."""
    score = 0.0
    card_id = card["id"]
    rarity = card.get("rarity_key", "")
    card_type = card.get("type_key", "")

    # 희귀도 보너스
    rarity_bonus = {"Rare": 3.0, "Uncommon": 1.5, "Common": 0.5, "Event": 1.0}
    score += rarity_bonus.get(rarity, 0)

    # 덱 크기 패널티 (덱이 클수록 카드 추가에 신중)
    deck_size = len(state.deck)
    if deck_size > 15:
        score -= (deck_size - 15) * 0.2

    # 공격/스킬 밸런스
    deck_attacks = sum(1 for c in state.deck if "STRIKE" in c.id or "STAR" in c.id)
    deck_skills = sum(1 for c in state.deck if "DEFEND" in c.id)
    if card_type == "Attack" and deck_attacks > deck_skills + 2:
        score -= 1.0
    elif card_type == "Skill" and deck_skills > deck_attacks + 2:
        score -= 1.0

    # 파워 카드 보너스 (초반에 특히 좋음)
    if card_type == "Power":
        score += 2.0
        if state.floor <= 5:
            score += 1.0

    # 0코스트 카드 보너스
    cost = card.get("cost")
    if cost == 0:
        score += 1.0

    # 피해/방어 수치 반영
    damage = card.get("damage") or 0
    block = card.get("block") or 0
    hit_count = card.get("hit_count") or 1
    score += (damage * hit_count + block) * 0.05

    # 카드 드로우 보너스
    draw = card.get("cards_draw") or 0
    score += draw * 1.0

    return round(score, 1)


def recommend_cards(detected_cards: list, cards_db: list, state: RunState):
    """감지된 카드에 대한 추천 출력."""
    print(f"\n{'─'*50}")
    print("  📋 카드 보상 감지!")
    print(f"{'─'*50}")

    recommendations = []
    for det in detected_cards:
        if not det.ocr_text:
            continue
        matches = fuzzy_match(det.ocr_text, cards_db, threshold=0.3)
        if matches:
            card, match_score = matches[0]
            game_score = score_card(card, state)
            recommendations.append((det, card, match_score, game_score))
            rarity = card.get("rarity", "")
            cost = card.get("cost", "?")
            print(f"\n  Card {det.position + 1}: {card['name']} ({card['id']})")
            print(f"    OCR: '{det.ocr_text}' (match: {match_score:.0%})")
            print(f"    Cost: {cost} | Rarity: {rarity} | Score: {game_score}")
            desc = card.get("description", "").replace("\n", " ")
            print(f"    {desc[:80]}")

    if recommendations:
        best = max(recommendations, key=lambda r: r[3])
        print(f"\n  ★ 추천: {best[1]['name']} (Score: {best[3]})")
    print(f"{'─'*50}")


def main():
    print("STS2 Tracker v0.1")
    print("Loading card database...")
    cards_db = load_card_db()
    print(f"Loaded {len(cards_db)} cards")

    # EasyOCR 초기화 (느림 - 한번만)
    print("Initializing OCR engine...")
    import easyocr
    reader = easyocr.Reader(["ko", "en"], gpu=False, verbose=False)
    print("OCR ready")

    # 게임 찾기
    window_id = find_game_window()
    if window_id is None:
        print("Game not found. Waiting...")

    save_path = find_save_file()
    last_mtime = 0.0
    last_card_screen = False

    print("\nTracking... (Ctrl+C to stop)\n")

    while True:
        try:
            # 1) 게임 창 확인
            if window_id is None:
                window_id = find_game_window()
                if window_id is None:
                    time.sleep(2)
                    continue

            # 2) 세이브 파일 변화 감지
            if save_path is None:
                save_path = find_save_file()
            if save_path and save_path.exists():
                mtime = save_path.stat().st_mtime
                if mtime != last_mtime:
                    last_mtime = mtime
                    state = parse_save(save_path)
                    if state:
                        print_run_state(state)

            # 3) 화면 캡처 + 카드 보상 감지
            img = capture_window(window_id)
            if img is not None:
                is_card_screen = detect_card_reward_screen(img)
                if is_card_screen and not last_card_screen:
                    # 카드 보상 화면 새로 진입
                    regions = extract_card_regions(img)
                    detected = ocr_card_names(regions, reader)
                    state = parse_save(save_path)
                    if state:
                        recommend_cards(detected, cards_db, state)
                last_card_screen = is_card_screen

            time.sleep(1)

        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()
