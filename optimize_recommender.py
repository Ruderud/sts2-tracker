"""실제 런 히스토리로 추천기를 벤치마크하고 전역/캐릭터별 priors를 튜닝한다."""

from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from build_meta import (
    build_decay_weight,
    ordered_build_ids,
    split_latest_and_legacy_paths,
    summarize_builds,
)
from card_db import load_card_db
from history_replay import RewardDecision, iter_history_paths, load_reward_decisions
from recommender import (
    ScoringWeights,
    build_scoring_context,
    load_card_priors,
    load_character_card_priors,
    load_relic_card_priors,
    load_scoring_weights,
    reset_loaded_assets,
    save_card_priors,
    save_relic_card_priors,
    save_character_card_priors,
    save_character_scoring_weights,
    save_scoring_weights,
    score_card,
    score_skip,
)
from save_parser import get_local_steam_user_id


RNG_SEED = 20260411
GLOBAL_SEARCH_ITERATIONS = 600
CHARACTER_SEARCH_ITERATIONS = 400
RELIC_SEARCH_ITERATIONS = 500
VALID_RATIO = 0.2
MIN_CHARACTER_VALID_IMPROVEMENT = 0.003
MIN_RELIC_VALID_IMPROVEMENT = 0.002
MIN_LEGACY_VALID_IMPROVEMENT = 0.002
LEGACY_DECAY_CANDIDATES = (0.35, 0.5, 0.65, 0.8)
LEGACY_BLEND_CANDIDATES = (0.15, 0.25, 0.35, 0.5)
BUILD_META_PATH = Path(__file__).parent / "data" / "recommender_build_meta.json"


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


def split_character_paths(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    group = sorted(paths)
    valid_count = max(1, round(len(group) * VALID_RATIO)) if len(group) > 1 else 0
    return group[:-valid_count] if valid_count else group, group[-valid_count:] if valid_count else []


def build_prior_stats(
    decisions: list[RewardDecision],
    *,
    decision_weights: dict[int, float] | None = None,
) -> dict:
    offers = Counter()
    picks = Counter()
    picked_wins = Counter()
    ratings = defaultdict(lambda: 1500.0)

    non_skip_picks = 0.0
    winning_picks = 0.0
    for decision in decisions:
        weight = max((decision_weights or {}).get(id(decision), 1.0), 0.0)
        if weight <= 0.0:
            continue
        for card_id in decision.option_ids:
            offers[card_id] += weight

        if decision.picked_id != "SKIP":
            picks[decision.picked_id] += weight
            non_skip_picks += weight
            if decision.won_run:
                picked_wins[decision.picked_id] += weight
                winning_picks += weight

    for _ in range(3):
        for decision in decisions:
            weight = max((decision_weights or {}).get(id(decision), 1.0), 0.0)
            if weight <= 0.0:
                continue
            winner = decision.picked_id
            if winner == "SKIP":
                continue
            for loser in decision.option_ids:
                if loser == winner:
                    continue
                rating_w = ratings[winner]
                rating_l = ratings[loser]
                expected_w = 1.0 / (1.0 + 10 ** ((rating_l - rating_w) / 400.0))
                delta = 24.0 * weight * (1.0 - expected_w)
                ratings[winner] = rating_w + delta
                ratings[loser] = rating_l - delta

    return {
        "offers": offers,
        "picks": picks,
        "picked_wins": picked_wins,
        "ratings": ratings,
        "global_pick_rate": non_skip_picks / max(sum(offers.values()), 1),
        "global_win_rate": winning_picks / max(non_skip_picks, 1),
    }


def build_card_priors(
    stats: dict,
    weights: ScoringWeights,
    params: dict,
    *,
    metadata: dict | None = None,
) -> dict[str, dict]:
    priors: dict[str, dict] = {}
    offers: Counter = stats["offers"]
    picks: Counter = stats["picks"]
    picked_wins: Counter = stats["picked_wins"]
    ratings: defaultdict = stats["ratings"]
    global_pick_rate = stats["global_pick_rate"]
    global_win_rate = stats["global_win_rate"]

    min_offers = params["min_offers"]
    for card_id, offer_count in offers.items():
        if offer_count < min_offers:
            continue

        pick_count = picks[card_id]
        picked_win_count = picked_wins[card_id]
        pick_rate = (pick_count + 0.75) / (offer_count + 1.5)
        picked_win_rate = (
            (picked_win_count + 0.75) / (pick_count + 1.5)
            if pick_count
            else global_win_rate
        )
        score = weights.default_base
        score += (ratings[card_id] - 1500.0) / params["rating_divisor"]
        score += (pick_rate - global_pick_rate) * params["pick_scale"]
        score += (picked_win_rate - global_win_rate) * params["win_scale"]
        score = max(0.6, min(5.4, score))

        confidence = offer_count / (offer_count + params["confidence_divisor"])
        priors[card_id] = {
            "score": round(score, 4),
            "confidence": round(confidence, 4),
            "offers": round(offer_count, 3),
            "picks": round(pick_count, 3),
            "rating": round(ratings[card_id], 2),
        }

    priors["__meta__"] = {
        "global_pick_rate": round(global_pick_rate, 4),
        "global_win_rate": round(global_win_rate, 4),
    }
    if metadata:
        priors["__build_meta__"] = metadata
    return priors


def infer_starting_relics(decisions: list[RewardDecision]) -> dict[str, set[str]]:
    earliest_by_run: dict[tuple[str, str], RewardDecision] = {}
    for decision in decisions:
        key = (decision.run_id, decision.character)
        current = earliest_by_run.get(key)
        if current is None or decision.floor < current.floor:
            earliest_by_run[key] = decision

    by_char: dict[str, list[set[str]]] = defaultdict(list)
    for decision in earliest_by_run.values():
        by_char[decision.character].append(set(decision.state.relics))

    result: dict[str, set[str]] = {}
    for character, relic_sets in by_char.items():
        result[character] = set.intersection(*relic_sets) if relic_sets else set()
    return result


def build_relic_prior_stats(
    decisions: list[RewardDecision],
    *,
    starting_relics: dict[str, set[str]] | None = None,
    decision_weights: dict[int, float] | None = None,
) -> dict:
    card_offers = Counter()
    card_picks = Counter()
    card_pick_wins = Counter()
    relic_usage = Counter()
    relic_card_offers = Counter()
    relic_card_picks = Counter()
    relic_card_pick_wins = Counter()

    for decision in decisions:
        weight = max((decision_weights or {}).get(id(decision), 1.0), 0.0)
        if weight <= 0.0:
            continue
        picked = decision.picked_id
        if picked != "SKIP":
            card_picks[picked] += weight
            if decision.won_run:
                card_pick_wins[picked] += weight

        blocked_relics = (starting_relics or {}).get(decision.character, set())
        unique_relics = {relic for relic in set(decision.state.relics) if relic not in blocked_relics}
        for card_id in decision.option_ids:
            card_offers[card_id] += weight
            for relic_id in unique_relics:
                relic_card_offers[(relic_id, card_id)] += weight

        if not unique_relics:
            continue

        for relic_id in unique_relics:
            relic_usage[relic_id] += weight
            if picked != "SKIP":
                relic_card_picks[(relic_id, picked)] += weight
                if decision.won_run:
                    relic_card_pick_wins[(relic_id, picked)] += weight

    return {
        "card_offers": card_offers,
        "card_picks": card_picks,
        "card_pick_wins": card_pick_wins,
        "relic_usage": relic_usage,
        "relic_card_offers": relic_card_offers,
        "relic_card_picks": relic_card_picks,
        "relic_card_pick_wins": relic_card_pick_wins,
    }


def build_relic_card_priors(stats: dict, params: dict) -> dict[str, dict[str, dict]]:
    priors: dict[str, dict[str, dict]] = defaultdict(dict)

    card_offers: Counter = stats["card_offers"]
    card_picks: Counter = stats["card_picks"]
    card_pick_wins: Counter = stats["card_pick_wins"]
    relic_usage: Counter = stats["relic_usage"]
    relic_card_offers: Counter = stats["relic_card_offers"]
    relic_card_picks: Counter = stats["relic_card_picks"]
    relic_card_pick_wins: Counter = stats["relic_card_pick_wins"]

    min_pair_offers = params["min_pair_offers"]
    min_relic_usage = params["min_relic_usage"]
    confidence_divisor = params["confidence_divisor"]
    pick_scale = params["pick_scale"]
    win_scale = params["win_scale"]
    max_bonus = params["max_bonus"]

    for (relic_id, card_id), offer_count in relic_card_offers.items():
        if offer_count < min_pair_offers or relic_usage[relic_id] < min_relic_usage:
            continue

        pair_pick_count = relic_card_picks[(relic_id, card_id)]
        pair_pick_rate = (pair_pick_count + 0.5) / (offer_count + 1.0)

        base_offer_count = card_offers[card_id]
        base_pick_count = card_picks[card_id]
        base_pick_rate = (base_pick_count + 0.5) / (base_offer_count + 1.0)

        pair_pick_win_count = relic_card_pick_wins[(relic_id, card_id)]
        pair_pick_win_rate = (
            (pair_pick_win_count + 0.5) / (pair_pick_count + 1.0)
            if pair_pick_count
            else 0.5
        )
        base_pick_win_rate = (
            (card_pick_wins[card_id] + 0.5) / (base_pick_count + 1.0)
            if base_pick_count
            else 0.5
        )

        score_bonus = (pair_pick_rate - base_pick_rate) * pick_scale
        score_bonus += (pair_pick_win_rate - base_pick_win_rate) * win_scale
        score_bonus = max(-max_bonus, min(max_bonus, score_bonus))

        confidence = offer_count / (offer_count + confidence_divisor)
        if abs(score_bonus) * confidence < 0.03:
            continue

        priors[relic_id][card_id] = {
            "score_bonus": round(score_bonus, 4),
            "confidence": round(confidence, 4),
            "offers": round(offer_count, 3),
            "picks": round(pair_pick_count, 3),
        }

    return {relic_id: dict(card_priors) for relic_id, card_priors in priors.items()}


def decision_weights_by_build(
    decisions: list[RewardDecision],
    *,
    latest_build: str,
    build_order: list[str],
    decay: float,
) -> dict[int, float]:
    return {
        id(decision): build_decay_weight(decision.build_id, latest_build, build_order, decay)
        for decision in decisions
    }


def merge_card_priors(
    base_priors: dict[str, dict],
    legacy_priors: dict[str, dict],
    *,
    blend_weight: float,
    metadata: dict | None = None,
) -> dict[str, dict]:
    merged: dict[str, dict] = {
        card_id: dict(value)
        for card_id, value in base_priors.items()
        if not card_id.startswith("__")
    }

    for card_id, legacy in legacy_priors.items():
        if card_id.startswith("__"):
            continue
        legacy_conf = min(float(legacy.get("confidence", 0.0)) * blend_weight, 0.45)
        if legacy_conf <= 0.01:
            continue

        if card_id not in merged:
            merged[card_id] = {
                **legacy,
                "confidence": round(legacy_conf, 4),
                "offers": round(float(legacy.get("offers", 0.0)) * blend_weight, 3),
                "picks": round(float(legacy.get("picks", 0.0)) * blend_weight, 3),
            }
            continue

        base = merged[card_id]
        merged[card_id] = {
            **base,
            "score": round(
                float(base.get("score", 2.0)) * (1.0 - legacy_conf)
                + float(legacy.get("score", base.get("score", 2.0))) * legacy_conf,
                4,
            ),
            "confidence": round(min(max(float(base.get("confidence", 0.0)), legacy_conf), 0.95), 4),
            "offers": round(
                float(base.get("offers", 0.0)) + float(legacy.get("offers", 0.0)) * blend_weight,
                3,
            ),
            "picks": round(
                float(base.get("picks", 0.0)) + float(legacy.get("picks", 0.0)) * blend_weight,
                3,
            ),
            "rating": round(
                float(base.get("rating", 1500.0)) * (1.0 - legacy_conf)
                + float(legacy.get("rating", base.get("rating", 1500.0))) * legacy_conf,
                2,
            ),
        }

    merged["__meta__"] = dict(base_priors.get("__meta__", {}))
    if metadata:
        merged["__build_meta__"] = metadata
    return merged


def benchmark(
    decisions: list[RewardDecision],
    cards_db: list[dict],
    global_weights: ScoringWeights,
    global_priors: dict[str, dict],
    *,
    character_models: dict[str, dict] | None = None,
    relic_priors: dict[str, dict[str, dict]] | None = None,
) -> dict:
    card_index = {card["id"]: card for card in cards_db}
    if character_models is None:
        character_models = {}

    total = 0
    top1 = 0
    mrr = 0.0
    pairwise_hits = 0
    pairwise_total = 0
    missing = 0
    by_char = defaultdict(lambda: {"total": 0, "top1": 0, "mrr": 0.0})

    for decision in decisions:
        character_model = character_models.get(decision.character, {})
        context = build_scoring_context(
            decision.state,
            cards_db,
            weights=global_weights,
            card_priors=global_priors,
            character_weights=character_model.get("weights"),
            character_priors=character_model.get("priors", {}),
            relic_priors=relic_priors,
        )

        scored = []
        for card_id in decision.option_ids:
            card = card_index.get(card_id)
            if card is None:
                missing += 1
                continue
            score, _ = score_card(card, decision.state, cards_db, context=context)
            scored.append((card_id, score))

        if len(scored) != len(decision.option_ids):
            continue

        skip_score, _ = score_skip(decision.state, cards_db, context=context)
        scored.append(("SKIP", skip_score))

        ranked = sorted(scored, key=lambda item: item[1], reverse=True)
        total += 1
        by_char[decision.character]["total"] += 1

        if ranked[0][0] == decision.picked_id:
            top1 += 1
            by_char[decision.character]["top1"] += 1

        rank = next(i for i, (card_id, _) in enumerate(ranked, 1) if card_id == decision.picked_id)
        reciprocal_rank = 1.0 / rank
        mrr += reciprocal_rank
        by_char[decision.character]["mrr"] += reciprocal_rank

        chosen_score = next(score for card_id, score in scored if card_id == decision.picked_id)
        for card_id, score in scored:
            if card_id == decision.picked_id:
                continue
            pairwise_total += 1
            if chosen_score >= score:
                pairwise_hits += 1

    return {
        "total": total,
        "top1": top1 / max(total, 1),
        "mrr": mrr / max(total, 1),
        "pairwise": pairwise_hits / max(pairwise_total, 1),
        "missing": missing,
        "by_char": {
            char: {
                "total": stats["total"],
                "top1": stats["top1"] / max(stats["total"], 1),
                "mrr": stats["mrr"] / max(stats["total"], 1),
            }
            for char, stats in sorted(by_char.items())
        },
    }


def objective(metrics: dict) -> float:
    return metrics["top1"] * 0.65 + metrics["mrr"] * 0.25 + metrics["pairwise"] * 0.10


def sample_weights(rng: random.Random) -> ScoringWeights:
    return ScoringWeights(
        default_base=rng.uniform(1.7, 2.4),
        damage_scale=rng.uniform(0.012, 0.045),
        block_scale=rng.uniform(0.015, 0.055),
        draw_scale=rng.uniform(0.12, 0.65),
        power_scale=rng.uniform(0.18, 0.95),
        star_scale=rng.uniform(0.35, 1.45),
        thin_scale=rng.uniform(0.18, 0.95),
        zero_cost_bonus=rng.uniform(0.10, 0.95),
        high_cost_penalty=rng.uniform(0.10, 0.95),
        deck_penalty_scale=rng.uniform(0.05, 0.26),
        duplicate_penalty=rng.uniform(0.08, 0.75),
        duplicate_plus_penalty=rng.uniform(0.35, 1.75),
        act1_attack_bonus=rng.uniform(0.0, 0.95),
        act1_power_bonus=rng.uniform(0.0, 0.50),
        skip_base=rng.uniform(0.70, 2.60),
        skip_deck_scale=rng.uniform(0.08, 0.52),
        skip_thin_bonus=rng.uniform(0.0, 1.20),
    )


def sample_prior_params(rng: random.Random, *, low_sample: bool = False) -> dict:
    return {
        "rating_divisor": rng.uniform(150.0, 420.0),
        "pick_scale": rng.uniform(0.0, 2.8 if not low_sample else 2.1),
        "win_scale": rng.uniform(0.0, 2.4 if not low_sample else 1.6),
        "confidence_divisor": rng.uniform(1.0, 10.0 if not low_sample else 14.0),
        "min_offers": rng.choice([1, 2, 2, 3, 4] if not low_sample else [1, 1, 2, 2, 3]),
    }


def sample_relic_params(rng: random.Random) -> dict:
    return {
        "pick_scale": rng.uniform(0.6, 4.2),
        "win_scale": rng.uniform(0.0, 1.8),
        "confidence_divisor": rng.uniform(2.0, 18.0),
        "min_pair_offers": rng.choice([2, 3, 3, 4, 5]),
        "min_relic_usage": rng.choice([4, 5, 6, 8]),
        "max_bonus": rng.uniform(0.25, 0.95),
    }


def format_metrics(label: str, metrics: dict) -> str:
    return (
        f"{label}: top1={metrics['top1']:.4f} "
        f"mrr={metrics['mrr']:.4f} pairwise={metrics['pairwise']:.4f} "
        f"n={metrics['total']}"
    )


def optimize_global_model(
    rng: random.Random,
    cards_db: list[dict],
    train_decisions: list[RewardDecision],
    valid_decisions: list[RewardDecision],
) -> dict:
    train_stats = build_prior_stats(train_decisions)
    baseline_weights = ScoringWeights()
    empty_priors: dict[str, dict] = {}

    baseline_train = benchmark(train_decisions, cards_db, baseline_weights, empty_priors)
    baseline_valid = benchmark(valid_decisions, cards_db, baseline_weights, empty_priors)

    best = {
        "score": objective(baseline_valid),
        "weights": baseline_weights,
        "prior_params": {
            "rating_divisor": 260.0,
            "pick_scale": 0.0,
            "win_scale": 0.0,
            "confidence_divisor": 6.0,
            "min_offers": 99,
        },
        "train": baseline_train,
        "valid": baseline_valid,
    }

    for _ in range(GLOBAL_SEARCH_ITERATIONS):
        weights = sample_weights(rng)
        prior_params = sample_prior_params(rng)
        priors = build_card_priors(train_stats, weights, prior_params)
        valid_metrics = benchmark(valid_decisions, cards_db, weights, priors)
        score = objective(valid_metrics)
        if score <= best["score"]:
            continue

        best = {
            "score": score,
            "weights": weights,
            "prior_params": prior_params,
            "train": benchmark(train_decisions, cards_db, weights, priors),
            "valid": valid_metrics,
        }

    return best


def optimize_character_model(
    rng: random.Random,
    character: str,
    paths: list[Path],
    cards_db: list[dict],
    global_weights: ScoringWeights,
    global_priors: dict[str, dict],
) -> dict:
    train_paths, valid_paths = split_character_paths(paths)
    train_decisions = load_reward_decisions(train_paths)
    valid_decisions = load_reward_decisions(valid_paths)
    full_decisions = load_reward_decisions(paths)

    baseline_valid = benchmark(valid_decisions, cards_db, global_weights, global_priors)
    baseline_full = benchmark(full_decisions, cards_db, global_weights, global_priors)

    if not train_decisions or not valid_decisions:
        return {
            "character": character,
            "applied": False,
            "reason": "not_enough_runs",
            "baseline_valid": baseline_valid,
            "baseline_full": baseline_full,
        }

    train_stats = build_prior_stats(train_decisions)
    low_sample = len(train_decisions) < 60
    best = {
        "score": objective(baseline_valid),
        "prior_params": None,
        "valid": baseline_valid,
    }

    for _ in range(CHARACTER_SEARCH_ITERATIONS):
        prior_params = sample_prior_params(rng, low_sample=low_sample)
        priors = build_card_priors(train_stats, global_weights, prior_params)
        valid_metrics = benchmark(
            valid_decisions,
            cards_db,
            global_weights,
            global_priors,
            character_models={character: {"priors": priors}},
        )
        score = objective(valid_metrics)
        if score <= best["score"]:
            continue

        best = {
            "score": score,
            "prior_params": prior_params,
            "valid": valid_metrics,
        }

    improvement = best["score"] - objective(baseline_valid)
    if best["prior_params"] is None or improvement < MIN_CHARACTER_VALID_IMPROVEMENT:
        return {
            "character": character,
            "applied": False,
            "reason": "no_valid_gain",
            "baseline_valid": baseline_valid,
            "baseline_full": baseline_full,
            "best_valid": best["valid"],
            "decisions": len(full_decisions),
        }

    full_stats = build_prior_stats(full_decisions)
    final_priors = build_card_priors(full_stats, global_weights, best["prior_params"])
    final_full = benchmark(
        full_decisions,
        cards_db,
        global_weights,
        global_priors,
        character_models={character: {"priors": final_priors}},
    )

    return {
        "character": character,
        "applied": True,
        "priors": final_priors,
        "prior_params": best["prior_params"],
        "baseline_valid": baseline_valid,
        "baseline_full": baseline_full,
        "best_valid": best["valid"],
        "final_full": final_full,
        "improvement": improvement,
        "decisions": len(full_decisions),
    }


def optimize_relic_model(
    rng: random.Random,
    cards_db: list[dict],
    train_decisions: list[RewardDecision],
    valid_decisions: list[RewardDecision],
    full_decisions: list[RewardDecision],
    global_weights: ScoringWeights,
    global_priors: dict[str, dict],
    character_models: dict[str, dict],
) -> dict:
    baseline_valid = benchmark(
        valid_decisions,
        cards_db,
        global_weights,
        global_priors,
        character_models=character_models,
    )
    baseline_full = benchmark(
        full_decisions,
        cards_db,
        global_weights,
        global_priors,
        character_models=character_models,
    )

    starting_relics = infer_starting_relics(train_decisions)
    train_stats = build_relic_prior_stats(train_decisions, starting_relics=starting_relics)
    best = {
        "score": objective(baseline_valid),
        "params": None,
        "valid": baseline_valid,
    }

    for _ in range(RELIC_SEARCH_ITERATIONS):
        params = sample_relic_params(rng)
        priors = build_relic_card_priors(train_stats, params)
        valid_metrics = benchmark(
            valid_decisions,
            cards_db,
            global_weights,
            global_priors,
            character_models=character_models,
            relic_priors=priors,
        )
        score = objective(valid_metrics)
        if score <= best["score"]:
            continue

        best = {
            "score": score,
            "params": params,
            "valid": valid_metrics,
        }

    improvement = best["score"] - objective(baseline_valid)
    if best["params"] is None or improvement < MIN_RELIC_VALID_IMPROVEMENT:
        return {
            "applied": False,
            "reason": "no_valid_gain",
            "baseline_valid": baseline_valid,
            "baseline_full": baseline_full,
            "best_valid": best["valid"],
            "relic_count": 0,
        }

    full_stats = build_relic_prior_stats(full_decisions, starting_relics=starting_relics)
    final_priors = build_relic_card_priors(full_stats, best["params"])
    final_full = benchmark(
        full_decisions,
        cards_db,
        global_weights,
        global_priors,
        character_models=character_models,
        relic_priors=final_priors,
    )
    return {
        "applied": True,
        "params": best["params"],
        "priors": final_priors,
        "baseline_valid": baseline_valid,
        "baseline_full": baseline_full,
        "best_valid": best["valid"],
        "final_full": final_full,
        "improvement": improvement,
        "relic_count": len(final_priors),
    }


def optimize_legacy_supplement(
    cards_db: list[dict],
    valid_decisions: list[RewardDecision],
    full_decisions: list[RewardDecision],
    legacy_decisions: list[RewardDecision],
    global_weights: ScoringWeights,
    valid_priors: dict[str, dict],
    full_priors: dict[str, dict],
    prior_params: dict,
    *,
    latest_build: str,
    build_order: list[str],
    character_models: dict[str, dict] | None = None,
    relic_priors: dict[str, dict[str, dict]] | None = None,
) -> dict:
    if character_models is None:
        character_models = {}

    baseline_valid = benchmark(
        valid_decisions,
        cards_db,
        global_weights,
        valid_priors,
        character_models=character_models,
        relic_priors=relic_priors,
    )
    baseline_full = benchmark(
        full_decisions,
        cards_db,
        global_weights,
        full_priors,
        character_models=character_models,
        relic_priors=relic_priors,
    )

    if not legacy_decisions:
        return {
            "applied": False,
            "reason": "no_legacy_runs",
            "baseline_valid": baseline_valid,
            "baseline_full": baseline_full,
        }

    best = {
        "score": objective(baseline_valid),
        "decay": None,
        "blend_weight": None,
        "valid": baseline_valid,
    }

    for decay in LEGACY_DECAY_CANDIDATES:
        legacy_weights = decision_weights_by_build(
            legacy_decisions,
            latest_build=latest_build,
            build_order=build_order,
            decay=decay,
        )
        legacy_stats = build_prior_stats(legacy_decisions, decision_weights=legacy_weights)
        legacy_priors = build_card_priors(legacy_stats, global_weights, prior_params)
        for blend_weight in LEGACY_BLEND_CANDIDATES:
            merged = merge_card_priors(valid_priors, legacy_priors, blend_weight=blend_weight)
            valid_metrics = benchmark(
                valid_decisions,
                cards_db,
                global_weights,
                merged,
                character_models=character_models,
                relic_priors=relic_priors,
            )
            score = objective(valid_metrics)
            if score <= best["score"]:
                continue

            best = {
                "score": score,
                "decay": decay,
                "blend_weight": blend_weight,
                "valid": valid_metrics,
            }

    improvement = best["score"] - objective(baseline_valid)
    if best["decay"] is None or improvement < MIN_LEGACY_VALID_IMPROVEMENT:
        return {
            "applied": False,
            "reason": "no_valid_gain",
            "baseline_valid": baseline_valid,
            "baseline_full": baseline_full,
            "best_valid": best["valid"],
            "legacy_runs": len({decision.run_id for decision in legacy_decisions}),
        }

    full_legacy_weights = decision_weights_by_build(
        legacy_decisions,
        latest_build=latest_build,
        build_order=build_order,
        decay=best["decay"],
    )
    full_legacy_stats = build_prior_stats(legacy_decisions, decision_weights=full_legacy_weights)
    legacy_priors = build_card_priors(full_legacy_stats, global_weights, prior_params)
    final_priors = merge_card_priors(
        full_priors,
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
    final_full = benchmark(
        full_decisions,
        cards_db,
        global_weights,
        final_priors,
        character_models=character_models,
        relic_priors=relic_priors,
    )
    legacy_replay = benchmark(
        legacy_decisions,
        cards_db,
        global_weights,
        final_priors,
        character_models=character_models,
        relic_priors=relic_priors,
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
        "improvement": improvement,
        "legacy_runs": len({decision.run_id for decision in legacy_decisions}),
    }


def save_build_meta(payload: dict) -> None:
    BUILD_META_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(BUILD_META_PATH, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def main() -> None:
    rng = random.Random(RNG_SEED)
    cards_db = load_card_db()
    all_paths = iter_history_paths()
    build_summary = summarize_builds(all_paths)
    build_order = ordered_build_ids(all_paths)
    latest_build, latest_paths, legacy_paths = split_latest_and_legacy_paths(all_paths)
    train_paths, valid_paths = split_history_paths(latest_paths)

    train_decisions = load_reward_decisions(train_paths)
    valid_decisions = load_reward_decisions(valid_paths)
    full_decisions = load_reward_decisions(latest_paths)
    legacy_decisions = load_reward_decisions(legacy_paths)

    global_best = optimize_global_model(rng, cards_db, train_decisions, valid_decisions)
    train_stats = build_prior_stats(train_decisions)
    train_global_priors = build_card_priors(
        train_stats,
        global_best["weights"],
        global_best["prior_params"],
        metadata={
            "target_build": latest_build,
            "included_builds": [latest_build],
            "legacy_builds": [build for build in build_order if build != latest_build],
            "strategy": "latest_only_train",
        },
    )
    full_stats = build_prior_stats(full_decisions)
    global_priors = build_card_priors(
        full_stats,
        global_best["weights"],
        global_best["prior_params"],
        metadata={
            "target_build": latest_build,
            "included_builds": [latest_build],
            "legacy_builds": [build for build in build_order if build != latest_build],
            "strategy": "latest_only",
        },
    )
    global_full = benchmark(full_decisions, cards_db, global_best["weights"], global_priors)

    by_char_paths: dict[str, list[Path]] = defaultdict(list)
    for path in latest_paths:
        by_char_paths[load_run_character(path)].append(path)

    character_models: dict[str, dict] = {}
    character_reports: dict[str, dict] = {}
    for character, paths in sorted(by_char_paths.items()):
        report = optimize_character_model(
            rng,
            character,
            sorted(paths),
            cards_db,
            global_best["weights"],
            global_priors,
        )
        character_reports[character] = report
        if report.get("applied"):
            character_models[character] = {"priors": report["priors"]}

    mixed_full = benchmark(
        full_decisions,
        cards_db,
        global_best["weights"],
        global_priors,
        character_models=character_models,
    )

    relic_report = optimize_relic_model(
        rng,
        cards_db,
        train_decisions,
        valid_decisions,
        full_decisions,
        global_best["weights"],
        global_priors,
        character_models,
    )
    relic_priors = relic_report["priors"] if relic_report.get("applied") else {}
    relic_mixed_full = benchmark(
        full_decisions,
        cards_db,
        global_best["weights"],
        global_priors,
        character_models=character_models,
        relic_priors=relic_priors,
    )
    legacy_report = optimize_legacy_supplement(
        cards_db,
        valid_decisions,
        full_decisions,
        legacy_decisions,
        global_best["weights"],
        train_global_priors,
        global_priors,
        global_best["prior_params"],
        latest_build=latest_build,
        build_order=build_order,
        character_models=character_models,
        relic_priors=relic_priors,
    )
    deployed_priors_to_save = legacy_report["priors"] if legacy_report.get("applied") else global_priors

    save_scoring_weights(global_best["weights"])
    save_card_priors(deployed_priors_to_save)
    save_character_scoring_weights({})
    save_character_card_priors(
        {character: model["priors"] for character, model in character_models.items()}
    )
    save_relic_card_priors(relic_priors)
    save_build_meta(
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
    reset_loaded_assets()
    deployed_weights = load_scoring_weights()
    deployed_priors = load_card_priors()
    deployed_character_models = {
        character: {"priors": priors}
        for character, priors in load_character_card_priors().items()
    }
    deployed_relic_priors = load_relic_card_priors()
    deployed_full = benchmark(
        full_decisions,
        cards_db,
        deployed_weights,
        deployed_priors,
        character_models=deployed_character_models,
        relic_priors=deployed_relic_priors,
    )

    print(
        f"selected build: {latest_build} | latest runs: {len(latest_paths)} "
        f"| legacy runs: {len(legacy_paths)} | total runs: {len(all_paths)}"
    )
    print(f"build counts: {json.dumps(build_summary['build_counts'], ensure_ascii=False)}")
    print(f"train runs: {len(train_paths)} | valid runs: {len(valid_paths)} | selected runs: {len(latest_paths)}")
    print(
        f"train decisions: {len(train_decisions)} | valid decisions: {len(valid_decisions)} "
        f"| selected decisions: {len(full_decisions)} | legacy decisions: {len(legacy_decisions)}"
    )
    print(f"global validation evaluations: {GLOBAL_SEARCH_ITERATIONS * len(valid_decisions)}")
    print(f"relic validation evaluations: {RELIC_SEARCH_ITERATIONS * len(valid_decisions)}")
    print(format_metrics("global/baseline/train", global_best["train"]))
    print(format_metrics("global/baseline/valid", global_best["valid"]))
    print(format_metrics("global/best/full", global_full))
    print(format_metrics("mixed/full", mixed_full))
    print(format_metrics("relic+mixed/full", relic_mixed_full))
    print(format_metrics("deployed/full", deployed_full))
    if legacy_decisions:
        print(format_metrics("deployed/legacy-replay", legacy_report.get("legacy_replay", benchmark(
            legacy_decisions,
            cards_db,
            deployed_weights,
            deployed_priors,
            character_models=deployed_character_models,
            relic_priors=deployed_relic_priors,
        ))))
    print()
    print("global weights")
    print(json.dumps(global_best["weights"].to_dict(), ensure_ascii=False, indent=2))
    print()
    print("global prior params")
    print(json.dumps(global_best["prior_params"], ensure_ascii=False, indent=2))
    print()
    print("character reports")
    for character, report in sorted(character_reports.items()):
        base_valid = report["baseline_valid"]
        base_full = report["baseline_full"]
        if report.get("applied"):
            best_valid = report["best_valid"]
            final_full = report["final_full"]
            print(
                f"{character:12s} applied  "
                f"valid {base_valid['top1']:.4f}->{best_valid['top1']:.4f} "
                f"full {base_full['top1']:.4f}->{final_full['top1']:.4f} "
                f"decisions={report['decisions']}"
            )
        else:
            best_valid = report.get("best_valid", base_valid)
            print(
                f"{character:12s} fallback "
                f"valid {base_valid['top1']:.4f}->{best_valid['top1']:.4f} "
                f"reason={report['reason']} decisions={report.get('decisions', 0)}"
            )
    print()
    print("relic report")
    if relic_report.get("applied"):
        print(
            "applied  "
            f"valid {relic_report['baseline_valid']['top1']:.4f}->{relic_report['best_valid']['top1']:.4f} "
            f"full {relic_report['baseline_full']['top1']:.4f}->{relic_report['final_full']['top1']:.4f} "
            f"relics={relic_report['relic_count']}"
        )
        print(json.dumps(relic_report["params"], ensure_ascii=False, indent=2))
    else:
        print(
            "fallback  "
            f"valid {relic_report['baseline_valid']['top1']:.4f}->{relic_report['best_valid']['top1']:.4f} "
            f"reason={relic_report['reason']}"
        )
    print()
    print("legacy correction")
    if legacy_report.get("applied"):
        print(
            "applied  "
            f"valid {legacy_report['baseline_valid']['top1']:.4f}->{legacy_report['best_valid']['top1']:.4f} "
            f"full {legacy_report['baseline_full']['top1']:.4f}->{legacy_report['final_full']['top1']:.4f} "
            f"decay={legacy_report['decay']:.2f} blend={legacy_report['blend_weight']:.2f} "
            f"legacy_runs={legacy_report['legacy_runs']}"
        )
    else:
        print(
            "fallback  "
            f"valid {legacy_report['baseline_valid']['top1']:.4f}->{legacy_report.get('best_valid', legacy_report['baseline_valid'])['top1']:.4f} "
            f"reason={legacy_report['reason']}"
        )
    print()
    print("mixed per-character")
    for character, metrics in deployed_full["by_char"].items():
        print(f"{character:12s} top1={metrics['top1']:.4f} mrr={metrics['mrr']:.4f} n={metrics['total']}")


if __name__ == "__main__":
    main()
