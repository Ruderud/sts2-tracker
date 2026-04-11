"""STS2 상점 추천 모듈."""

from __future__ import annotations

import json
import os
import random
import re
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path

from card_db import fuzzy_match as fuzzy_match_cards
from recommender import build_scoring_context, score_card
from save_parser import CardInfo, MapCoord, MapPointInfo, MapSnapshot, RunState
from screen_capture import OCRTextLine
from utils import clean_game_text, normalize_ocr_text


DATA_DIR = Path(__file__).parent / "data"
RELICS_DB_PATH = DATA_DIR / "relics_kor.json"
POTIONS_DB_PATH = DATA_DIR / "potions_kor.json"
_PRICE_RE = re.compile(r"(?<!\d)(\d{2,4})(?!\d)")
_SHOP_SIM_ROLLOUTS = 96
_SHOP_SIM_MAX_DEPTH = 6
_SHOP_SIM_MAX_CANDIDATES = 6
_SHOP_SIM_MAX_BUNDLE_SIZE = 3
_SHOP_SIM_CACHE: dict[str, dict] = {}


@dataclass
class ShopItem:
    kind: str
    title: str
    item_id: str
    price: int | None
    match_score: float
    description: str = ""
    raw_text: str = ""
    cx: float = 0.0
    cy: float = 0.0


def _download_json(url: str, path: Path) -> None:
    os.makedirs(path.parent, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "STS2Tracker/1.0"})
    with urllib.request.urlopen(req) as resp:
        with open(path, "wb") as f:
            f.write(resp.read())


def load_relic_db() -> list[dict]:
    if not RELICS_DB_PATH.exists():
        _download_json("https://spire-codex.com/api/relics?lang=kor", RELICS_DB_PATH)
    with open(RELICS_DB_PATH) as f:
        return json.load(f)


def load_potion_db() -> list[dict]:
    if not POTIONS_DB_PATH.exists():
        _download_json("https://spire-codex.com/api/potions?lang=kor", POTIONS_DB_PATH)
    with open(POTIONS_DB_PATH) as f:
        return json.load(f)


def _fuzzy_match_named(query: str, entries: list[dict], threshold: float = 0.56) -> list[tuple[dict, float]]:
    results: list[tuple[dict, float]] = []
    query_clean = normalize_ocr_text(query)
    if len(query_clean) < 2:
        return results

    for entry in entries:
        name_clean = normalize_ocr_text(entry.get("name", ""))
        if len(name_clean) < 2:
            continue
        score = SequenceMatcher(None, query_clean, name_clean).ratio()
        if name_clean in query_clean or query_clean in name_clean:
            score = max(score, 0.92 if len(name_clean) >= 3 else 0.85)

        desc_clean = normalize_ocr_text(entry.get("description", ""))
        if len(query_clean) >= 4 and desc_clean:
            score = max(score, SequenceMatcher(None, query_clean, desc_clean[: max(len(query_clean) * 2, 12)]).ratio() * 0.72)

        if score >= threshold:
            results.append((entry, score))
    results.sort(key=lambda item: item[1], reverse=True)
    return results


def _extract_price(lines: list[OCRTextLine], *, cx: float, cy: float) -> int | None:
    best: tuple[float, int] | None = None
    for line in lines:
        match = _PRICE_RE.search(line.text.replace(",", ""))
        if not match:
            continue
        price = int(match.group(1))
        if price < 30 or price > 999:
            continue
        dx = abs(line.cx - cx)
        dy = line.cy - cy
        if dx > 0.10 or dy < -0.03 or dy > 0.18:
            continue
        score = dy * 1.2 + dx
        if best is None or score < best[0]:
            best = (score, price)
    return best[1] if best else None


def _looks_like_remove(text: str) -> bool:
    normalized = clean_game_text(text)
    return "제거" in normalized and ("카드" in normalized or "덱" in normalized)


def _resolve_merchant_price(value) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        base = value.get("base")
        if isinstance(base, (int, float)):
            return int(base)
        for key in ("min", "max"):
            candidate = value.get(key)
            if isinstance(candidate, (int, float)):
                return int(candidate)
    return None


def _score_remove(state: RunState, price: int | None, context: dict) -> tuple[float, list[str]]:
    analysis = context["analysis"]
    basics_ratio = analysis["basics"] / max(analysis["total"], 1)
    price = price or 75

    score = 2.2 + basics_ratio * 2.7
    reasons = [f"기본카드비율 {basics_ratio:.2f}"]

    if state.act == 0:
        score += 0.7
        reasons.append("Act1 제거 +0.7")
    if analysis["total"] > 14:
        bonus = min((analysis["total"] - 14) * 0.12, 0.8)
        score += bonus
        reasons.append(f"덱정리 +{bonus:.1f}")
    if analysis["basics"] <= 2:
        score -= 0.9
        reasons.append("기본카드 적음 -0.9")

    if price > state.gold:
        score -= 2.8
        reasons.insert(0, "골드 부족")
    else:
        pressure = price / max(state.gold, 1) * 0.9
        score -= pressure
        reasons.append(f"가격 -{pressure:.1f}")

    return round(max(score, 0.0), 1), reasons[:5]


def _score_relic(entry: dict, state: RunState, price: int | None, context: dict) -> tuple[float, list[str]]:
    desc = clean_game_text(entry.get("description", ""))
    needs = context["needs"]
    score = 3.0
    reasons = ["기본 유물가치 3.0"]

    if "에너지" in desc:
        score += 1.8
        reasons.append("에너지 +1.8")
    if "카드를" in desc and ("뽑" in desc or "손" in desc):
        score += 0.9
        reasons.append("드로우/손패 +0.9")
    if "방어도" in desc:
        bonus = min(0.4 + needs["block"] * 0.2, 1.0)
        score += bonus
        reasons.append(f"방어시너지 +{bonus:.1f}")
    if "피해" in desc or "힘" in desc:
        bonus = min(0.4 + needs["damage"] * 0.18, 1.0)
        score += bonus
        reasons.append(f"딜시너지 +{bonus:.1f}")
    if "포션" in desc:
        bonus = 0.45 if len(state.potions) < state.max_potion_slots else 0.15
        score += bonus
        reasons.append(f"포션활용 +{bonus:.1f}")
    if "제거" in desc or "변화" in desc:
        score += 1.0
        reasons.append("덱정제 +1.0")

    merchant_price = _resolve_merchant_price(entry.get("merchant_price")) or price or 160
    if merchant_price > state.gold:
        score -= 2.8
        reasons.insert(0, "골드 부족")
    else:
        pressure = merchant_price / max(state.gold, 1) * 1.0
        score -= pressure
        reasons.append(f"가격 -{pressure:.1f}")

    return round(max(score, 0.0), 1), reasons[:5]


def _score_potion(entry: dict, state: RunState, price: int | None, context: dict) -> tuple[float, list[str]]:
    desc = clean_game_text(entry.get("description", ""))
    hp_pct = state.current_hp / max(state.max_hp, 1)
    score = 1.7
    reasons = ["즉발 자원 1.7"]

    if len(state.potions) >= state.max_potion_slots > 0:
        score -= 0.9
        reasons.insert(0, "포션 슬롯 가득 참")
    if "피해" in desc:
        score += 0.8
        reasons.append("공격포션 +0.8")
    if "방어도" in desc or "약화" in desc or "취약" in desc:
        bonus = 0.8 if hp_pct < 0.7 else 0.5
        score += bonus
        reasons.append(f"생존보조 +{bonus:.1f}")
    if "카드" in desc or "에너지" in desc:
        score += 0.5
        reasons.append("유틸 +0.5")

    price = price or 70
    if price > state.gold:
        score -= 2.2
        reasons.insert(0, "골드 부족")
    else:
        pressure = price / max(state.gold, 1) * 0.8
        score -= pressure
        reasons.append(f"가격 -{pressure:.1f}")

    return round(max(score, 0.0), 1), reasons[:5]


def _score_card_purchase(card: dict, state: RunState, cards_db: list[dict], price: int | None, context: dict) -> tuple[float, list[str]]:
    score, reasons = score_card(card, state, cards_db, context=context)
    reasons = list(reasons)

    if price is None:
        return score, reasons[:5]

    if price > state.gold:
        score -= 3.0
        reasons.insert(0, "골드 부족")
    else:
        pressure = price / max(state.gold, 1) * 1.1
        score -= pressure
        reasons.append(f"가격 -{pressure:.1f}")
        if price <= state.gold * 0.4:
            score += 0.4
            reasons.append("가성비 +0.4")

    return round(max(score, 0.0), 1), reasons[:6]


def _coord_key(coord: MapCoord) -> tuple[int, int]:
    return coord.col, coord.row


def _lookup_points(snapshot: MapSnapshot) -> dict[tuple[int, int], MapPointInfo]:
    lookup = {_coord_key(point.coord): point for point in snapshot.points}
    if snapshot.boss_coord is not None:
        lookup.setdefault(
            _coord_key(snapshot.boss_coord),
            MapPointInfo(coord=snapshot.boss_coord, type="boss", children=[]),
        )
    return lookup


def _children_for_coord(snapshot: MapSnapshot, lookup: dict[tuple[int, int], MapPointInfo], coord: MapCoord) -> list[MapCoord]:
    if snapshot.start_coord is not None and coord == snapshot.start_coord:
        return list(snapshot.start_children)
    point = lookup.get(_coord_key(coord))
    if point is None:
        return []
    return list(point.children)


def _clone_state(state: RunState) -> RunState:
    return RunState(
        player_id=state.player_id,
        build_id=state.build_id,
        character=state.character,
        current_hp=state.current_hp,
        max_hp=state.max_hp,
        max_energy=state.max_energy,
        gold=state.gold,
        act=state.act,
        deck=[card.clone() for card in state.deck],
        relics=list(state.relics),
        max_potion_slots=state.max_potion_slots,
        seed=state.seed,
        potions=list(state.potions),
        room_type=state.room_type,
        room_model_id=state.room_model_id,
        monster_ids=list(state.monster_ids),
        floor=state.floor,
        ascension=state.ascension,
        game_mode=state.game_mode,
        modifiers=list(state.modifiers),
        map_snapshot=state.map_snapshot,
    )


def _pick_remove_index(state: RunState, context: dict, cards_db: list[dict]) -> int | None:
    card_index = context["card_index"]
    needs = context["needs"]
    prefer_remove_defend = needs["damage"] > needs["block"]

    ranked: list[tuple[tuple[float, ...], int]] = []
    for idx, card in enumerate(state.deck):
        card_id = card.id.replace("CARD.", "")
        db_card = card_index.get(card_id)
        base_priority = 20.0
        if "CURSE" in card.id or "STATUS" in card.id:
            base_priority = 0.0
        elif "STRIKE" in card.id:
            base_priority = 1.0 if not prefer_remove_defend else 2.0
        elif "DEFEND" in card.id:
            base_priority = 1.0 if prefer_remove_defend else 2.0
        elif db_card:
            base_priority = 10.0
            if db_card.get("type_key") == "Skill":
                base_priority += 0.6 if needs["block"] < 1.2 else 1.5
            if db_card.get("type_key") == "Attack":
                base_priority += 0.6 if needs["damage"] < 1.2 else 1.5
            if db_card.get("type_key") == "Power":
                base_priority += 2.4
            base_priority += float(db_card.get("cost") or 0) * 0.25
        ranked.append(((base_priority, float(card.upgrades)), idx))

    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


def _extract_sim_effects(kind: str, item_id: str, description: str) -> dict[str, float]:
    desc = clean_game_text(description)
    effects = {
        "damage": 0.0,
        "block": 0.0,
        "draw": 0.0,
        "energy": 0.0,
        "scaling": 0.0,
        "heal": 0.0,
        "shop": 0.0,
        "potion": 0.0,
        "thin": 0.0,
        "burst": 0.0,
    }

    if kind == "remove":
        effects["thin"] += 1.25
        return effects

    if "에너지" in desc:
        effects["energy"] += 1.0
    if "카드를" in desc and ("뽑" in desc or "손" in desc):
        effects["draw"] += 0.8
    if "방어도" in desc or "약화" in desc:
        effects["block"] += 0.65
    if "피해" in desc or "힘" in desc or "취약" in desc:
        effects["damage"] += 0.75
    if "회복" in desc or "체력을" in desc:
        effects["heal"] += 0.8
    if "포션" in desc:
        effects["potion"] += 1.0
    if "변형" in desc or "변화" in desc or "제거" in desc:
        effects["thin"] += 0.8
    if "영구" in desc or "매 턴" in desc:
        effects["scaling"] += 0.65
    if "상점" in desc or item_id in {"MEMBERSHIP_CARD", "THE_COURIER"}:
        effects["shop"] += 1.6

    if kind == "potion":
        effects["burst"] += 1.0
        effects["potion"] += 0.6
        if "피해" in desc:
            effects["damage"] += 0.6
        if "방어도" in desc or "약화" in desc or "취약" in desc:
            effects["block"] += 0.5

    return effects


def _collect_path_prefixes(snapshot: MapSnapshot | None) -> list[list[str]]:
    if snapshot is None or snapshot.current_coord is None:
        return []

    lookup = _lookup_points(snapshot)
    start_children = _children_for_coord(snapshot, lookup, snapshot.current_coord)
    if not start_children:
        return []

    paths: list[list[str]] = []

    def dfs(coord: MapCoord, prefix: list[str]) -> None:
        point = lookup.get(_coord_key(coord))
        if point is None:
            return
        next_prefix = prefix + [point.type]
        children = point.children
        if len(next_prefix) >= _SHOP_SIM_MAX_DEPTH or not children:
            paths.append(next_prefix)
            return
        for child in sorted(children, key=lambda item: (item.row, item.col)):
            dfs(child, next_prefix)

    for child in sorted(start_children, key=lambda item: (item.row, item.col)):
        dfs(child, [])

    if not paths:
        return []

    unique: dict[tuple[str, ...], list[str]] = {}
    for path in paths:
        unique.setdefault(tuple(path), path)
    deduped = list(unique.values())
    deduped.sort(key=lambda path: (-sum(1 for room in path if room in {"elite", "shop", "rest_site"}), len(path)))
    return deduped[:24]


def _simulate_reward_progress(needs: dict[str, float], rng: random.Random, *, elite: bool) -> None:
    keys = ["damage", "block", "draw", "scaling", "thin"]
    weights = [max(needs.get(key, 0.0), 0.1) for key in keys]
    total = sum(weights)
    pick = rng.random() * total
    cumulative = 0.0
    chosen = keys[0]
    for key, weight in zip(keys, weights):
        cumulative += weight
        if pick <= cumulative:
            chosen = key
            break

    delta = 0.52 if elite else 0.34
    needs[chosen] = max(0.0, needs.get(chosen, 0.0) - delta)
    if elite:
        needs["scaling"] = max(0.0, needs.get("scaling", 0.0) - 0.12)


def _evaluate_rollout(
    path: list[str],
    *,
    state: RunState,
    context: dict,
    effects: dict[str, float],
    rng: random.Random,
) -> float:
    hp = float(state.current_hp)
    max_hp = float(max(state.max_hp, 1))
    gold = float(state.gold)
    potion_charges = int(round(effects.get("potion", 0.0)))
    needs = {key: float(value) for key, value in context["needs"].items()}

    analysis = context["analysis"]
    total = max(analysis["total"], 1)
    value = 0.0
    value += analysis["total_damage"] / total * 0.18
    value += analysis["total_block"] / total * 0.16
    value += analysis["total_draw"] * 0.22
    value += effects["energy"] * 1.0 + effects["draw"] * 0.45 + effects["thin"] * 0.4
    value += effects["heal"] * 0.2 + effects["shop"] * 0.15

    for room in path[:_SHOP_SIM_MAX_DEPTH]:
        if room in {"monster", "elite"}:
            threat = 5.8 if room == "monster" else 10.2
            power = (
                4.6
                - needs["damage"] * 0.78
                - needs["block"] * 0.62
                - needs["draw"] * 0.24
                - needs["scaling"] * (0.18 if room == "monster" else 0.28)
                + effects["damage"] * 0.72
                + effects["block"] * 0.55
                + effects["draw"] * 0.30
                + effects["energy"] * 0.95
                + effects["scaling"] * 0.42
                + rng.uniform(-0.35, 0.35)
            )
            hp_loss = max(0.0, threat - power * (1.35 if room == "monster" else 1.18))
            if potion_charges > 0 and hp_loss > 1.5:
                hp_loss = max(0.0, hp_loss - (2.8 + effects["burst"] * 0.9))
                potion_charges -= 1
            hp -= hp_loss
            gold += 18.0 if room == "monster" else 34.0
            value += (1.25 if room == "monster" else 2.5) - hp_loss * (0.14 if room == "monster" else 0.23)
            _simulate_reward_progress(needs, rng, elite=room == "elite")
            if hp <= 0:
                return -8.0
            continue

        if room == "rest_site":
            hp_ratio = hp / max_hp
            if hp_ratio < 0.62:
                healed = max_hp * 0.24 + effects["heal"] * 2.0
                hp = min(max_hp, hp + healed)
                value += healed / max_hp * 1.4
            else:
                biggest_need = max(("damage", "block", "draw", "scaling"), key=lambda key: needs[key])
                needs[biggest_need] = max(0.0, needs[biggest_need] - 0.28)
                value += 0.95 + effects["thin"] * 0.1
            continue

        if room == "shop":
            value += min(gold / 120.0, 1.8) + effects["shop"] * 0.9 + needs["thin"] * 0.15
            continue

        if room == "treasure":
            value += 1.45 + effects["scaling"] * 0.2
            continue

        if room == "unknown":
            odds = state.map_snapshot.unknown_odds if state.map_snapshot else {}
            event_share = max(0.0, 1.0 - sum(float(v) for v in odds.values()))
            roll = rng.random()
            monster_share = float(odds.get("monster", 0.0))
            elite_share = monster_share + float(odds.get("elite", 0.0))
            shop_share = elite_share + float(odds.get("shop", 0.0))
            treasure_share = shop_share + float(odds.get("treasure", 0.0))
            if roll < monster_share:
                value += _evaluate_rollout(["monster"], state=state, context={"analysis": analysis, "needs": needs}, effects=effects, rng=rng)
            elif roll < elite_share:
                value += _evaluate_rollout(["elite"], state=state, context={"analysis": analysis, "needs": needs}, effects=effects, rng=rng)
            elif roll < shop_share:
                value += min(gold / 130.0, 1.5) + effects["shop"] * 0.8
            elif roll < treasure_share:
                value += 1.25
            else:
                value += 0.9 + event_share * 0.4 + effects["thin"] * 0.08
            continue

        if room == "boss":
            value += 0.6

    hp_ratio = hp / max_hp
    value += hp_ratio * 1.8
    value += min(gold / 160.0, 1.2)
    value -= needs["damage"] * 0.42 + needs["block"] * 0.38 + needs["scaling"] * 0.24
    return value


def _simulate_bundle_value(
    bundle_items: list[dict],
    *,
    state: RunState,
    cards_db: list[dict],
    context: dict,
    path_prefixes: list[list[str]],
    rollouts: int = _SHOP_SIM_ROLLOUTS,
) -> tuple[float, dict]:
    sim_state = _clone_state(state)
    sim_effects = {
        "damage": 0.0,
        "block": 0.0,
        "draw": 0.0,
        "energy": 0.0,
        "scaling": 0.0,
        "heal": 0.0,
        "shop": 0.0,
        "potion": 0.0,
        "thin": 0.0,
        "burst": 0.0,
    }

    for item in bundle_items:
        price = item.get("price")
        if isinstance(price, int):
            sim_state.gold = max(0, sim_state.gold - price)

        if item["kind"] == "card":
            sim_state.deck.append(CardInfo(id=f"CARD.{item['item_id']}"))
        elif item["kind"] == "remove":
            remove_idx = _pick_remove_index(sim_state, context, cards_db)
            if remove_idx is not None:
                sim_state.deck.pop(remove_idx)
        elif item["kind"] == "relic":
            relic_id = f"RELIC.{item['item_id']}"
            if relic_id not in sim_state.relics:
                sim_state.relics.append(relic_id)
        elif item["kind"] == "potion":
            potion_id = f"POTION.{item['item_id']}"
            if sim_state.max_potion_slots > 0:
                if len(sim_state.potions) < sim_state.max_potion_slots:
                    sim_state.potions.append(potion_id)
                elif sim_state.potions:
                    sim_state.potions[-1] = potion_id

        item_effects = _extract_sim_effects(item["kind"], item["item_id"], item.get("description", ""))
        for key, value in item_effects.items():
            sim_effects[key] += value

    sim_context = build_scoring_context(sim_state, cards_db)
    immediate_gain = sum(float(item["score"]) * 0.58 for item in bundle_items)

    if not path_prefixes:
        path_prefixes = [["monster", "unknown", "rest_site"]]

    seed_material = json.dumps(
        {
            "seed": state.seed,
            "gold": state.gold,
            "floor": state.floor,
            "bundle": [(item["kind"], item["item_id"], item.get("price")) for item in bundle_items],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    rng = random.Random(seed_material)

    total = 0.0
    for _ in range(rollouts):
        path = path_prefixes[rng.randrange(len(path_prefixes))]
        total += _evaluate_rollout(
            path,
            state=sim_state,
            context=sim_context,
            effects=sim_effects,
            rng=rng,
        )

    mean_future = total / max(rollouts, 1)
    final_score = immediate_gain + mean_future
    return final_score, {
        "sim_rollouts": rollouts,
        "sim_future": round(mean_future, 2),
        "sim_total": round(final_score, 2),
    }


def parse_shop_items(
    lines: list[OCRTextLine],
    *,
    cards_db: list[dict],
    relics_db: list[dict],
    potions_db: list[dict],
) -> list[ShopItem]:
    items: list[ShopItem] = []
    seen: set[tuple[str, str, int, int]] = set()

    for line in lines:
        text = clean_game_text(line.text)
        if len(text) < 2:
            continue

        if _looks_like_remove(text):
            key = ("remove", "REMOVE_CARD", int(line.cx * 10), int(line.cy * 10))
            if key in seen:
                continue
            seen.add(key)
            items.append(
                ShopItem(
                    kind="remove",
                    title="카드 제거",
                    item_id="REMOVE_CARD",
                    price=_extract_price(lines, cx=line.cx, cy=line.cy),
                    match_score=1.0,
                    description="덱에서 카드 1장을 제거합니다.",
                    raw_text=text,
                    cx=line.cx,
                    cy=line.cy,
                )
            )
            continue

        best_kind = None
        best_entry = None
        best_score = 0.0

        card_matches = fuzzy_match_cards(text, cards_db, threshold=0.58)
        if card_matches:
            best_kind, best_entry, best_score = "card", card_matches[0][0], float(card_matches[0][1])

        for kind, entries in (("relic", relics_db), ("potion", potions_db)):
            matches = _fuzzy_match_named(text, entries, threshold=0.58)
            if matches and matches[0][1] > best_score:
                best_kind, best_entry, best_score = kind, matches[0][0], float(matches[0][1])

        if not best_kind or not best_entry:
            continue

        item_id = str(best_entry["id"])
        key = (best_kind, item_id, int(line.cx * 10), int(line.cy * 10))
        if key in seen:
            continue
        seen.add(key)
        items.append(
            ShopItem(
                kind=best_kind,
                title=str(best_entry.get("name") or text),
                item_id=item_id,
                price=_extract_price(lines, cx=line.cx, cy=line.cy),
                match_score=best_score,
                description=clean_game_text(best_entry.get("description", "")),
                raw_text=text,
                cx=line.cx,
                cy=line.cy,
            )
        )

    return items


def recommend_shop_purchases(
    state: RunState,
    lines: list[OCRTextLine],
    *,
    cards_db: list[dict],
    relics_db: list[dict],
    potions_db: list[dict],
) -> dict | None:
    context = build_scoring_context(state, cards_db)
    items = parse_shop_items(lines, cards_db=cards_db, relics_db=relics_db, potions_db=potions_db)
    if not items:
        return None

    card_index = context["card_index"]
    relic_index = {entry["id"]: entry for entry in relics_db}
    potion_index = {entry["id"]: entry for entry in potions_db}

    scored = []
    for item in items:
        if item.kind == "remove":
            score, reasons = _score_remove(state, item.price, context)
        elif item.kind == "card":
            card = card_index.get(item.item_id)
            if not card:
                continue
            score, reasons = _score_card_purchase(card, state, cards_db, item.price, context)
        elif item.kind == "relic":
            entry = relic_index.get(item.item_id)
            if not entry:
                continue
            score, reasons = _score_relic(entry, state, item.price, context)
        else:
            entry = potion_index.get(item.item_id)
            if not entry:
                continue
            score, reasons = _score_potion(entry, state, item.price, context)

        scored.append(
            {
                "kind": item.kind,
                "kind_label": {
                    "card": "카드",
                    "relic": "유물",
                    "potion": "포션",
                    "remove": "제거",
                }.get(item.kind, item.kind),
                "title": item.title,
                "item_id": item.item_id,
                "price": item.price,
                "price_known": item.price is not None,
                "affordable": item.price is None or item.price <= state.gold,
                "match_pct": round(item.match_score * 100),
                "score": score,
                "reasons": reasons,
                "description": item.description,
            }
        )

    scored.sort(key=lambda item: (item["score"], item["match_pct"]), reverse=True)
    if not scored:
        return None

    bundle = _build_purchase_bundle(
        scored,
        state=state,
        cards_db=cards_db,
        context=context,
    )
    bundle_keys = {
        (item["kind"], item["item_id"], item["price"])
        for item in bundle["items"]
    }
    for item in scored:
        item["recommended_buy"] = (item["kind"], item["item_id"], item["price"]) in bundle_keys

    return {
        "gold": state.gold,
        "best_idx": next((idx for idx, item in enumerate(scored) if item.get("recommended_buy")), 0),
        "bundle": bundle,
        "items": scored[:8],
    }


def _build_purchase_bundle(
    items: list[dict],
    *,
    state: RunState,
    cards_db: list[dict],
    context: dict,
) -> dict:
    gold = state.gold
    priced_candidates = [
        item
        for item in items
        if isinstance(item.get("price"), int)
        and item["price"] <= gold
        and float(item["score"]) >= 1.8
    ]
    priced_candidates = priced_candidates[:_SHOP_SIM_MAX_CANDIDATES]

    path_prefixes = _collect_path_prefixes(state.map_snapshot)
    cache_key = json.dumps(
        {
            "seed": state.seed,
            "floor": state.floor,
            "gold": state.gold,
            "deck": [card.display_id for card in state.deck],
            "relics": state.relics,
            "potions": state.potions,
            "candidates": [
                (item["kind"], item["item_id"], item.get("price"), round(float(item["score"]), 2))
                for item in priced_candidates
            ],
            "paths": path_prefixes,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    cached = _SHOP_SIM_CACHE.get(cache_key)
    if cached is not None:
        return cached

    best_bundle: list[dict] = []
    best_total = _simulate_bundle_value(
        [],
        state=state,
        cards_db=cards_db,
        context=context,
        path_prefixes=path_prefixes,
    )[0]
    best_meta = {
        "sim_rollouts": _SHOP_SIM_ROLLOUTS,
        "sim_future": round(best_total, 2),
        "sim_total": round(best_total, 2),
    }
    best_cost = 0
    best_non_empty_bundle: list[dict] = []
    best_non_empty_total = float("-inf")
    best_non_empty_meta: dict | None = None
    best_non_empty_cost = 0

    count = len(priced_candidates)
    for size in range(1, min(_SHOP_SIM_MAX_BUNDLE_SIZE, count) + 1):
        for indexes in combinations(range(count), size):
            bundle = [priced_candidates[idx] for idx in indexes]
            total_cost = sum(int(item["price"]) for item in bundle)
            if total_cost > gold:
                continue
            total_score, meta = _simulate_bundle_value(
                bundle,
                state=state,
                cards_db=cards_db,
                context=context,
                path_prefixes=path_prefixes,
            )
            if (
                total_score > best_non_empty_total
                or (
                    abs(total_score - best_non_empty_total) < 1e-9
                    and total_cost > best_non_empty_cost
                )
            ):
                best_non_empty_total = total_score
                best_non_empty_bundle = bundle
                best_non_empty_cost = total_cost
                best_non_empty_meta = meta
            if (
                total_score > best_total
                or (
                    abs(total_score - best_total) < 1e-9
                    and total_cost > best_cost
                )
            ):
                best_total = total_score
                best_bundle = bundle
                best_cost = total_cost
                best_meta = meta

    if (
        not best_bundle
        and best_non_empty_bundle
        and (
            best_non_empty_total >= best_total - 0.45
            or max(float(item["score"]) for item in best_non_empty_bundle) >= 4.2
        )
    ):
        best_bundle = best_non_empty_bundle
        best_total = best_non_empty_total
        best_cost = best_non_empty_cost
        best_meta = best_non_empty_meta or best_meta

    best_bundle = sorted(
        best_bundle,
        key=lambda item: (float(item["score"]), -(int(item["price"]) or 0)),
        reverse=True,
    )
    result = {
        "items": [
            {
                "kind": item["kind"],
                "kind_label": item["kind_label"],
                "title": item["title"],
                "item_id": item["item_id"],
                "price": item["price"],
                "score": item["score"],
            }
            for item in best_bundle
        ],
        "total_cost": best_cost,
        "remaining_gold": max(gold - best_cost, 0),
        "total_score": round(best_total, 1),
        **best_meta,
    }
    if len(_SHOP_SIM_CACHE) > 32:
        _SHOP_SIM_CACHE.clear()
    _SHOP_SIM_CACHE[cache_key] = result
    return result
