"""STS2 이벤트 선택지 추천 엔진 - 실전 히스토리 priors + 상태 기반 휴리스틱."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from build_meta import (
    build_decay_weight,
    load_history_build_id,
    ordered_build_ids,
    split_latest_and_legacy_paths,
    summarize_builds,
)
from event_db import (
    build_event_page_lookup,
    filter_options_for_state,
    flatten_event_pages,
    load_event_db,
    match_event_page,
    visible_options_for_query,
)
from history_replay import EventDecision, iter_history_paths, load_event_decisions
from recommender import analyze_deck, assess_needs
from save_parser import RunState, get_local_steam_user_id
from utils import clean_game_text


DATA_DIR = Path(__file__).parent / "data"
EVENT_PRIORS_PATH = DATA_DIR / "event_option_priors.json"
EVENT_BUILD_META_PATH = DATA_DIR / "event_build_meta.json"
_NUMBER_RE = re.compile(r"(\d+)")
VALID_RATIO = 0.2
MIN_LEGACY_EVENT_IMPROVEMENT = 0.002
LEGACY_EVENT_DECAY_CANDIDATES = (0.35, 0.5, 0.65, 0.8)
LEGACY_EVENT_BLEND_CANDIDATES = (0.15, 0.25, 0.35, 0.5)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _page_key(event_id: str, page_id: str) -> str:
    return f"{event_id}.{page_id}"


def _extract_numbers(text: str) -> list[int]:
    return [int(match) for match in _NUMBER_RE.findall(text or "")]


@lru_cache(maxsize=1)
def load_event_priors(path: str = str(EVENT_PRIORS_PATH)) -> dict[str, dict]:
    """이벤트 페이지별 option priors 로드."""
    priors_path = Path(path)
    if not priors_path.exists():
        return {}

    with open(priors_path) as f:
        return json.load(f)


def save_event_priors(priors: dict[str, dict], path: Path = EVENT_PRIORS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(priors, f, ensure_ascii=False, indent=2)
    load_event_priors.cache_clear()


def load_run_character(path: Path) -> str:
    with open(path) as f:
        data = json.load(f)
    players = data.get("players") or []
    preferred_player_id = get_local_steam_user_id(path)
    if preferred_player_id:
        for player in players:
            player_id = str(player.get("id") or player.get("player_id") or "")
            if player_id == preferred_player_id:
                return player["character"].replace("CHARACTER.", "")
    return players[0]["character"].replace("CHARACTER.", "")


def split_history_paths(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    by_char: dict[str, list[Path]] = defaultdict(list)
    for path in paths:
        by_char[load_run_character(path)].append(path)

    train_paths: list[Path] = []
    valid_paths: list[Path] = []
    for group in by_char.values():
        group = sorted(group)
        valid_count = max(1, round(len(group) * VALID_RATIO)) if len(group) > 1 else 0
        valid_paths.extend(group[-valid_count:] if valid_count else [])
        train_paths.extend(group[:-valid_count] if valid_count else group)

    if not valid_paths and len(paths) > 1:
        ordered = sorted(paths)
        fallback_valid_count = max(1, round(len(ordered) * VALID_RATIO))
        valid_paths = ordered[-fallback_valid_count:]
        train_paths = ordered[:-fallback_valid_count]

    return sorted(train_paths), sorted(valid_paths)


def build_event_priors(
    decisions: list[EventDecision],
    *,
    decision_weights: dict[int, float] | None = None,
    metadata: dict | None = None,
) -> dict[str, dict]:
    """히스토리 이벤트 선택을 페이지별 priors로 요약."""
    by_page: dict[str, dict] = {}
    for decision in decisions:
        weight = max((decision_weights or {}).get(id(decision), 1.0), 0.0)
        if weight <= 0.0:
            continue
        key = _page_key(decision.event_id, decision.page_id)
        page_bucket = by_page.setdefault(
            key,
            {
                "__meta__": {
                    "event_id": decision.event_id,
                    "event_name": decision.event_name,
                    "page_id": decision.page_id,
                    "samples": 0,
                }
            },
        )
        page_bucket["__meta__"]["samples"] += weight
        for option_id in decision.option_ids:
            option_bucket = page_bucket.setdefault(
                option_id,
                {"offers": 0, "picks": 0, "wins": 0, "comparisons": 0},
            )
            option_bucket["offers"] += weight
            if option_id == decision.picked_id:
                option_bucket["picks"] += weight
                option_bucket["wins"] += weight * max(len(decision.option_ids) - 1, 0)
            else:
                option_bucket["comparisons"] += weight
                if decision.picked_id in decision.option_ids:
                    option_bucket["comparisons"] += weight * max(len(decision.option_ids) - 2, 0)

        for option_id in decision.option_ids:
            if option_id == decision.picked_id:
                continue
            loser_bucket = page_bucket.setdefault(
                option_id,
                {"offers": 0, "picks": 0, "wins": 0, "comparisons": 0},
            )
            loser_bucket["comparisons"] += weight

    priors: dict[str, dict] = {}
    for key, page_bucket in by_page.items():
        meta = dict(page_bucket["__meta__"])
        page_prior = {"__meta__": meta}
        samples = meta["samples"]
        for option_id, stats in page_bucket.items():
            if option_id == "__meta__":
                continue
            offers = int(stats["offers"])
            picks = int(stats["picks"])
            comparisons = max(int(stats["comparisons"]) + int(stats["wins"]), 1)
            pairwise = stats["wins"] / comparisons
            pick_rate = picks / max(offers, 1)
            confidence = _clamp(1.0 - math.exp(-offers / 3.5), 0.0, 0.95)
            score = round(0.9 + pairwise * 4.2 + pick_rate * 0.6, 3)
            page_prior[option_id] = {
                "offers": round(offers, 3),
                "picks": round(picks, 3),
                "pick_rate": round(pick_rate, 4),
                "pairwise": round(pairwise, 4),
                "score": score,
                "confidence": round(confidence, 4),
                "samples": round(samples, 3),
            }
        priors[key] = page_prior
    if metadata:
        priors["__build_meta__"] = metadata
    return priors


def _count_upgradable_cards(state: RunState) -> int:
    return sum(1 for card in state.deck if getattr(card, "upgrades", 0) == 0)


def _count_keyword(state: RunState, needle: str) -> int:
    return sum(1 for card in state.deck if needle in card.id)


def _heuristic_option_score(option: dict, state: RunState, cards_db: list[dict]) -> tuple[float, list[str]]:
    analysis = analyze_deck(state, cards_db)
    needs = assess_needs(analysis, state)
    hp_pct = state.current_hp / max(state.max_hp, 1)
    basics_ratio = analysis["basics"] / max(analysis["total"], 1)
    upgradable = _count_upgradable_cards(state)
    title = clean_game_text(option.get("title", ""))
    desc = clean_game_text(option.get("description", ""))
    text = f"{title}\n{desc}"
    numbers = _extract_numbers(text)

    if option["id"].endswith("_LOCKED") or "잠김" in title:
        return 0.0, ["잠김"]

    score = 2.0
    reasons: list[str] = []

    if "체력을" in text and "잃" in text:
        amount = max(numbers) if numbers else 5
        penalty = _clamp(amount * (0.035 + max(0.55 - hp_pct, 0.0) * 0.08), 0.2, 2.6)
        score -= penalty
        reasons.append(f"체력비용 -{penalty:.1f}")

    if "최대 체력" in text and "잃" in text:
        amount = max(numbers) if numbers else 3
        penalty = _clamp(amount * 0.18, 0.5, 2.4)
        score -= penalty
        reasons.append(f"최대체력 -{penalty:.1f}")

    if "체력을" in text and ("회복" in text or "회복합니다" in text):
        amount = max(numbers) if numbers else 8
        bonus = _clamp(amount * (0.02 + max(0.75 - hp_pct, 0.0) * 0.05), 0.2, 1.4)
        score += bonus
        reasons.append(f"회복 +{bonus:.1f}")

    if "최대 체력" in text and ("얻" in text or "증가" in text):
        amount = max(numbers) if numbers else 4
        bonus = _clamp(amount * 0.18, 0.4, 1.8)
        score += bonus
        reasons.append(f"최대체력 +{bonus:.1f}")

    if "골드" in text and ("지불" in text or "준다" in text):
        cost = max(numbers) if numbers else 35
        if state.gold < cost:
            score -= 2.6
            reasons.append("골드부족 -2.6")
        else:
            penalty = _clamp(cost / max(state.gold, 1) * 1.2, 0.1, 1.5)
            score -= penalty
            reasons.append(f"골드비용 -{penalty:.1f}")

    if "골드" in text and "얻" in text:
        gain = max(numbers) if numbers else 50
        bonus = _clamp(gain / 90, 0.2, 1.3)
        score += bonus
        reasons.append(f"골드획득 +{bonus:.1f}")

    if "유물" in text and "얻" in text:
        relic_count = max(1, len([num for num in numbers if num <= 3]) or [1])
        bonus = _clamp(0.9 + (relic_count - 1) * 0.55, 0.9, 2.0)
        score += bonus
        reasons.append(f"유물 +{bonus:.1f}")

    if "카드를 1장 선택해" in text or ("카드 보상" in text and "무색" in text):
        bonus = _clamp(0.6 + needs["damage"] * 0.08 + needs["scaling"] * 0.08, 0.6, 1.2)
        score += bonus
        reasons.append(f"선택보상 +{bonus:.1f}")

    if "덱" in text and "추가" in text:
        bonus = _clamp(0.35 + needs["damage"] * 0.08 + needs["scaling"] * 0.08, 0.3, 1.0)
        score += bonus
        reasons.append(f"카드추가 +{bonus:.1f}")

    if "제거" in text and "덱" in text:
        bonus = _clamp(0.4 + basics_ratio * 1.5 + max(analysis["total"] - 14, 0) * 0.05, 0.4, 2.2)
        score += bonus
        reasons.append(f"제거 +{bonus:.1f}")

    if "변화" in text:
        bonus = _clamp(0.35 + basics_ratio * 1.1, 0.3, 1.4)
        score += bonus
        reasons.append(f"변화 +{bonus:.1f}")

    if "강화" in text:
        bonus = _clamp(0.3 + min(upgradable, 8) * 0.09, 0.3, 1.3)
        score += bonus
        reasons.append(f"강화 +{bonus:.1f}")

    if "포션" in text and ("생성" in text or "얻" in text):
        bonus = 0.35
        if state.max_potion_slots > 0 and len(state.potions) >= state.max_potion_slots:
            bonus -= 0.25
        score += bonus
        reasons.append(f"포션 {bonus:+.1f}")

    if "공격 카드" in text:
        bonus = needs["damage"] * 0.35
        score += bonus
        if bonus >= 0.25:
            reasons.append(f"공격필요 +{bonus:.1f}")

    if "스킬 카드" in text:
        bonus = max(needs["block"], needs["draw"]) * 0.28
        score += bonus
        if bonus >= 0.25:
            reasons.append(f"스킬필요 +{bonus:.1f}")

    if "파워 카드" in text:
        bonus = needs["scaling"] * 0.35
        score += bonus
        if bonus >= 0.25:
            reasons.append(f"스케일링 +{bonus:.1f}")

    if "카드를 3장 뽑" in text or "카드를 2장 뽑" in text:
        bonus = needs["draw"] * 0.4
        score += bonus
        if bonus >= 0.2:
            reasons.append(f"드로우 +{bonus:.1f}")

    if "[energy" in text or "에너" in text:
        score += 0.45
        reasons.append("에너지 +0.5")

    if "저주" in text or "상처" in text or "쇠약" in text and "부여" in text:
        score -= 0.8
        reasons.append("리스크 -0.8")

    if any(word in title for word in ("떠난다", "나아간다", "지나친다")) and len(reasons) == 0:
        score = min(score, 1.8)
        reasons.append("무난")

    if "STRIKE" in text.upper():
        bonus = _clamp(_count_keyword(state, "STRIKE") * 0.08, 0.0, 0.5)
        score += bonus
        if bonus > 0:
            reasons.append(f"기본공격 +{bonus:.1f}")

    return round(_clamp(score, 0.0, 6.5), 2), reasons


def score_event_option(
    page: dict,
    option: dict,
    state: RunState,
    cards_db: list[dict],
    *,
    event_priors: dict[str, dict] | None = None,
) -> tuple[float, list[str]]:
    """이벤트 선택지 점수 계산."""
    heuristic_score, reasons = _heuristic_option_score(option, state, cards_db)
    if option["id"].endswith("_LOCKED") or reasons == ["잠김"]:
        return 0.0, ["잠김"]
    score = heuristic_score
    priors = event_priors or load_event_priors()
    page_prior = priors.get(_page_key(page["event_id"], page["page_id"]), {})
    option_prior = page_prior.get(option["id"])
    if option_prior:
        confidence = _clamp(float(option_prior.get("confidence", 0.0)), 0.0, 0.95)
        score = float(option_prior.get("score", score))
        if confidence >= 0.3:
            reasons.insert(0, f"실전 {score:.1f}")
    elif not reasons:
        reasons.append("기본")

    return round(_clamp(score, 0.0, 6.5), 2), reasons[:5]


def rank_event_options(
    page: dict,
    state: RunState,
    cards_db: list[dict],
    *,
    event_priors: dict[str, dict] | None = None,
    option_ids: list[str] | None = None,
    query: str | None = None,
) -> list[dict]:
    """페이지 내 선택지들을 점수순으로 정렬."""
    options = filter_options_for_state(page, state, option_ids=option_ids)
    if query is not None:
        visible_ids = {option["id"] for option in visible_options_for_query(page, query)}
        options = [option for option in options if option["id"] in visible_ids]

    ranked = []
    for option in options:
        score, reasons = score_event_option(
            page,
            option,
            state,
            cards_db,
            event_priors=event_priors,
        )
        ranked.append(
            {
                "id": option["id"],
                "title": option["title"],
                "description": option["description"],
                "score": score,
                "reasons": reasons,
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked


def recommend_event_choices(
    ocr_text: str,
    state: RunState,
    cards_db: list[dict],
    *,
    event_pages: list[dict] | None = None,
    event_priors: dict[str, dict] | None = None,
) -> dict | None:
    """OCR 텍스트에서 현재 이벤트 페이지를 추론하고 선택지를 추천."""
    if event_pages is None:
        event_pages = flatten_event_pages(load_event_db())
    if event_priors is None:
        event_priors = load_event_priors()

    matches = match_event_page(ocr_text, event_pages)
    if not matches:
        return None

    page, match_score = matches[0]
    ranked = rank_event_options(
        page,
        state,
        cards_db,
        event_priors=event_priors,
        query=ocr_text,
    )
    if len(ranked) < 2:
        ranked = rank_event_options(
            page,
            state,
            cards_db,
            event_priors=event_priors,
        )
    if len(ranked) < 2:
        return None

    return {
        "event_id": page["event_id"],
        "event_name": page["event_name"],
        "page_id": page["page_id"],
        "match_score": match_score,
        "options": ranked,
        "best_idx": 0,
    }


def evaluate_event_decisions(
    decisions: list[EventDecision],
    cards_db: list[dict],
    *,
    event_pages: list[dict] | None = None,
    event_priors: dict[str, dict] | None = None,
) -> dict:
    """히스토리 이벤트 의사결정에 대한 재생 벤치마크."""
    if event_pages is None:
        event_pages = flatten_event_pages(load_event_db())
    if event_priors is None:
        event_priors = load_event_priors()
    page_lookup = build_event_page_lookup(event_pages)

    total = 0
    top1 = 0.0
    mrr = 0.0
    pairwise = 0.0

    for decision in decisions:
        page = page_lookup.get((decision.event_id, decision.page_id))
        if page is None or decision.picked_id not in decision.option_ids:
            continue
        ranked = rank_event_options(
            page,
            decision.state,
            cards_db,
            event_priors=event_priors,
            option_ids=decision.option_ids,
        )
        ranked_ids = [item["id"] for item in ranked]
        if decision.picked_id not in ranked_ids:
            continue

        total += 1
        rank = ranked_ids.index(decision.picked_id)
        if rank == 0:
            top1 += 1.0
        mrr += 1.0 / (rank + 1)
        pairwise += (len(ranked_ids) - 1 - rank) / max(len(ranked_ids) - 1, 1)

    if total == 0:
        return {"top1": 0.0, "mrr": 0.0, "pairwise": 0.0, "n": 0}
    return {
        "top1": round(top1 / total, 4),
        "mrr": round(mrr / total, 4),
        "pairwise": round(pairwise / total, 4),
        "n": total,
    }


def merge_event_priors(
    base_priors: dict[str, dict],
    legacy_priors: dict[str, dict],
    *,
    blend_weight: float,
    metadata: dict | None = None,
) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for page_key, page_prior in base_priors.items():
        if page_key.startswith("__"):
            continue
        merged[page_key] = {option_id: dict(value) for option_id, value in page_prior.items()}

    for page_key, legacy_page in legacy_priors.items():
        if page_key.startswith("__"):
            continue
        target_page = merged.setdefault(
            page_key,
            {"__meta__": dict(legacy_page.get("__meta__", {}))},
        )
        for option_id, legacy_value in legacy_page.items():
            if option_id.startswith("__"):
                continue
            legacy_conf = min(float(legacy_value.get("confidence", 0.0)) * blend_weight, 0.45)
            if legacy_conf <= 0.01:
                continue
            if option_id not in target_page:
                target_page[option_id] = {
                    **legacy_value,
                    "confidence": round(legacy_conf, 4),
                    "offers": round(float(legacy_value.get("offers", 0.0)) * blend_weight, 3),
                    "picks": round(float(legacy_value.get("picks", 0.0)) * blend_weight, 3),
                    "samples": round(float(legacy_value.get("samples", 0.0)) * blend_weight, 3),
                }
                continue

            base_value = target_page[option_id]
            target_page[option_id] = {
                **base_value,
                "score": round(
                    float(base_value.get("score", 2.0)) * (1.0 - legacy_conf)
                    + float(legacy_value.get("score", base_value.get("score", 2.0))) * legacy_conf,
                    3,
                ),
                "confidence": round(min(max(float(base_value.get("confidence", 0.0)), legacy_conf), 0.95), 4),
                "offers": round(
                    float(base_value.get("offers", 0.0)) + float(legacy_value.get("offers", 0.0)) * blend_weight,
                    3,
                ),
                "picks": round(
                    float(base_value.get("picks", 0.0)) + float(legacy_value.get("picks", 0.0)) * blend_weight,
                    3,
                ),
                "samples": round(
                    float(base_value.get("samples", 0.0)) + float(legacy_value.get("samples", 0.0)) * blend_weight,
                    3,
                ),
            }

    if metadata:
        merged["__build_meta__"] = metadata
    return merged


def optimize_legacy_event_priors(
    valid_decisions: list[EventDecision],
    full_decisions: list[EventDecision],
    legacy_decisions: list[EventDecision],
    cards_db: list[dict],
    *,
    event_pages: list[dict],
    latest_priors: dict[str, dict],
    latest_build: str,
    build_order: list[str],
) -> dict:
    baseline_valid = evaluate_event_decisions(
        valid_decisions,
        cards_db,
        event_pages=event_pages,
        event_priors=latest_priors,
    )
    baseline_full = evaluate_event_decisions(
        full_decisions,
        cards_db,
        event_pages=event_pages,
        event_priors=latest_priors,
    )

    if not legacy_decisions:
        return {
            "applied": False,
            "reason": "no_legacy_runs",
            "baseline_valid": baseline_valid,
            "baseline_full": baseline_full,
        }

    best = {
        "score": baseline_valid["top1"] * 0.65 + baseline_valid["mrr"] * 0.25 + baseline_valid["pairwise"] * 0.10,
        "decay": None,
        "blend_weight": None,
        "valid": baseline_valid,
    }
    for decay in LEGACY_EVENT_DECAY_CANDIDATES:
        decision_weights = {
            id(decision): build_decay_weight(decision.build_id, latest_build, build_order, decay)
            for decision in legacy_decisions
        }
        legacy_priors = build_event_priors(legacy_decisions, decision_weights=decision_weights)
        for blend_weight in LEGACY_EVENT_BLEND_CANDIDATES:
            merged = merge_event_priors(latest_priors, legacy_priors, blend_weight=blend_weight)
            valid_metrics = evaluate_event_decisions(
                valid_decisions,
                cards_db,
                event_pages=event_pages,
                event_priors=merged,
            )
            score = valid_metrics["top1"] * 0.65 + valid_metrics["mrr"] * 0.25 + valid_metrics["pairwise"] * 0.10
            if score <= best["score"]:
                continue
            best = {
                "score": score,
                "decay": decay,
                "blend_weight": blend_weight,
                "valid": valid_metrics,
            }

    improvement = best["score"] - (
        baseline_valid["top1"] * 0.65 + baseline_valid["mrr"] * 0.25 + baseline_valid["pairwise"] * 0.10
    )
    if best["decay"] is None or improvement < MIN_LEGACY_EVENT_IMPROVEMENT:
        return {
            "applied": False,
            "reason": "no_valid_gain",
            "baseline_valid": baseline_valid,
            "baseline_full": baseline_full,
            "best_valid": best["valid"],
            "legacy_runs": len({decision.run_id for decision in legacy_decisions}),
        }

    full_decision_weights = {
        id(decision): build_decay_weight(decision.build_id, latest_build, build_order, best["decay"])
        for decision in legacy_decisions
    }
    legacy_priors = build_event_priors(legacy_decisions, decision_weights=full_decision_weights)
    final_priors = merge_event_priors(
        latest_priors,
        legacy_priors,
        blend_weight=best["blend_weight"],
        metadata={
            "target_build": latest_build,
            "legacy_decay": best["decay"],
            "legacy_blend_weight": best["blend_weight"],
            "legacy_builds": [build for build in build_order if build != latest_build],
            "strategy": "latest_plus_decay_corrected_legacy",
        },
    )
    final_full = evaluate_event_decisions(
        full_decisions,
        cards_db,
        event_pages=event_pages,
        event_priors=final_priors,
    )
    legacy_replay = evaluate_event_decisions(
        legacy_decisions,
        cards_db,
        event_pages=event_pages,
        event_priors=final_priors,
    )
    return {
        "applied": True,
        "decay": best["decay"],
        "blend_weight": best["blend_weight"],
        "baseline_valid": baseline_valid,
        "baseline_full": baseline_full,
        "best_valid": best["valid"],
        "final_full": final_full,
        "legacy_replay": legacy_replay,
        "priors": final_priors,
        "legacy_runs": len({decision.run_id for decision in legacy_decisions}),
    }


def save_event_build_meta(payload: dict) -> None:
    EVENT_BUILD_META_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(EVENT_BUILD_META_PATH, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    from card_db import load_card_db

    cards_db = load_card_db()
    event_pages = flatten_event_pages(load_event_db())
    all_paths = iter_history_paths()
    build_summary = summarize_builds(all_paths)
    build_order = ordered_build_ids(all_paths)
    latest_build, latest_paths, legacy_paths = split_latest_and_legacy_paths(all_paths)
    train_paths, valid_paths = split_history_paths(latest_paths)

    train_decisions = load_event_decisions(train_paths)
    valid_decisions = load_event_decisions(valid_paths)
    full_decisions = load_event_decisions(latest_paths)
    legacy_decisions = load_event_decisions(legacy_paths)

    train_priors = build_event_priors(
        train_decisions,
        metadata={
            "target_build": latest_build,
            "included_builds": [latest_build],
            "legacy_builds": [build for build in build_order if build != latest_build],
            "strategy": "latest_only_train",
        },
    )
    latest_priors = build_event_priors(
        full_decisions,
        metadata={
            "target_build": latest_build,
            "included_builds": [latest_build],
            "legacy_builds": [build for build in build_order if build != latest_build],
            "strategy": "latest_only",
        },
    )
    legacy_report = optimize_legacy_event_priors(
        valid_decisions,
        full_decisions,
        legacy_decisions,
        cards_db,
        event_pages=event_pages,
        latest_priors=train_priors,
        latest_build=latest_build,
        build_order=build_order,
    )
    priors = legacy_report["priors"] if legacy_report.get("applied") else latest_priors
    save_event_priors(priors)
    save_event_build_meta(
        {
            **build_summary,
            "selected_build": latest_build,
            "selected_run_count": len(latest_paths),
            "legacy_run_count": len(legacy_paths),
            "train_run_count": len(train_paths),
            "valid_run_count": len(valid_paths),
            "train_decision_count": len(train_decisions),
            "valid_decision_count": len(valid_decisions),
            "selected_decision_count": len(full_decisions),
            "legacy_decision_count": len(legacy_decisions),
            "legacy_strategy": (
                {
                    "applied": True,
                    "decay": legacy_report["decay"],
                    "blend_weight": legacy_report["blend_weight"],
                }
                if legacy_report.get("applied")
                else {
                    "applied": False,
                    "reason": legacy_report["reason"],
                }
            ),
        }
    )

    print(
        f"selected build: {latest_build} | latest runs: {len(latest_paths)} "
        f"| legacy runs: {len(legacy_paths)} | total runs: {len(all_paths)}"
    )
    print(f"build counts: {json.dumps(build_summary['build_counts'], ensure_ascii=False)}")
    print(
        f"train decisions: {len(train_decisions)} | valid decisions: {len(valid_decisions)} "
        f"| selected decisions: {len(full_decisions)} | legacy decisions: {len(legacy_decisions)}"
    )
    print("selected/full", evaluate_event_decisions(full_decisions, cards_db, event_pages=event_pages, event_priors=latest_priors))
    print("deployed/full", evaluate_event_decisions(full_decisions, cards_db, event_pages=event_pages, event_priors=priors))
    if legacy_decisions:
        print("deployed/legacy-replay", evaluate_event_decisions(legacy_decisions, cards_db, event_pages=event_pages, event_priors=priors))
    print("legacy correction", {
        "applied": legacy_report.get("applied", False),
        "reason": legacy_report.get("reason"),
        "decay": legacy_report.get("decay"),
        "blend_weight": legacy_report.get("blend_weight"),
    })

    page_lookup = build_event_page_lookup(event_pages)
    for sample in full_decisions[:5]:
        page = page_lookup[(sample.event_id, sample.page_id)]
        ranked = rank_event_options(
            page,
            sample.state,
            cards_db,
            event_priors=priors,
            option_ids=sample.option_ids,
        )
        print(
            f"{sample.event_name} {sample.page_id}: "
            f"{sample.picked_id} <- {[item['id'] for item in ranked[:3]]}"
        )
