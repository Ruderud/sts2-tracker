"""STS2 카드 추천 엔진 - 덱 분석 + 시너지 기반 스코어링."""

from save_parser import RunState
from utils import clean_description


# --- 덱 분석 ---

def analyze_deck(state: RunState, cards_db: list[dict]) -> dict:
    """현재 덱을 분석해서 특성 추출."""
    db_index = {c["id"]: c for c in cards_db}

    deck_cards = []
    for card in state.deck:
        card_id = card.id.replace("CARD.", "")
        if card_id in db_index:
            deck_cards.append(db_index[card_id])

    total = len(state.deck)
    attacks = sum(1 for c in deck_cards if c.get("type_key") == "Attack")
    skills = sum(1 for c in deck_cards if c.get("type_key") == "Skill")
    powers = sum(1 for c in deck_cards if c.get("type_key") == "Power")

    total_damage = sum(
        (c.get("damage") or 0) * (c.get("hit_count") or 1)
        for c in deck_cards if c.get("type_key") == "Attack"
    )
    total_block = sum(
        c.get("block") or 0
        for c in deck_cards if c.get("block")
    )
    total_draw = sum(c.get("cards_draw") or 0 for c in deck_cards)

    # ★ 시너지 분석
    star_generators = 0  # ★ 생성 카드
    star_consumers = 0   # ★ 소비 카드
    for c in deck_cards:
        desc = c.get("description_raw", "")
        if "star" in desc.lower():
            # 생성: "을 얻습니다" 패턴
            if "얻" in c.get("description", ""):
                star_generators += 1
            # 소비: "소모" 패턴 또는 ★ 비용
            if "소모" in c.get("description", "") or c.get("star_cost"):
                star_consumers += 1

    # 멀티히트 분석
    multi_hit_cards = sum(
        1 for c in deck_cards
        if (c.get("hit_count") or 1) > 1
    )

    # 에너지 커브
    costs = [c.get("cost") or 0 for c in deck_cards if c.get("cost") is not None]
    avg_cost = sum(costs) / max(len(costs), 1)

    # 기본 카드 비율 (Strike/Defend)
    basics = sum(1 for c in state.deck if "STRIKE" in c.id or "DEFEND" in c.id)

    return {
        "total": total,
        "attacks": attacks,
        "skills": skills,
        "powers": powers,
        "basics": basics,
        "total_damage": total_damage,
        "total_block": total_block,
        "total_draw": total_draw,
        "star_generators": star_generators,
        "star_consumers": star_consumers,
        "multi_hit": multi_hit_cards,
        "avg_cost": avg_cost,
    }


# --- 덱 필요도 분석 ---

def assess_needs(analysis: dict, state: RunState) -> dict:
    """덱이 부족한 부분 평가. 높을수록 해당 요소가 필요함."""
    needs = {}

    total = analysis["total"]
    # 피해 부족? (기본 카드 제외 공격력)
    non_basic_dmg = analysis["total_damage"] - analysis["basics"] * 6
    needs["damage"] = max(0, 3.0 - non_basic_dmg / max(total, 1) * 0.5)

    # 방어 부족?
    needs["block"] = max(0, 2.5 - analysis["total_block"] / max(total, 1) * 0.8)

    # 드로우 부족?
    needs["draw"] = max(0, 2.0 - analysis["total_draw"] * 0.5)

    # 스케일링 부족? (파워 카드)
    needs["scaling"] = max(0, 2.5 - analysis["powers"] * 1.5)

    # ★ 시너지 발전 가능성
    if analysis["star_generators"] >= 2:
        needs["star_synergy"] = 1.5  # 이미 ★ 방향이면 더 원함
    elif analysis["star_generators"] >= 1:
        needs["star_synergy"] = 1.0
    else:
        needs["star_synergy"] = 0.3

    # 덱 압축 필요? (기본 카드 많으면)
    basic_ratio = analysis["basics"] / max(total, 1)
    needs["thin"] = basic_ratio * 2.0  # 기본 카드 비율이 높으면 skip/exhaust 가치 증가

    # HP 낮으면 방어 필요도 증가
    hp_pct = state.current_hp / max(state.max_hp, 1)
    if hp_pct < 0.4:
        needs["block"] += 1.0
        needs["damage"] += 0.5  # 빠른 처치도 방어

    return needs


# --- 카드 스코어링 ---

# Regent 카드 티어 (커뮤니티 기반 대략적 평가)
CARD_TIERS = {
    # S-tier
    "MAKE_IT_SO": 5.0, "BOMBARDMENT": 4.8, "CRASH_LANDING": 4.7,
    "GENESIS": 4.7, "SWORD_SAGE": 4.6, "BIG_BANG": 4.5,
    "THE_SEALED_THRONE": 4.5, "VOID_FORM": 4.5,
    # A-tier
    "DYING_STAR": 4.2, "HEIRLOOM_HAMMER": 4.2, "BUNDLE_OF_JOY": 4.0,
    "BLACK_HOLE": 4.0, "NEUTRON_AEGIS": 4.0, "COMET": 4.0,
    "MONARCHS_GAZE": 3.8, "FOREGONE_CONCLUSION": 3.8,
    "SEVEN_STARS": 3.7, "ARSENAL": 3.7, "BEAT_INTO_SHAPE": 3.7,
    # B-tier
    "PROPHESIZE": 3.5, "DECISIONS_DECISIONS": 3.5,
    "CONVERGENCE": 3.4, "CHARGE": 3.4, "ROYALTIES": 3.3,
    "HIDDEN_CACHE": 3.3, "PHOTON_CUT": 3.2, "GLOW": 3.2,
    "CELESTIAL_MIGHT": 3.1, "SHINING_STRIKE": 3.1,
    "GUARDS": 3.0, "BULWARK": 3.0, "HAMMER_TIME": 3.0,
    # C-tier
    "CRESCENT_SPEAR": 2.8, "GUIDING_STAR": 2.7,
    "COSMIC_INDIFFERENCE": 2.7, "SOLAR_STRIKE": 2.7,
    "GATHER_LIGHT": 2.6, "GLITTERSTREAM": 2.5,
    "BEGONE": 2.5, "CRUSH_UNDER": 2.5,
    "REFINE_BLADE": 2.4, "ASTRAL_PULSE": 2.4,
    "PATTER": 2.3, "CLOAK_OF_STARS": 2.3,
}


def score_card(card: dict, state: RunState, cards_db: list[dict]) -> tuple[float, list[str]]:
    """카드 점수 계산. (score, reasons) 반환."""
    card_id = card["id"]
    card_type = card.get("type_key", "")
    rarity = card.get("rarity_key", "")
    cost = card.get("cost") or 0
    desc = card.get("description", "")
    reasons = []

    analysis = analyze_deck(state, cards_db)
    needs = assess_needs(analysis, state)

    # 1) 기본 티어 점수
    base = CARD_TIERS.get(card_id, 2.0)
    reasons.append(f"Tier {base:.1f}")

    score = base

    # 2) 덱 필요도 반영
    damage = (card.get("damage") or 0) * (card.get("hit_count") or 1)
    block = card.get("block") or 0
    draw = card.get("cards_draw") or 0

    if damage > 0:
        bonus = needs["damage"] * damage * 0.02
        if bonus > 0.3:
            reasons.append(f"피해 필요 +{bonus:.1f}")
        score += bonus

    if block > 0:
        bonus = needs["block"] * block * 0.03
        if bonus > 0.3:
            reasons.append(f"방어 필요 +{bonus:.1f}")
        score += bonus

    if draw > 0:
        bonus = needs["draw"] * draw * 0.3
        if bonus > 0.3:
            reasons.append(f"드로우 +{bonus:.1f}")
        score += bonus

    if card_type == "Power":
        bonus = needs["scaling"] * 0.5
        if bonus > 0.3:
            reasons.append(f"스케일링 +{bonus:.1f}")
        score += bonus

    # 3) ★ 시너지
    desc_raw = card.get("description_raw", "")
    has_star = "star" in desc_raw.lower()
    if has_star:
        bonus = needs["star_synergy"] * 1.0
        reasons.append(f"★시너지 +{bonus:.1f}")
        score += bonus

    # 4) Exhaust → 덱 압축 보너스
    keywords = card.get("keywords_key") or []
    if "Exhaust" in keywords:
        bonus = needs["thin"] * 0.5
        if bonus > 0.2:
            reasons.append(f"덱압축 +{bonus:.1f}")
        score += bonus

    # 5) 에너지 효율 (코스트 대비 효과)
    if cost == 0:
        score += 0.5
        reasons.append("0코스트 +0.5")
    elif cost >= 3 and analysis["avg_cost"] > 1.5:
        score -= 0.5
        reasons.append("고코스트 -0.5")

    # 6) 덱 크기 패널티
    if analysis["total"] > 18:
        penalty = (analysis["total"] - 18) * 0.15
        score -= penalty
        reasons.append(f"덱과대 -{penalty:.1f}")

    # 7) 중복 카드 패널티
    existing_count = sum(1 for c in state.deck if c.id.replace("CARD.", "") == card_id)
    if existing_count >= 2:
        score -= 1.0
        reasons.append("중복2+ -1.0")
    elif existing_count == 1:
        score -= 0.3
        reasons.append("중복 -0.3")

    # 8) 액트별 가중치
    if state.act == 0:  # Act 1
        if card_type == "Attack" and damage >= 10:
            score += 0.5
            reasons.append("Act1 고딜 +0.5")
        if card_type == "Power":
            score += 0.3

    # 9) skip 점수 (비교용)
    # 아무 카드도 안 가져가는 것이 나을 수 있음

    return round(max(score, 0), 1), reasons


def recommend(detected_cards: list[dict], state: RunState, cards_db: list[dict]) -> list[dict]:
    """카드 추천 결과 생성."""
    results = []
    for card in detected_cards:
        score, reasons = score_card(card, state, cards_db)
        results.append({
            "card": card,
            "score": score,
            "reasons": reasons,
        })

    # Skip 옵션 추가
    analysis = analyze_deck(state, cards_db)
    skip_score = 1.5
    skip_reasons = ["기본"]
    if analysis["total"] > 15:
        skip_score += (analysis["total"] - 15) * 0.3
        skip_reasons.append(f"덱{analysis['total']}장")
    if analysis["basics"] / max(analysis["total"], 1) < 0.4:
        skip_score += 0.5
        skip_reasons.append("기본카드 적음")

    results.append({
        "card": {"id": "SKIP", "name": "넘기기", "cost": "-", "rarity": "",
                 "rarity_key": "", "type": "", "description": "카드를 가져가지 않습니다."},
        "score": round(skip_score, 1),
        "reasons": skip_reasons,
    })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


if __name__ == "__main__":
    from save_parser import parse_save
    from card_db import load_card_db

    state = parse_save()
    if state is None:
        print("No active run")
        exit()

    cards_db = load_card_db()
    analysis = analyze_deck(state, cards_db)
    needs = assess_needs(analysis, state)

    print(f"=== 덱 분석 ({state.character}) ===")
    print(f"  카드: {analysis['total']}장 (공격 {analysis['attacks']} / 스킬 {analysis['skills']} / 파워 {analysis['powers']})")
    print(f"  기본: {analysis['basics']}장 | 평균코스트: {analysis['avg_cost']:.1f}")
    print(f"  총피해: {analysis['total_damage']} | 총방어: {analysis['total_block']} | 드로우: {analysis['total_draw']}")
    print(f"  ★생성: {analysis['star_generators']} | ★소비: {analysis['star_consumers']} | 멀티히트: {analysis['multi_hit']}")

    print(f"\n=== 필요도 ===")
    for key, val in sorted(needs.items(), key=lambda x: -x[1]):
        bar = "█" * int(val * 4)
        print(f"  {key:15s} {val:.1f} {bar}")

    # 테스트: 몇 개 카드 추천
    test_ids = ["GLOW", "CONVERGENCE", "PHOTON_CUT", "BULWARK", "GENESIS", "SWORD_SAGE"]
    db_index = {c["id"]: c for c in cards_db}
    test_cards = [db_index[cid] for cid in test_ids if cid in db_index]

    print(f"\n=== 카드 추천 테스트 ===")
    results = recommend(test_cards, state, cards_db)
    for r in results:
        c = r["card"]
        print(f"  {r['score']:4.1f} | {c['name']:12s} | {', '.join(r['reasons'])}")
