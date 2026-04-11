"""STS2 전투 조언 모듈 - 덱 분석 기반 일반 전투 팁 생성."""

from functools import lru_cache

from save_parser import RunState
from recommender import analyze_deck, build_scoring_context, score_card


_SUPPORT_KEYWORDS = {
    "PRIEST",
    "OPERATOR",
    "ENTOMANCER",
    "NEXUS",
    "DEMON",
    "CULTIST",
    "PRISM",
    "SHAMAN",
    "MAGE",
    "SEER",
    "TOTEM",
}
_THREAT_KEYWORDS = {
    "THIEVING",
    "HOPPER",
    "BOMBER",
    "TRACKER",
    "RAIDER",
    "ASSASSIN",
    "SNIPER",
}
_SWARM_KEYWORDS = {
    "TOADPOLE",
    "INKLET",
    "MYTE",
    "EGG",
    "SLIME_S",
    "LOUSE",
    "MINION",
}
_TANK_KEYWORDS = {
    "SHIELD",
    "EXOSKELETON",
    "SEGMENT_FRONT",
    "CONSTRUCT",
    "GIANT",
    "GOLEM",
    "GUARDIAN",
}


def _build_deck_candidate_pool(state: RunState, cards_db: list[dict], *, card_index: dict[str, dict]) -> tuple[list[dict], set[str]]:
    deck_ids: set[str] = set()
    for card in state.deck:
        card_id = card.id.replace("CARD.", "")
        if card_id in card_index:
            deck_ids.add(card_id)
    return [card_index[card_id] for card_id in deck_ids], deck_ids


def _match_combat_hand_card(
    ocr_text: str,
    *,
    state: RunState,
    cards_db: list[dict],
    card_index: dict[str, dict],
    fuzzy_match,
) -> tuple[dict, float, str] | None:
    deck_pool, deck_ids = _build_deck_candidate_pool(state, cards_db, card_index=card_index)
    deck_matches = fuzzy_match(ocr_text, deck_pool, threshold=0.18) if deck_pool else []
    full_matches = fuzzy_match(ocr_text, cards_db, threshold=0.22)

    best_deck = deck_matches[0] if deck_matches else None
    best_full = full_matches[0] if full_matches else None

    if best_deck and (
        not best_full
        or best_full[0]["id"] in deck_ids
        or best_deck[1] >= best_full[1] - 0.12
        or best_full[1] < 0.72
    ):
        return best_deck[0], float(best_deck[1]), "deck"

    if best_full:
        return best_full[0], float(best_full[1]), "generated"

    return None


def _effective_play_cost(card: dict, energy: int) -> int:
    if card.get("is_x_cost"):
        return max(1, energy)
    cost = card.get("cost")
    if cost is None:
        return 0
    try:
        return max(int(cost), 0)
    except (TypeError, ValueError):
        return 0


def _effective_star_cost(card: dict) -> int:
    star_cost = card.get("star_cost")
    if star_cost is None:
        return 0
    try:
        return max(int(star_cost), 0)
    except (TypeError, ValueError):
        return 0


def _star_gain(card: dict) -> int:
    vars_data = card.get("vars") or {}
    value = vars_data.get("Stars")
    if value is None:
        return 0
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _sequence_step_value(entry: dict, remaining_entries: list[dict], energy_before: int, step: int) -> float:
    card = entry["card"]
    value = float(entry["score"]) * (0.97 ** step)
    draw = card.get("cards_draw") or 0
    energy_gain = card.get("energy_gain") or 0
    card_type = card.get("type_key", "")

    if card_type == "Power":
        payoff = sum(
            0.18
            for candidate in remaining_entries
            if candidate["card"].get("type_key") != "Power"
        )
        value += payoff

    if draw > 0:
        value += min(len(remaining_entries), draw) * 0.26

    if energy_gain > 0:
        value += min(len(remaining_entries), energy_gain + 1) * 0.24

    star_gain = _star_gain(card)
    star_cost = _effective_star_cost(card)
    if star_gain > 0:
        value += star_gain * 0.42
    if star_cost > 0:
        value += min(star_cost, 4) * 0.08

    if card.get("is_x_cost"):
        value += max(energy_before - 1, 0) * 0.18

    if (card.get("block") or 0) > 0 and step > 0:
        value -= 0.12

    return value


def _simulate_combat_sequence(entries: list[dict], max_energy: int, available_stars: int | None) -> tuple[list[int], float]:
    if not entries:
        return [], 0.0

    capped_energy = max(0, min(max_energy, 6))
    capped_stars = 20 if available_stars is None else max(0, min(available_stars, 20))

    @lru_cache(maxsize=None)
    def search(mask: int, energy: int, stars: int, step: int) -> tuple[float, tuple[int, ...]]:
        best_score = 0.0
        best_sequence: tuple[int, ...] = ()

        for idx, entry in enumerate(entries):
            if not (mask & (1 << idx)):
                continue

            card = entry["card"]
            play_cost = _effective_play_cost(card, energy)
            star_cost = _effective_star_cost(card)
            if play_cost > energy:
                continue
            if star_cost > stars:
                continue

            next_mask = mask ^ (1 << idx)
            next_energy = max(0, energy - play_cost) + int(card.get("energy_gain") or 0)
            next_energy = min(next_energy, 8)
            next_stars = max(0, stars - star_cost + _star_gain(card))
            next_stars = min(next_stars, 20)
            remaining_entries = [entries[j] for j in range(len(entries)) if next_mask & (1 << j)]
            immediate = _sequence_step_value(entry, remaining_entries, energy, step)
            future_score, future_sequence = search(next_mask, next_energy, next_stars, step + 1)
            total = immediate + future_score
            if total > best_score:
                best_score = total
                best_sequence = (idx,) + future_sequence

        return best_score, best_sequence

    score, sequence = search((1 << len(entries)) - 1, capped_energy, capped_stars, 0)
    return list(sequence), score


def _monster_slot_prefix(index: int, total: int) -> str:
    if total == 1:
        return "현재"
    if total == 2:
        return ["왼쪽", "오른쪽"][index]
    if total == 3:
        return ["왼쪽", "가운데", "오른쪽"][index]
    return f"{index + 1}번"


def _classify_monster_role(monster_id: str) -> tuple[str, str, float]:
    name = monster_id.replace("MONSTER.", "").upper()
    if any(keyword in name for keyword in _SUPPORT_KEYWORDS):
        return "지원형 적", "버프/스케일링 차단", 4.2
    if any(keyword in name for keyword in _THREAT_KEYWORDS):
        return "위협 적", "방치 손실이 큼", 3.8
    if any(keyword in name for keyword in _SWARM_KEYWORDS):
        return "약한 적", "빠른 정리 후보", 2.4
    if any(keyword in name for keyword in _TANK_KEYWORDS):
        return "탱커 적", "앞라인이라 후순위", 1.4
    return "적", "집중 포커싱", 2.8


def _rank_monsters(state: RunState) -> list[dict]:
    total = len(state.monster_ids)
    ranked: list[dict] = []
    for index, monster_id in enumerate(state.monster_ids):
        role_label, reason, base_threat = _classify_monster_role(monster_id)
        threat = base_threat + (0.08 * index if total > 1 else 0.0)
        ranked.append(
            {
                "index": index,
                "id": monster_id,
                "slot_prefix": _monster_slot_prefix(index, total),
                "role_label": role_label,
                "reason": reason,
                "threat": threat,
            }
        )
    ranked.sort(key=lambda item: (-item["threat"], item["index"]))
    return ranked


def _format_target_label(monster: dict, total: int) -> str:
    prefix = monster["slot_prefix"]
    role_label = monster["role_label"]
    if total == 1:
        return "현재 적" if role_label == "적" else f"현재 {role_label}"
    return f"{prefix} 적" if role_label == "적" else f"{prefix} {role_label}"


def _recommend_target_for_card(
    card: dict,
    state: RunState,
    *,
    focus_target_index: int | None = None,
) -> dict:
    target_type = card.get("target") or "None"
    monsters = _rank_monsters(state)
    total = len(monsters)

    if target_type == "Self":
        return {"label": "자신", "reason": "자기 대상 카드", "focus_index": focus_target_index}
    if target_type == "AllEnemies":
        focus = monsters[0] if monsters else None
        reason = "광역 처리"
        if focus:
            reason += f" · 핵심 위협은 {focus['slot_prefix']}"
        return {"label": "전체 적", "reason": reason, "focus_index": focus_target_index}
    if target_type == "RandomEnemy":
        focus = monsters[0] if monsters else None
        reason = "무작위 대상"
        if focus:
            reason += f" · 이상적 표적은 {_format_target_label(focus, total)}"
        return {"label": "무작위 적", "reason": reason, "focus_index": focus_target_index}
    if target_type == "AnyAlly":
        return {"label": "아군", "reason": "아군 대상 카드", "focus_index": focus_target_index}
    if target_type == "AllAllies":
        return {"label": "전체 아군", "reason": "전체 아군 대상 카드", "focus_index": focus_target_index}
    if target_type != "AnyEnemy":
        return {"label": "", "reason": "", "focus_index": focus_target_index}

    if not monsters:
        return {"label": "적", "reason": "단일 대상 카드", "focus_index": focus_target_index}

    best_monster = monsters[0]
    chosen = best_monster
    if focus_target_index is not None:
        focus_match = next((monster for monster in monsters if monster["index"] == focus_target_index), None)
        if focus_match and focus_match["threat"] >= best_monster["threat"] - 0.7:
            chosen = focus_match

    return {
        "label": _format_target_label(chosen, total),
        "reason": chosen["reason"],
        "focus_index": chosen["index"],
    }


def score_combat_hand_card(
    card: dict,
    state: RunState,
    cards_db: list[dict],
    *,
    context: dict | None = None,
    available_stars: int | None = None,
) -> tuple[float, list[str]]:
    """전투 중 손패 카드의 즉시 플레이 가치를 계산."""
    if context is None:
        context = build_scoring_context(state, cards_db)

    score, reasons = score_card(card, state, cards_db, context=context)
    reasons = list(reasons)

    analysis = context["analysis"]
    card_type = card.get("type_key", "")
    cost = card.get("cost")
    star_cost = _effective_star_cost(card)
    star_gain = _star_gain(card)
    max_energy = max(getattr(state, "max_energy", 3), 0)
    damage = (card.get("damage") or 0) * (card.get("hit_count") or 1)
    block = card.get("block") or 0
    draw = card.get("cards_draw") or 0
    energy_gain = card.get("energy_gain") or 0
    hp_loss = card.get("hp_loss") or 0
    target = card.get("target") or ""
    hp_pct = state.current_hp / max(state.max_hp, 1)
    monster_count = len(state.monster_ids)

    if cost is not None:
        if cost > max_energy and not card.get("is_x_cost"):
            penalty = min(1.8 + (cost - max_energy) * 0.6, 3.0)
            score -= penalty
            reasons.insert(0, f"최대에너지 초과 -{penalty:.1f}")
        elif cost == 0:
            score += 0.6
            reasons.append("즉발 0코스트 +0.6")
        elif cost == 1:
            score += 0.3
            reasons.append("가벼운 코스트 +0.3")
        elif cost >= max_energy and card_type != "Power":
            score -= 0.2
            reasons.append("턴 에너지 부담 -0.2")

    if available_stars is not None:
        if star_cost > available_stars:
            penalty = min(2.2 + (star_cost - available_stars) * 0.7, 3.4)
            score -= penalty
            reasons.insert(0, f"별 부족 -{penalty:.1f}")
        elif star_cost > 0:
            scarcity = max(star_cost / max(available_stars, 1), 0.0)
            pressure = scarcity * 0.65
            score -= pressure
            reasons.append(f"별소모 -{pressure:.1f}")

    if star_gain > 0:
        bonus = star_gain * 0.95
        score += bonus
        reasons.append(f"별확보 +{bonus:.1f}")

    if card_type == "Power":
        score += 0.9
        reasons.append("파워 선사용 +0.9")

    if damage > 0:
        single_target_bonus = damage * (0.05 if monster_count <= 1 else 0.02)
        if single_target_bonus >= 0.25:
            score += single_target_bonus
            reasons.append(f"즉시딜 +{single_target_bonus:.1f}")
        if monster_count >= 3 and target == "AllEnemies":
            score += 1.3
            reasons.append("광역 처리 +1.3")
        elif monster_count >= 3 and (card.get("hit_count") or 1) >= 2:
            score += 0.5
            reasons.append("다수전 다단히트 +0.5")
        elif monster_count == 1 and target == "AllEnemies":
            score -= 0.5
            reasons.append("단일전에 광역 -0.5")

    if block > 0:
        block_urgency = max(0.0, 0.72 - hp_pct)
        block_bonus = block_urgency * block * 0.08
        if state.room_type in {"elite", "boss"}:
            block_bonus += block * 0.02
        if block_bonus >= 0.25:
            score += block_bonus
            reasons.append(f"생존/방어 +{block_bonus:.1f}")

    if draw > 0:
        draw_bonus = draw * 0.35
        score += draw_bonus
        if draw_bonus >= 0.25:
            reasons.append(f"손패 확장 +{draw_bonus:.1f}")

    if energy_gain > 0:
        score += energy_gain * 0.9
        reasons.append(f"에너지 회복 +{energy_gain * 0.9:.1f}")

    if hp_loss > 0:
        penalty = hp_loss * (0.18 if hp_pct < 0.55 else 0.08)
        score -= penalty
        reasons.append(f"체력소모 -{penalty:.1f}")

    if analysis["avg_cost"] > 1.8 and cost and cost >= 2:
        score -= 0.4
        reasons.append("무거운 손패 -0.4")

    return round(max(score, 0.0), 1), reasons[:6]


def recommend_combat_hand(
    detected_cards: list,
    state: RunState,
    cards_db: list[dict],
    *,
    fuzzy_match,
    available_stars: int | None = None,
) -> tuple[list[tuple[dict, float, float, list[str], int, str, str]], list[dict]]:
    """OCR된 손패를 카드 DB와 매칭해 전투 중 우선순위를 계산."""
    context = build_scoring_context(state, cards_db)
    card_index = context["card_index"]
    matched_entries: list[dict] = []
    for det in detected_cards:
        if not det.ocr_text:
            continue
        matched = _match_combat_hand_card(
            det.ocr_text,
            state=state,
            cards_db=cards_db,
            card_index=card_index,
            fuzzy_match=fuzzy_match,
        )
        if not matched:
            continue
        card, match_score, match_source = matched
        score, reasons = score_combat_hand_card(
            card,
            state,
            cards_db,
            context=context,
            available_stars=available_stars,
        )
        reasons = list(reasons)
        if match_source == "deck":
            reasons.insert(0, "손패/덱 기준 매칭")
        else:
            reasons.insert(0, "생성 카드 후보")

        matched_entries.append(
            {
                "card": card,
                "match_score": match_score,
                "score": score,
                "reasons": reasons[:6],
                "position": det.position,
            }
        )

    if not matched_entries:
        return [], []

    sequence_indexes, _ = _simulate_combat_sequence(
        matched_entries,
        max(getattr(state, "max_energy", 3), 0),
        available_stars,
    )
    step_by_index = {entry_idx: step + 1 for step, entry_idx in enumerate(sequence_indexes)}

    ordered_indexes = sorted(
        range(len(matched_entries)),
        key=lambda idx: (
            step_by_index.get(idx, 999),
            -matched_entries[idx]["score"],
            -matched_entries[idx]["match_score"],
            matched_entries[idx]["position"],
        ),
    )

    target_plan_by_index: dict[int, dict] = {}
    focus_target_index = _rank_monsters(state)[0]["index"] if state.monster_ids else None
    for idx in sequence_indexes:
        target_plan = _recommend_target_for_card(
            matched_entries[idx]["card"],
            state,
            focus_target_index=focus_target_index,
        )
        target_plan_by_index[idx] = target_plan
        focus_target_index = target_plan.get("focus_index", focus_target_index)

    recommendations: list[tuple[dict, float, float, list[str], int, str, str]] = []
    for idx in ordered_indexes[:5]:
        entry = matched_entries[idx]
        reasons = list(entry["reasons"])
        if idx in step_by_index:
            reasons.insert(0, f"순서 {step_by_index[idx]}")
        target_plan = target_plan_by_index.get(idx) or _recommend_target_for_card(entry["card"], state)
        if target_plan.get("label"):
            reasons.insert(1 if idx in step_by_index else 0, f"대상 {target_plan['label']}")
        recommendations.append(
            (
                entry["card"],
                entry["match_score"],
                entry["score"],
                reasons[:7],
                entry["position"],
                target_plan.get("label", ""),
                target_plan.get("reason", ""),
            )
        )

    sequence = []
    for idx in sequence_indexes[:5]:
        entry = matched_entries[idx]
        target_plan = target_plan_by_index.get(idx) or _recommend_target_for_card(entry["card"], state)
        sequence.append(
            {
                "step": step_by_index[idx],
                "name": entry["card"]["name"],
                "id": entry["card"]["id"],
                "cost": entry["card"].get("cost", "?"),
                "star_cost": _effective_star_cost(entry["card"]),
                "score": round(entry["score"], 1),
                "position": entry["position"],
                "target_label": target_plan.get("label", ""),
                "target_reason": target_plan.get("reason", ""),
            }
        )

    return recommendations, sequence


def generate_combat_advice(state: RunState, cards_db: list[dict]) -> list[str]:
    """현재 덱과 상태를 분석해서 전투 팁 생성.

    Returns: 우선순위 순 팁 리스트 (최대 5개).
    """
    analysis = analyze_deck(state, cards_db)
    tips: list[str] = []

    # 1) ★ 시너지 팁
    if analysis["star_generators"] >= 2 and analysis["star_consumers"] >= 1:
        tips.append("★ 생성 카드를 먼저 사용해 ★ 축적")
    elif analysis["star_generators"] >= 1 and analysis["star_consumers"] >= 1:
        tips.append("★ 생성 카드로 ★ 확보 후 소비 카드 사용")

    # 2) 파워 카드 팁
    if analysis["powers"] >= 2:
        tips.append("파워 카드 우선 사용 (초반 턴 활용)")
    elif analysis["powers"] == 1:
        tips.append("파워 카드를 첫 턴에 사용")

    # 3) 방어 부족 경고
    total = max(analysis["total"], 1)
    block_ratio = analysis["total_block"] / total
    attack_ratio = analysis["attacks"] / total
    if block_ratio < 3.0 and analysis["skills"] < analysis["attacks"]:
        tips.append("방어 카드 부족 - 방어 우선 고려")

    # 4) 멀티히트 팁
    if analysis["multi_hit"] >= 2:
        tips.append("멀티히트 카드로 딜 극대화 (힘 버프 시 효과 증폭)")

    # 5) 에너지 관리 팁
    if analysis["avg_cost"] > 1.8:
        tips.append("평균 코스트 높음 - 에너지 관리 주의")
    elif analysis["avg_cost"] <= 1.0:
        tips.append("저코스트 덱 - 많은 카드를 매 턴 사용")

    # 6) 드로우 팁
    if analysis["total_draw"] >= 3:
        tips.append("드로우 카드 먼저 사용해 핸드 확장")

    # 7) HP 기반 팁
    hp_pct = state.current_hp / max(state.max_hp, 1)
    if hp_pct < 0.3:
        tips.insert(0, "⚠ HP 위험 - 방어 최우선, 장기전 회피")
    elif hp_pct < 0.5:
        tips.append("HP 낮음 - 불필요한 피해 최소화")

    # 8) 전투 메타데이터 팁
    if state.room_type == "elite":
        tips.append("엘리트전 - 핵심 카드/포션 아끼지 않기")
    elif state.room_type == "boss":
        tips.append("보스전 - 장기전 대비 스케일링 우선")

    monster_count = len(state.monster_ids)
    if monster_count >= 3:
        tips.append("다수전 - 광역/다단히트 카드 각 보기")
    elif monster_count == 1 and analysis["total_damage"] >= analysis["total_block"]:
        tips.append("단일전 - 집중 딜로 빠르게 정리")

    if state.potions and state.room_type in {"elite", "boss"}:
        tips.append("보유 포션 적극 사용")
    elif state.max_potion_slots > 0 and len(state.potions) >= state.max_potion_slots:
        tips.append("포션 슬롯 가득 참 - 다음 보상 전 소모 고려")

    if state.ascension >= 10:
        tips.append("고승천 기준 - 체력 손해 교환 신중")
    if state.modifiers:
        tips.append(f"변형 룰 {len(state.modifiers)}개 활성 - 룰 확인")
    if getattr(state, "max_energy", 3) >= 4:
        tips.append(f"최대 에너지 {state.max_energy} - 고코스트 활용 가능")

    # 9) 덱 크기 팁
    if analysis["total"] <= 12:
        tips.append("슬림 덱 - 핵심 카드 빠르게 순환")
    elif analysis["total"] > 25:
        tips.append("덱 과대 - 핵심 카드 드로우 확률 낮음")

    # 10) 기본 카드 비율 팁
    basic_ratio = analysis["basics"] / total
    if basic_ratio > 0.5:
        tips.append("기본 카드 비율 높음 - 스트라이크/수비 의존 주의")

    return tips[:5]


if __name__ == "__main__":
    from save_parser import parse_save
    from card_db import load_card_db

    state = parse_save()
    if state is None:
        print("No active run found")
    else:
        cards_db = load_card_db()
        analysis = analyze_deck(state, cards_db)

        print(f"=== 전투 조언 ({state.character}) ===")
        print(f"HP: {state.current_hp}/{state.max_hp}")
        print(f"덱: {analysis['total']}장 (공격 {analysis['attacks']} / 스킬 {analysis['skills']} / 파워 {analysis['powers']})")
        print(f"★생성: {analysis['star_generators']} | ★소비: {analysis['star_consumers']}")
        print(f"멀티히트: {analysis['multi_hit']} | 평균코스트: {analysis['avg_cost']:.1f}")
        print(f"기본카드: {analysis['basics']}장")
        print()

        tips = generate_combat_advice(state, cards_db)
        if tips:
            print("전투 팁:")
            for i, tip in enumerate(tips, 1):
                print(f"  {i}. {tip}")
        else:
            print("(특별한 팁 없음)")
