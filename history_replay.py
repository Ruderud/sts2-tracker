"""런 히스토리(.run)에서 카드 보상 의사결정 데이터셋을 재생성한다."""

from __future__ import annotations

import json
import re
from glob import glob
from dataclasses import dataclass
from pathlib import Path

from event_db import filter_options_for_state
from save_parser import CardInfo, RunState, get_local_steam_user_id


HISTORY_GLOB = (
    Path.home()
    / "Library/Application Support/SlayTheSpire2/steam"
    / "*"
    / "profile1/saves/history/*.run"
)

COMBAT_REWARD_TYPES = {"monster", "elite", "boss"}
CARD_ID_ALIASES = {
    "PREPARE": "PREPARED",
}
EVENT_OPTION_KEY_RE = re.compile(
    r"^(?P<event>[A-Z0-9_]+)\.pages\.(?P<page>[A-Z0-9_]+)\.options\.(?P<option>[A-Z0-9_]+)\.title$"
)


@dataclass
class RelicInfo:
    id: str
    floor_added: int | None = None

    def clone(self) -> "RelicInfo":
        return RelicInfo(id=self.id, floor_added=self.floor_added)


@dataclass
class RewardDecision:
    run_id: str
    build_id: str
    character: str
    act: int
    floor: int
    room_type: str
    won_run: bool
    state: RunState
    option_ids: list[str]
    picked_id: str


@dataclass
class EventDecision:
    run_id: str
    build_id: str
    character: str
    act: int
    floor: int
    room_type: str
    won_run: bool
    event_id: str
    event_name: str
    page_id: str
    state: RunState
    option_ids: list[str]
    picked_id: str


def iter_history_paths() -> list[Path]:
    """로컬 히스토리 파일 목록."""
    return sorted(Path(path) for path in glob(str(HISTORY_GLOB)))


def normalize_card_id(card_id: str) -> str:
    normalized = card_id.replace("CARD.", "")
    return CARD_ID_ALIASES.get(normalized, normalized)


def _normalize_player_id(value) -> str:
    return str(value) if value is not None else ""


def _props_key(obj: dict | None) -> str:
    if not obj:
        return ""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def card_info_from_dict(card: dict) -> CardInfo:
    enchantment = card.get("enchantment") or {}
    return CardInfo(
        id=card["id"],
        upgrades=card.get("current_upgrade_level", card.get("upgrades", 0)),
        floor_added=card.get("floor_added_to_deck"),
        enchantment_id=enchantment.get("id"),
        enchantment_amount=enchantment.get("amount", 0),
        props_key=_props_key(card.get("props")),
    )


def relic_info_from_dict(relic: dict | str) -> RelicInfo:
    if isinstance(relic, str):
        return RelicInfo(id=relic)
    return RelicInfo(
        id=relic["id"],
        floor_added=relic.get("floor_added_to_deck"),
    )


def parse_event_choice_key(key: str) -> tuple[str, str, str] | None:
    """이벤트 choice localization key에서 (event, page, option) 추출."""
    match = EVENT_OPTION_KEY_RE.match(key)
    if not match:
        return None
    return match.group("event"), match.group("page"), match.group("option")


def _select_run_player(players: list[dict], preferred_player_id: str | None) -> tuple[dict, str]:
    if preferred_player_id:
        for player in players:
            player_id = _normalize_player_id(player.get("id") or player.get("player_id"))
            if player_id == preferred_player_id:
                return player, player_id

    player = players[0]
    return player, _normalize_player_id(player.get("id") or player.get("player_id") or preferred_player_id)


def _select_player_stats(player_stats: list[dict], preferred_player_id: str | None) -> dict | None:
    if not player_stats:
        return None
    if preferred_player_id:
        for stats in player_stats:
            if _normalize_player_id(stats.get("player_id")) == preferred_player_id:
                return stats
    return player_stats[0]


def _extract_room_metadata(point: dict) -> tuple[str, str, list[str]]:
    rooms = point.get("rooms") or []
    room = rooms[0] if rooms else {}
    return (
        room.get("room_type", point.get("map_point_type", "")),
        room.get("model_id", ""),
        list(room.get("monster_ids") or []),
    )


def _flatten_points(run_data: dict) -> list[tuple[int, int, dict]]:
    points: list[tuple[int, int, dict]] = []
    floor = 0
    for act_index, act_points in enumerate(run_data.get("map_point_history", [])):
        for point in act_points:
            floor += 1
            points.append((act_index, floor, point))
    return points


def _card_matches(
    card: CardInfo,
    needle: CardInfo,
    *,
    exact_upgrade: bool = False,
    exact_enchantment: bool = False,
) -> bool:
    if card.id != needle.id:
        return False
    if needle.floor_added is not None and card.floor_added is not None:
        if card.floor_added != needle.floor_added:
            return False
    if needle.props_key and card.props_key and needle.props_key != card.props_key:
        return False
    if exact_upgrade and card.upgrades != needle.upgrades:
        return False
    if exact_enchantment and card.enchantment_id != needle.enchantment_id:
        return False
    return True


def _remove_matching_card(
    deck: list[CardInfo],
    needle: CardInfo,
    *,
    exact_upgrade: bool = False,
    exact_enchantment: bool = False,
) -> bool:
    for idx, card in enumerate(deck):
        if _card_matches(
            card,
            needle,
            exact_upgrade=exact_upgrade,
            exact_enchantment=exact_enchantment,
        ):
            deck.pop(idx)
            return True

    for idx, card in enumerate(deck):
        if card.id == needle.id:
            deck.pop(idx)
            return True
    return False


def _strip_enchantment(deck: list[CardInfo], needle: CardInfo) -> None:
    for card in deck:
        if _card_matches(
            card,
            needle,
            exact_upgrade=True,
            exact_enchantment=True,
        ):
            card.enchantment_id = None
            card.enchantment_amount = 0
            return

    for card in deck:
        if _card_matches(card, needle):
            card.enchantment_id = None
            card.enchantment_amount = 0
            return


def _downgrade_card(deck: list[CardInfo], card_id: str, floor: int) -> None:
    candidates = [
        (idx, card)
        for idx, card in enumerate(deck)
        if card.id == card_id
        and (card.floor_added is None or card.floor_added <= floor)
        and card.upgrades > 0
    ]
    if not candidates:
        return

    candidates.sort(
        key=lambda item: (
            item[1].upgrades,
            item[1].floor_added or -1,
            bool(item[1].enchantment_id),
        ),
        reverse=True,
    )
    idx, card = candidates[0]
    deck[idx] = CardInfo(
        id=card.id,
        upgrades=max(card.upgrades - 1, 0),
        floor_added=card.floor_added,
        enchantment_id=card.enchantment_id,
        enchantment_amount=card.enchantment_amount,
        props_key=card.props_key,
    )


def _reverse_stats_mutations(
    stats: dict,
    current_deck: list[CardInfo],
    current_relics: list[RelicInfo],
    current_potions: list[str],
    floor: int,
) -> None:
    for enchanted in stats.get("cards_enchanted", []):
        _strip_enchantment(current_deck, card_info_from_dict(enchanted["card"]))

    for upgraded_id in stats.get("upgraded_cards", []):
        _downgrade_card(current_deck, upgraded_id, floor)

    for transformed in stats.get("cards_transformed", []):
        _remove_matching_card(
            current_deck,
            card_info_from_dict(transformed["final_card"]),
            exact_upgrade=False,
            exact_enchantment=False,
        )
        current_deck.append(card_info_from_dict(transformed["original_card"]))

    for removed in stats.get("cards_removed", []):
        current_deck.append(card_info_from_dict(removed))

    for removed_relic in stats.get("relics_removed", []):
        current_relics.append(relic_info_from_dict(removed_relic))

    for picked_potion in _picked_potion_ids(stats):
        _remove_matching_potion(current_potions, picked_potion)
    for potion_id in stats.get("potion_used") or []:
        current_potions.append(potion_id)
    for potion_id in stats.get("potion_discarded") or []:
        current_potions.append(potion_id)


def _visible_deck(current_deck: list[CardInfo], floor: int) -> list[CardInfo]:
    return [
        card.clone()
        for card in current_deck
        if card.floor_added is None or card.floor_added <= floor
    ]


def _visible_relics(current_relics: list[RelicInfo], floor: int) -> list[str]:
    return [
        relic.id
        for relic in current_relics
        if relic.floor_added is None or relic.floor_added <= floor
    ]


def _visible_relic_infos(current_relics: list[RelicInfo], floor: int) -> list[RelicInfo]:
    return [
        relic.clone()
        for relic in current_relics
        if relic.floor_added is None or relic.floor_added <= floor
    ]


def _visible_potions(current_potions: list[str]) -> list[str]:
    return list(current_potions)


def _remove_matching_relic(relics: list[RelicInfo], relic_id: str) -> bool:
    for idx, relic in enumerate(relics):
        if relic.id == relic_id:
            relics.pop(idx)
            return True
    return False


def _remove_matching_potion(potions: list[str], potion_id: str) -> bool:
    for idx, existing in enumerate(potions):
        if existing == potion_id:
            potions.pop(idx)
            return True
    return False


def _picked_relic_ids(stats: dict) -> list[str]:
    picked = []
    for choice in stats.get("relic_choices") or []:
        relic_id = choice.get("choice")
        if relic_id and choice.get("was_picked"):
            picked.append(relic_id)
    return picked


def _picked_potion_ids(stats: dict) -> list[str]:
    picked = []
    for choice in stats.get("potion_choices") or []:
        potion_id = choice.get("choice")
        if potion_id and choice.get("was_picked"):
            picked.append(potion_id)
    return picked


def _pre_event_hp(stats: dict) -> tuple[int, int]:
    current_hp = int(stats.get("current_hp", 0))
    max_hp = int(stats.get("max_hp", 0))
    max_hp_gained = int(stats.get("max_hp_gained", 0))
    max_hp_lost = int(stats.get("max_hp_lost", 0))
    damage_taken = int(stats.get("damage_taken", 0))
    hp_healed = int(stats.get("hp_healed", 0))

    pre_max_hp = max(max_hp - max_hp_gained + max_hp_lost, 1)
    pre_current_hp = current_hp + damage_taken - hp_healed
    pre_current_hp = max(1, min(pre_current_hp, pre_max_hp))
    return pre_current_hp, pre_max_hp


def _pre_event_gold(stats: dict) -> int:
    current_gold = int(stats.get("current_gold", 0))
    gold_gained = int(stats.get("gold_gained", 0))
    gold_lost = int(stats.get("gold_lost", 0))
    gold_spent = int(stats.get("gold_spent", 0))
    gold_stolen = int(stats.get("gold_stolen", 0))
    return max(current_gold - gold_gained + gold_lost + gold_spent + gold_stolen, 0)


def _build_pre_event_state(
    character: str,
    player_id: str,
    build_id: str,
    seed: str,
    act: int,
    floor: int,
    ascension: int,
    game_mode: str,
    modifiers: list[str],
    max_potion_slots: int,
    point: dict,
    current_deck: list[CardInfo],
    current_relics: list[RelicInfo],
    current_potions: list[str],
    stats: dict,
) -> RunState:
    deck_snapshot = _visible_deck(current_deck, floor)
    relic_snapshot = _visible_relic_infos(current_relics, floor)
    potion_snapshot = _visible_potions(current_potions)

    for gained_card in stats.get("cards_gained") or []:
        _remove_matching_card(deck_snapshot, card_info_from_dict(gained_card))

    for picked_relic in _picked_relic_ids(stats):
        _remove_matching_relic(relic_snapshot, picked_relic)

    _reverse_stats_mutations(stats, deck_snapshot, relic_snapshot, potion_snapshot, floor)
    current_hp, max_hp = _pre_event_hp(stats)
    room_type, room_model_id, monster_ids = _extract_room_metadata(point)

    return RunState(
        player_id=player_id,
        build_id=build_id,
        character=character,
        current_hp=current_hp,
        max_hp=max_hp,
        gold=_pre_event_gold(stats),
        act=act,
        deck=deck_snapshot,
        relics=[relic.id for relic in relic_snapshot],
        max_potion_slots=max_potion_slots,
        seed=seed,
        potions=potion_snapshot,
        room_type=room_type,
        room_model_id=room_model_id,
        monster_ids=monster_ids,
        floor=floor,
        ascension=ascension,
        game_mode=game_mode,
        modifiers=list(modifiers),
    )


def _extract_decision(
    path: Path,
    character: str,
    player_id: str,
    build_id: str,
    seed: str,
    won_run: bool,
    act: int,
    floor: int,
    ascension: int,
    game_mode: str,
    modifiers: list[str],
    max_potion_slots: int,
    point: dict,
    stats: dict,
    current_deck: list[CardInfo],
    current_relics: list[RelicInfo],
    current_potions: list[str],
) -> RewardDecision | None:
    if point.get("map_point_type") not in COMBAT_REWARD_TYPES:
        return None

    if not stats:
        return None

    choices = stats.get("card_choices") or []
    if len(choices) != 3:
        return None

    option_ids = []
    picked_cards = []
    for choice in choices:
        card = choice.get("card") or {}
        card_id = card.get("id")
        if not card_id:
            return None
        option_ids.append(normalize_card_id(card_id))
        if choice.get("was_picked"):
            picked_cards.append(card)

    deck_snapshot = _visible_deck(current_deck, floor)
    potion_snapshot = _visible_potions(current_potions)
    for picked_card in picked_cards:
        _remove_matching_card(deck_snapshot, card_info_from_dict(picked_card))
    for picked_potion in _picked_potion_ids(stats):
        _remove_matching_potion(potion_snapshot, picked_potion)

    room_type, room_model_id, monster_ids = _extract_room_metadata(point)

    state = RunState(
        player_id=player_id,
        build_id=build_id,
        character=character,
        current_hp=stats.get("current_hp", 0),
        max_hp=stats.get("max_hp", 0),
        gold=stats.get("current_gold", 0),
        act=act,
        deck=deck_snapshot,
        relics=_visible_relics(current_relics, floor),
        max_potion_slots=max_potion_slots,
        seed=seed,
        potions=potion_snapshot,
        room_type=room_type,
        room_model_id=room_model_id,
        monster_ids=monster_ids,
        floor=floor,
        ascension=ascension,
        game_mode=game_mode,
        modifiers=list(modifiers),
    )

    picked_id = normalize_card_id(picked_cards[0]["id"]) if picked_cards else "SKIP"
    return RewardDecision(
        run_id=path.stem,
        build_id=build_id,
        character=character,
        act=act,
        floor=floor,
        room_type=room_type,
        won_run=won_run,
        state=state,
        option_ids=option_ids,
        picked_id=picked_id,
    )


def _copy_state(state: RunState) -> RunState:
    return RunState(
        player_id=state.player_id,
        build_id=state.build_id,
        character=state.character,
        current_hp=state.current_hp,
        max_hp=state.max_hp,
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
    )


def _find_option(page: dict, option_id: str) -> dict | None:
    for option in page["options"]:
        if option["id"] == option_id:
            return option
    return None


def _infer_picked_option_id(
    page: dict,
    candidate_ids: list[str],
    stats: dict,
    next_cluster: list[tuple[str, str, str]] | None,
) -> str | None:
    next_page_id = next_cluster[0][1] if next_cluster else ""
    scored: list[tuple[float, str]] = []

    for option_id in candidate_ids:
        option = _find_option(page, option_id)
        if option is None:
            continue

        title = option.get("title", "")
        desc = option.get("description", "")
        text = f"{title}\n{desc}"
        score = 0.0

        if next_page_id and option_id == next_page_id:
            score += 5.0
        if stats.get("cards_removed") and "제거" in text:
            score += 3.0
        if stats.get("cards_transformed") and "변화" in text:
            score += 3.0
        if (stats.get("upgraded_cards") or stats.get("cards_enchanted")) and (
            "강화" in text or "인챈트" in text
        ):
            score += 2.5
        if stats.get("relic_choices") and ("유물" in text or "지도" in text):
            score += 2.0
        if stats.get("potion_choices") and "포션" in text:
            score += 1.5
        if stats.get("cards_gained") and ("카드" in text and ("추가" in text or "선택" in text)):
            score += 2.0
        if stats.get("damage_taken") and "잃습니다" in text:
            score += 1.5
        if stats.get("gold_lost") or stats.get("gold_spent"):
            if "골드" in text and ("지불" in text or "준다" in title):
                score += 2.0
        if stats.get("gold_gained") and "골드" in text and "얻" in text:
            score += 1.5
        if not any(
            stats.get(key)
            for key in (
                "cards_removed",
                "cards_transformed",
                "upgraded_cards",
                "cards_enchanted",
                "relic_choices",
                "potion_choices",
                "cards_gained",
                "gold_gained",
                "gold_lost",
                "gold_spent",
                "damage_taken",
            )
        ) and any(word in title for word in ("떠난다", "나아간다", "지나친다")):
            score += 1.0
        scored.append((score, option_id))

    if not scored:
        return candidate_ids[-1] if candidate_ids else None
    scored.sort(reverse=True)
    return scored[0][1]


def _cluster_explicit_option_ids(page: dict, cluster: list[tuple[str, str, str]]) -> list[str] | None:
    unique_ids = []
    for _, _, option_id in cluster:
        if option_id not in unique_ids:
            unique_ids.append(option_id)

    if len(unique_ids) >= 2 and len(unique_ids) <= 3 and len(page["options"]) <= len(unique_ids) + 1:
        return unique_ids
    return None


def _extract_event_decisions(
    path: Path,
    character: str,
    player_id: str,
    build_id: str,
    seed: str,
    won_run: bool,
    act: int,
    floor: int,
    ascension: int,
    game_mode: str,
    modifiers: list[str],
    max_potion_slots: int,
    point: dict,
    stats: dict,
    current_deck: list[CardInfo],
    current_relics: list[RelicInfo],
    current_potions: list[str],
    page_lookup: dict[tuple[str, str], dict],
) -> list[EventDecision]:
    room_type, _, _ = _extract_room_metadata(point)
    if room_type != "event":
        return []

    if not stats:
        return []

    parsed_choices = []
    for raw_choice in stats.get("event_choices") or []:
        title = raw_choice.get("title") or {}
        if title.get("table") != "events":
            continue
        parsed = parse_event_choice_key(title.get("key", ""))
        if parsed is not None:
            parsed_choices.append(parsed)

    if not parsed_choices:
        return []

    state = _build_pre_event_state(
        character=character,
        player_id=player_id,
        build_id=build_id,
        seed=seed,
        act=act,
        floor=floor,
        ascension=ascension,
        game_mode=game_mode,
        modifiers=modifiers,
        max_potion_slots=max_potion_slots,
        point=point,
        current_deck=current_deck,
        current_relics=current_relics,
        current_potions=current_potions,
        stats=stats,
    )

    decisions: list[EventDecision] = []
    clusters: list[list[tuple[str, str, str]]] = []
    for choice in parsed_choices:
        if clusters and clusters[-1][0][:2] == choice[:2]:
            clusters[-1].append(choice)
        else:
            clusters.append([choice])

    for idx, cluster in enumerate(clusters):
        event_id, page_id, picked_id = cluster[0]
        page = page_lookup.get((event_id, page_id))
        if page is None:
            continue

        explicit_option_ids = _cluster_explicit_option_ids(page, cluster)
        if explicit_option_ids is not None:
            option_ids = explicit_option_ids
            inferred = _infer_picked_option_id(
                page,
                option_ids,
                stats,
                clusters[idx + 1] if idx + 1 < len(clusters) else None,
            )
            if inferred is None:
                continue
            picked_ids = [inferred]
        else:
            option_ids = [option["id"] for option in filter_options_for_state(page, state)]
            picked_ids = [option_id for _, _, option_id in cluster]

        if len(option_ids) < 2:
            continue

        for picked_id in picked_ids:
            if picked_id not in option_ids:
                continue
            decisions.append(
                EventDecision(
                    run_id=path.stem,
                    build_id=build_id,
                    character=character,
                    act=act,
                    floor=floor,
                    room_type=room_type,
                    won_run=won_run,
                    event_id=event_id,
                    event_name=page["event_name"],
                    page_id=page_id,
                    state=_copy_state(state),
                    option_ids=list(option_ids),
                    picked_id=picked_id,
                )
            )
    return decisions


def extract_reward_decisions(path: Path) -> list[RewardDecision]:
    """한 개 런 파일에서 전투 카드 보상 데이터셋을 뽑는다."""
    with open(path) as f:
        run_data = json.load(f)

    players = run_data.get("players") or []
    if not players:
        return []

    preferred_player_id = get_local_steam_user_id(path)
    player, player_id = _select_run_player(players, preferred_player_id)
    character = player["character"].replace("CHARACTER.", "")
    current_deck = [card_info_from_dict(card) for card in player.get("deck", [])]
    current_relics = [relic_info_from_dict(relic) for relic in player.get("relics", [])]
    current_potions = [potion["id"] for potion in player.get("potions", []) if potion.get("id")]
    build_id = run_data.get("build_id", "")
    max_potion_slots = int(player.get("max_potion_slot_count") or 0)
    ascension = int(run_data.get("ascension") or 0)
    game_mode = run_data.get("game_mode", "")
    modifiers = list(run_data.get("modifiers") or [])

    decisions: list[RewardDecision] = []
    for act, floor, point in reversed(_flatten_points(run_data)):
        stats = _select_player_stats(point.get("player_stats") or [], player_id)
        decision = _extract_decision(
            path=path,
            character=character,
            player_id=player_id,
            build_id=build_id,
            seed=run_data.get("seed", ""),
            won_run=bool(run_data.get("win")),
            act=act,
            floor=floor,
            ascension=ascension,
            game_mode=game_mode,
            modifiers=modifiers,
            max_potion_slots=max_potion_slots,
            point=point,
            stats=stats,
            current_deck=current_deck,
            current_relics=current_relics,
            current_potions=current_potions,
        )
        if decision is not None:
            decisions.append(decision)

        if stats is not None:
            _reverse_stats_mutations(stats, current_deck, current_relics, current_potions, floor)

    decisions.reverse()
    return decisions


def extract_event_decisions(
    path: Path,
    page_lookup: dict[tuple[str, str], dict],
) -> list[EventDecision]:
    """한 개 런 파일에서 이벤트 선택 의사결정 데이터셋을 뽑는다."""
    with open(path) as f:
        run_data = json.load(f)

    players = run_data.get("players") or []
    if not players:
        return []

    preferred_player_id = get_local_steam_user_id(path)
    player, player_id = _select_run_player(players, preferred_player_id)
    character = player["character"].replace("CHARACTER.", "")
    current_deck = [card_info_from_dict(card) for card in player.get("deck", [])]
    current_relics = [relic_info_from_dict(relic) for relic in player.get("relics", [])]
    current_potions = [potion["id"] for potion in player.get("potions", []) if potion.get("id")]
    build_id = run_data.get("build_id", "")
    max_potion_slots = int(player.get("max_potion_slot_count") or 0)
    ascension = int(run_data.get("ascension") or 0)
    game_mode = run_data.get("game_mode", "")
    modifiers = list(run_data.get("modifiers") or [])

    decisions: list[EventDecision] = []
    for act, floor, point in reversed(_flatten_points(run_data)):
        stats = _select_player_stats(point.get("player_stats") or [], player_id)
        decisions.extend(
            _extract_event_decisions(
                path=path,
                character=character,
                player_id=player_id,
                build_id=build_id,
                seed=run_data.get("seed", ""),
                won_run=bool(run_data.get("win")),
                act=act,
                floor=floor,
                ascension=ascension,
                game_mode=game_mode,
                modifiers=modifiers,
                max_potion_slots=max_potion_slots,
                point=point,
                stats=stats,
                current_deck=current_deck,
                current_relics=current_relics,
                current_potions=current_potions,
                page_lookup=page_lookup,
            )
        )
        if stats is not None:
            _reverse_stats_mutations(stats, current_deck, current_relics, current_potions, floor)

    decisions.reverse()
    return decisions


def load_reward_decisions(paths: list[Path] | None = None) -> list[RewardDecision]:
    """전체 히스토리에서 전투 카드 보상 의사결정 로드."""
    if paths is None:
        paths = iter_history_paths()

    decisions: list[RewardDecision] = []
    for path in paths:
        decisions.extend(extract_reward_decisions(path))
    return decisions


def load_event_decisions(paths: list[Path] | None = None) -> list[EventDecision]:
    """전체 히스토리에서 이벤트 의사결정 로드."""
    if paths is None:
        paths = iter_history_paths()

    from event_db import build_event_page_lookup, flatten_event_pages, load_event_db

    page_lookup = build_event_page_lookup(flatten_event_pages(load_event_db()))
    decisions: list[EventDecision] = []
    for path in paths:
        decisions.extend(extract_event_decisions(path, page_lookup))
    return decisions


if __name__ == "__main__":
    paths = iter_history_paths()
    decisions = load_reward_decisions(paths)
    print(f"runs: {len(paths)}")
    print(f"reward decisions: {len(decisions)}")
    for sample in decisions[:5]:
        print(
            f"{sample.run_id} {sample.character} A{sample.act + 1} F{sample.floor}"
            f" {sample.option_ids} -> {sample.picked_id}"
        )
    event_decisions = load_event_decisions(paths)
    print(f"event decisions: {len(event_decisions)}")
    for sample in event_decisions[:5]:
        print(
            f"{sample.run_id} {sample.event_id}.{sample.page_id}"
            f" {sample.option_ids} -> {sample.picked_id}"
        )
