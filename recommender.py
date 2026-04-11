"""STS2 카드 추천 엔진 - 덱 분석 + 실전 히스토리 priors 기반 스코어링."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

from save_parser import RunState


DATA_DIR = Path(__file__).parent / "data"
WEIGHTS_PATH = DATA_DIR / "recommender_weights.json"
CARD_PRIORS_PATH = DATA_DIR / "card_priors.json"
CHARACTER_WEIGHTS_PATH = DATA_DIR / "recommender_weights_by_character.json"
CHARACTER_PRIORS_PATH = DATA_DIR / "card_priors_by_character.json"
RELIC_PRIORS_PATH = DATA_DIR / "relic_card_priors.json"


@dataclass(frozen=True)
class ScoringWeights:
    default_base: float = 2.0
    damage_scale: float = 0.02
    block_scale: float = 0.03
    draw_scale: float = 0.30
    power_scale: float = 0.50
    star_scale: float = 1.00
    thin_scale: float = 0.50
    zero_cost_bonus: float = 0.50
    high_cost_penalty: float = 0.50
    deck_penalty_scale: float = 0.15
    duplicate_penalty: float = 0.30
    duplicate_plus_penalty: float = 1.00
    act1_attack_bonus: float = 0.50
    act1_power_bonus: float = 0.30
    skip_base: float = 1.50
    skip_deck_scale: float = 0.30
    skip_thin_bonus: float = 0.50

    def to_dict(self) -> dict:
        return asdict(self)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


@lru_cache(maxsize=1)
def load_scoring_weights(path: str = str(WEIGHTS_PATH)) -> ScoringWeights:
    """저장된 추천 가중치 로드."""
    weights_path = Path(path)
    if not weights_path.exists():
        return ScoringWeights()

    with open(weights_path) as f:
        raw = json.load(f)
    return ScoringWeights(**raw)


def save_scoring_weights(weights: ScoringWeights, path: Path = WEIGHTS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(weights.to_dict(), f, ensure_ascii=False, indent=2)
    load_scoring_weights.cache_clear()


@lru_cache(maxsize=1)
def load_card_priors(path: str = str(CARD_PRIORS_PATH)) -> dict[str, dict]:
    """시뮬레이션으로 튜닝한 카드 priors 로드."""
    priors_path = Path(path)
    if not priors_path.exists():
        return {}

    with open(priors_path) as f:
        raw = json.load(f)
    return {card_id: value for card_id, value in raw.items() if not card_id.startswith("__")}


def save_card_priors(priors: dict[str, dict], path: Path = CARD_PRIORS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(priors, f, ensure_ascii=False, indent=2)
    load_card_priors.cache_clear()


@lru_cache(maxsize=1)
def load_character_scoring_weights(
    path: str = str(CHARACTER_WEIGHTS_PATH),
) -> dict[str, ScoringWeights]:
    """캐릭터별 추천 가중치 로드."""
    weights_path = Path(path)
    if not weights_path.exists():
        return {}

    with open(weights_path) as f:
        raw = json.load(f)
    return {character: ScoringWeights(**weights) for character, weights in raw.items()}


def save_character_scoring_weights(
    by_character: dict[str, ScoringWeights],
    path: Path = CHARACTER_WEIGHTS_PATH,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {character: weights.to_dict() for character, weights in by_character.items()}
    with open(path, "w") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    load_character_scoring_weights.cache_clear()


@lru_cache(maxsize=1)
def load_character_card_priors(
    path: str = str(CHARACTER_PRIORS_PATH),
) -> dict[str, dict[str, dict]]:
    """캐릭터별 카드 priors 로드."""
    priors_path = Path(path)
    if not priors_path.exists():
        return {}

    with open(priors_path) as f:
        raw = json.load(f)
    return {
        character: {
            card_id: value
            for card_id, value in priors.items()
            if not card_id.startswith("__")
        }
        for character, priors in raw.items()
    }


def save_character_card_priors(
    by_character: dict[str, dict[str, dict]],
    path: Path = CHARACTER_PRIORS_PATH,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(by_character, f, ensure_ascii=False, indent=2)
    load_character_card_priors.cache_clear()


@lru_cache(maxsize=1)
def load_relic_card_priors(path: str = str(RELIC_PRIORS_PATH)) -> dict[str, dict[str, dict]]:
    """유물-카드 시너지 priors 로드."""
    priors_path = Path(path)
    if not priors_path.exists():
        return {}

    with open(priors_path) as f:
        raw = json.load(f)
    return {
        relic_id: {
            card_id: value
            for card_id, value in card_priors.items()
            if not card_id.startswith("__")
        }
        for relic_id, card_priors in raw.items()
    }


def save_relic_card_priors(
    priors: dict[str, dict[str, dict]],
    path: Path = RELIC_PRIORS_PATH,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(priors, f, ensure_ascii=False, indent=2)
    load_relic_card_priors.cache_clear()


def reset_loaded_assets() -> None:
    load_scoring_weights.cache_clear()
    load_card_priors.cache_clear()
    load_character_scoring_weights.cache_clear()
    load_character_card_priors.cache_clear()
    load_relic_card_priors.cache_clear()


# Regent 커뮤니티 티어.
CARD_TIERS = {
    "MAKE_IT_SO": 5.0,
    "BOMBARDMENT": 4.8,
    "CRASH_LANDING": 4.7,
    "GENESIS": 4.7,
    "SWORD_SAGE": 4.6,
    "BIG_BANG": 4.5,
    "THE_SEALED_THRONE": 4.5,
    "VOID_FORM": 4.5,
    "DYING_STAR": 4.2,
    "HEIRLOOM_HAMMER": 4.2,
    "BUNDLE_OF_JOY": 4.0,
    "BLACK_HOLE": 4.0,
    "NEUTRON_AEGIS": 4.0,
    "COMET": 4.0,
    "MONARCHS_GAZE": 3.8,
    "FOREGONE_CONCLUSION": 3.8,
    "SEVEN_STARS": 3.7,
    "ARSENAL": 3.7,
    "BEAT_INTO_SHAPE": 3.7,
    "PROPHESIZE": 3.5,
    "DECISIONS_DECISIONS": 3.5,
    "CONVERGENCE": 3.4,
    "CHARGE": 3.4,
    "ROYALTIES": 3.3,
    "HIDDEN_CACHE": 3.3,
    "PHOTON_CUT": 3.2,
    "GLOW": 3.2,
    "CELESTIAL_MIGHT": 3.1,
    "SHINING_STRIKE": 3.1,
    "GUARDS": 3.0,
    "BULWARK": 3.0,
    "HAMMER_TIME": 3.0,
    "CRESCENT_SPEAR": 2.8,
    "GUIDING_STAR": 2.7,
    "COSMIC_INDIFFERENCE": 2.7,
    "SOLAR_STRIKE": 2.7,
    "GATHER_LIGHT": 2.6,
    "GLITTERSTREAM": 2.5,
    "BEGONE": 2.5,
    "CRUSH_UNDER": 2.5,
    "REFINE_BLADE": 2.4,
    "ASTRAL_PULSE": 2.4,
    "PATTER": 2.3,
    "CLOAK_OF_STARS": 2.3,
}


def analyze_deck(
    state: RunState,
    cards_db: list[dict],
    *,
    card_index: dict[str, dict] | None = None,
) -> dict:
    """현재 덱을 분석해서 특성 추출."""
    if card_index is None:
        card_index = {c["id"]: c for c in cards_db}

    deck_cards = []
    for card in state.deck:
        card_id = card.id.replace("CARD.", "")
        db_card = card_index.get(card_id)
        if db_card:
            deck_cards.append(db_card)

    total = len(state.deck)
    attacks = sum(1 for c in deck_cards if c.get("type_key") == "Attack")
    skills = sum(1 for c in deck_cards if c.get("type_key") == "Skill")
    powers = sum(1 for c in deck_cards if c.get("type_key") == "Power")

    total_damage = sum(
        (c.get("damage") or 0) * (c.get("hit_count") or 1)
        for c in deck_cards
        if c.get("type_key") == "Attack"
    )
    total_block = sum(c.get("block") or 0 for c in deck_cards if c.get("block"))
    total_draw = sum(c.get("cards_draw") or 0 for c in deck_cards)

    star_generators = 0
    star_consumers = 0
    for card in deck_cards:
        desc = card.get("description_raw", "")
        if "star" not in desc.lower():
            continue
        if "얻" in card.get("description", ""):
            star_generators += 1
        if "소모" in card.get("description", "") or card.get("star_cost"):
            star_consumers += 1

    multi_hit_cards = sum(1 for c in deck_cards if (c.get("hit_count") or 1) > 1)
    costs = [c.get("cost") or 0 for c in deck_cards if c.get("cost") is not None]
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
        "avg_cost": sum(costs) / max(len(costs), 1),
    }


def assess_needs(analysis: dict, state: RunState) -> dict:
    """덱이 부족한 부분 평가. 높을수록 해당 요소가 필요함."""
    total = max(analysis["total"], 1)
    non_basic_dmg = analysis["total_damage"] - analysis["basics"] * 6

    needs = {
        "damage": max(0.0, 3.0 - non_basic_dmg / total * 0.5),
        "block": max(0.0, 2.5 - analysis["total_block"] / total * 0.8),
        "draw": max(0.0, 2.0 - analysis["total_draw"] * 0.5),
        "scaling": max(0.0, 2.5 - analysis["powers"] * 1.5),
        "thin": (analysis["basics"] / total) * 2.0,
    }

    if analysis["star_generators"] >= 2:
        needs["star_synergy"] = 1.5
    elif analysis["star_generators"] >= 1:
        needs["star_synergy"] = 1.0
    else:
        needs["star_synergy"] = 0.3

    hp_pct = state.current_hp / max(state.max_hp, 1)
    if hp_pct < 0.4:
        needs["block"] += 1.0
        needs["damage"] += 0.5

    if state.ascension >= 10:
        needs["block"] += 0.3
        needs["scaling"] += 0.2
    if state.ascension >= 20:
        needs["damage"] += 0.2
        needs["thin"] += 0.2
    if state.modifiers:
        needs["scaling"] += min(len(state.modifiers) * 0.1, 0.4)

    return needs


def build_scoring_context(
    state: RunState,
    cards_db: list[dict],
    *,
    weights: ScoringWeights | None = None,
    card_priors: dict[str, dict] | None = None,
    character_weights: ScoringWeights | None = None,
    character_priors: dict[str, dict] | None = None,
    relic_priors: dict[str, dict[str, dict]] | None = None,
) -> dict:
    """점수 계산에 필요한 공용 컨텍스트."""
    if weights is None:
        weights = load_scoring_weights()
    if character_weights is None:
        character_weights = load_character_scoring_weights().get(state.character)
    if card_priors is None:
        card_priors = load_card_priors()
    if character_priors is None:
        character_priors = load_character_card_priors().get(state.character, {})
    if relic_priors is None:
        relic_priors = load_relic_card_priors()

    active_weights = character_weights or weights

    card_index = {c["id"]: c for c in cards_db}
    analysis = analyze_deck(state, cards_db, card_index=card_index)
    needs = assess_needs(analysis, state)
    return {
        "weights": active_weights,
        "card_priors": card_priors,
        "character_priors": character_priors,
        "relic_priors": relic_priors,
        "card_index": card_index,
        "analysis": analysis,
        "needs": needs,
    }


def _resolve_base_score(
    card_id: str,
    weights: ScoringWeights,
    card_priors: dict[str, dict],
    character_priors: dict[str, dict],
    reasons: list[str],
) -> float:
    base = CARD_TIERS.get(card_id, weights.default_base)

    blended = base
    global_prior = card_priors.get(card_id)
    if global_prior:
        confidence = _clamp(float(global_prior.get("confidence", 0.0)), 0.0, 1.0)
        empirical = float(global_prior.get("score", blended))
        blended = blended * (1.0 - confidence) + empirical * confidence
        if confidence >= 0.45:
            reasons.append(f"실전데이터 {blended:.1f}")

    character_prior = character_priors.get(card_id)
    if character_prior:
        confidence = _clamp(float(character_prior.get("confidence", 0.0)), 0.0, 1.0)
        empirical = float(character_prior.get("score", blended))
        blended = blended * (1.0 - confidence) + empirical * confidence
        if confidence >= 0.35:
            reasons.append(f"캐릭터보정 {blended:.1f}")

    if not global_prior and not character_prior:
        reasons.append(f"Base {base:.1f}")
    elif not reasons:
        reasons.append(f"Base {blended:.1f}")
    return blended


def score_card(
    card: dict,
    state: RunState,
    cards_db: list[dict],
    *,
    context: dict | None = None,
    weights: ScoringWeights | None = None,
    card_priors: dict[str, dict] | None = None,
    character_weights: ScoringWeights | None = None,
    character_priors: dict[str, dict] | None = None,
    relic_priors: dict[str, dict[str, dict]] | None = None,
) -> tuple[float, list[str]]:
    """카드 점수 계산. (score, reasons) 반환."""
    if context is None:
        context = build_scoring_context(
            state,
            cards_db,
            weights=weights,
            card_priors=card_priors,
            character_weights=character_weights,
            character_priors=character_priors,
            relic_priors=relic_priors,
        )

    weights = context["weights"]
    analysis = context["analysis"]
    needs = context["needs"]
    card_priors = context["card_priors"]
    character_priors = context["character_priors"]
    relic_priors = context["relic_priors"]

    card_id = card["id"]
    card_type = card.get("type_key", "")
    cost = card.get("cost") or 0
    reasons: list[str] = []

    score = _resolve_base_score(card_id, weights, card_priors, character_priors, reasons)

    damage = (card.get("damage") or 0) * (card.get("hit_count") or 1)
    block = card.get("block") or 0
    draw = card.get("cards_draw") or 0

    if damage > 0:
        bonus = needs["damage"] * damage * weights.damage_scale
        if bonus > 0.25:
            reasons.append(f"피해 필요 +{bonus:.1f}")
        score += bonus

    if block > 0:
        bonus = needs["block"] * block * weights.block_scale
        if bonus > 0.25:
            reasons.append(f"방어 필요 +{bonus:.1f}")
        score += bonus

    if draw > 0:
        bonus = needs["draw"] * draw * weights.draw_scale
        if bonus > 0.25:
            reasons.append(f"드로우 +{bonus:.1f}")
        score += bonus

    if card_type == "Power":
        bonus = needs["scaling"] * weights.power_scale
        if bonus > 0.25:
            reasons.append(f"스케일링 +{bonus:.1f}")
        score += bonus

    desc_raw = card.get("description_raw", "")
    if "star" in desc_raw.lower():
        bonus = needs["star_synergy"] * weights.star_scale
        reasons.append(f"★시너지 +{bonus:.1f}")
        score += bonus

    keywords = card.get("keywords_key") or []
    if "Exhaust" in keywords:
        bonus = needs["thin"] * weights.thin_scale
        if bonus > 0.20:
            reasons.append(f"덱압축 +{bonus:.1f}")
        score += bonus

    relic_bonus = 0.0
    best_relic: str | None = None
    best_relic_value = 0.0
    for relic_id in set(state.relics):
        relic_bonus_data = (relic_priors.get(relic_id) or {}).get(card_id)
        if not relic_bonus_data:
            continue
        confidence = _clamp(float(relic_bonus_data.get("confidence", 0.0)), 0.0, 1.0)
        raw_bonus = float(relic_bonus_data.get("score_bonus", 0.0))
        bonus = raw_bonus * confidence
        relic_bonus += bonus
        if abs(bonus) > abs(best_relic_value):
            best_relic_value = bonus
            best_relic = relic_id.replace("RELIC.", "")

    relic_bonus = _clamp(relic_bonus, -1.1, 1.25)
    if abs(relic_bonus) >= 0.20:
        label = best_relic if best_relic else "유물"
        sign = "+" if relic_bonus >= 0 else ""
        reasons.append(f"유물시너지({label}) {sign}{relic_bonus:.1f}")
    score += relic_bonus

    if cost == 0:
        score += weights.zero_cost_bonus
        reasons.append(f"0코스트 +{weights.zero_cost_bonus:.1f}")
    elif cost >= 3 and analysis["avg_cost"] > 1.5:
        score -= weights.high_cost_penalty
        reasons.append(f"고코스트 -{weights.high_cost_penalty:.1f}")

    if analysis["total"] > 18:
        penalty = (analysis["total"] - 18) * weights.deck_penalty_scale
        score -= penalty
        reasons.append(f"덱과대 -{penalty:.1f}")

    existing_count = sum(1 for item in state.deck if item.id.replace("CARD.", "") == card_id)
    if existing_count >= 2:
        score -= weights.duplicate_plus_penalty
        reasons.append(f"중복2+ -{weights.duplicate_plus_penalty:.1f}")
    elif existing_count == 1:
        score -= weights.duplicate_penalty
        reasons.append(f"중복 -{weights.duplicate_penalty:.1f}")

    if state.act == 0 and card_type == "Attack" and damage >= 10:
        score += weights.act1_attack_bonus
        reasons.append(f"Act1 고딜 +{weights.act1_attack_bonus:.1f}")
    if state.act == 0 and card_type == "Power" and weights.act1_power_bonus:
        score += weights.act1_power_bonus

    return round(max(score, 0.0), 1), reasons


def score_skip(
    state: RunState,
    cards_db: list[dict],
    *,
    context: dict | None = None,
    weights: ScoringWeights | None = None,
    character_weights: ScoringWeights | None = None,
    relic_priors: dict[str, dict[str, dict]] | None = None,
) -> tuple[float, list[str]]:
    """넘기기 점수 계산."""
    if context is None:
        context = build_scoring_context(
            state,
            cards_db,
            weights=weights,
            character_weights=character_weights,
            relic_priors=relic_priors,
        )

    weights = context["weights"]
    analysis = context["analysis"]
    reasons = ["기본"]
    score = weights.skip_base

    if analysis["total"] > 15:
        bonus = (analysis["total"] - 15) * weights.skip_deck_scale
        score += bonus
        reasons.append(f"덱{analysis['total']}장 +{bonus:.1f}")

    if analysis["basics"] / max(analysis["total"], 1) < 0.4:
        score += weights.skip_thin_bonus
        reasons.append(f"기본카드 적음 +{weights.skip_thin_bonus:.1f}")

    if state.ascension >= 15 and analysis["total"] > 14:
        score += 0.3
        reasons.append("고승천 +0.3")
    if state.modifiers:
        bonus = min(len(state.modifiers) * 0.1, 0.3)
        score += bonus
        reasons.append(f"변형룰 +{bonus:.1f}")

    return round(score, 1), reasons


def recommend(
    detected_cards: list[dict],
    state: RunState,
    cards_db: list[dict],
    *,
    weights: ScoringWeights | None = None,
    card_priors: dict[str, dict] | None = None,
    character_weights: ScoringWeights | None = None,
    character_priors: dict[str, dict] | None = None,
    relic_priors: dict[str, dict[str, dict]] | None = None,
) -> list[dict]:
    """카드 추천 결과 생성."""
    context = build_scoring_context(
        state,
        cards_db,
        weights=weights,
        card_priors=card_priors,
        character_weights=character_weights,
        character_priors=character_priors,
        relic_priors=relic_priors,
    )

    results = []
    for card in detected_cards:
        score, reasons = score_card(card, state, cards_db, context=context)
        results.append(
            {
                "card": card,
                "score": score,
                "reasons": reasons,
            }
        )

    skip_score, skip_reasons = score_skip(state, cards_db, context=context)
    results.append(
        {
            "card": {
                "id": "SKIP",
                "name": "넘기기",
                "cost": "-",
                "rarity": "",
                "rarity_key": "",
                "type": "",
                "description": "카드를 가져가지 않습니다.",
            },
            "score": skip_score,
            "reasons": skip_reasons,
        }
    )

    results.sort(key=lambda item: item["score"], reverse=True)
    return results


if __name__ == "__main__":
    from card_db import load_card_db
    from save_parser import parse_save

    state = parse_save()
    if state is None:
        print("No active run")
        raise SystemExit(1)

    cards_db = load_card_db()
    context = build_scoring_context(state, cards_db)
    analysis = context["analysis"]
    needs = context["needs"]

    print(f"=== 덱 분석 ({state.character}) ===")
    print(
        f"  카드: {analysis['total']}장 "
        f"(공격 {analysis['attacks']} / 스킬 {analysis['skills']} / 파워 {analysis['powers']})"
    )
    print(f"  기본: {analysis['basics']}장 | 평균코스트: {analysis['avg_cost']:.1f}")
    print(
        f"  총피해: {analysis['total_damage']} | 총방어: {analysis['total_block']} "
        f"| 드로우: {analysis['total_draw']}"
    )
    print(
        f"  ★생성: {analysis['star_generators']} | ★소비: {analysis['star_consumers']} "
        f"| 멀티히트: {analysis['multi_hit']}"
    )

    print("\n=== 필요도 ===")
    for key, value in sorted(needs.items(), key=lambda item: -item[1]):
        bar = "█" * int(value * 4)
        print(f"  {key:15s} {value:.1f} {bar}")

    test_ids = ["GLOW", "CONVERGENCE", "PHOTON_CUT", "BULWARK", "GENESIS", "SWORD_SAGE"]
    db_index = {c["id"]: c for c in cards_db}
    test_cards = [db_index[cid] for cid in test_ids if cid in db_index]

    print("\n=== 카드 추천 테스트 ===")
    for result in recommend(test_cards, state, cards_db):
        card = result["card"]
        print(f"  {result['score']:4.1f} | {card['name']:12s} | {', '.join(result['reasons'])}")
