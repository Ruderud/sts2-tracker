"""STS2 세이브 파일 파서 - current_run.save에서 게임 상태 추출."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

STS2_DATA_DIR = Path.home() / "Library/Application Support/SlayTheSpire2"
_STEAM_ID_RE = re.compile(r"/steam/(?P<steam_id>\d+)/")


def _find_steam_user_dir() -> Path | None:
    """Steam 유저 디렉토리 자동 탐지."""
    steam_dir = STS2_DATA_DIR / "steam"
    if not steam_dir.exists():
        return None
    for user_dir in steam_dir.iterdir():
        if user_dir.is_dir() and user_dir.name.isdigit():
            return user_dir
    return None


@dataclass
class CardInfo:
    id: str
    upgrades: int = 0
    floor_added: int | None = None
    enchantment_id: str | None = None
    enchantment_amount: int = 0
    props_key: str = ""

    def clone(self) -> "CardInfo":
        return CardInfo(
            id=self.id,
            upgrades=self.upgrades,
            floor_added=self.floor_added,
            enchantment_id=self.enchantment_id,
            enchantment_amount=self.enchantment_amount,
            props_key=self.props_key,
        )

    @property
    def display_id(self) -> str:
        base = self.id.replace("CARD.", "")
        return f"{base}+{self.upgrades}" if self.upgrades > 0 else base


@dataclass(frozen=True)
class MapCoord:
    col: int
    row: int

    def to_dict(self) -> dict:
        return {"col": self.col, "row": self.row}


@dataclass
class MapPointInfo:
    coord: MapCoord
    type: str
    children: list[MapCoord] = field(default_factory=list)
    can_modify: bool = True


@dataclass
class MapSnapshot:
    act_id: str = ""
    width: int = 0
    height: int = 0
    start_coord: MapCoord | None = None
    current_coord: MapCoord | None = None
    boss_coord: MapCoord | None = None
    start_children: list[MapCoord] = field(default_factory=list)
    points: list[MapPointInfo] = field(default_factory=list)
    unknown_odds: dict[str, float] = field(default_factory=dict)


@dataclass
class RunState:
    player_id: str = ""
    build_id: str = ""
    character: str = ""
    current_hp: int = 0
    max_hp: int = 0
    max_energy: int = 3
    gold: int = 0
    act: int = 0
    deck: list[CardInfo] = field(default_factory=list)
    relics: list[str] = field(default_factory=list)
    max_potion_slots: int = 0
    seed: str = ""
    potions: list[str] = field(default_factory=list)
    room_type: str = ""
    room_model_id: str = ""
    monster_ids: list[str] = field(default_factory=list)
    floor: int = 0
    ascension: int = 0
    game_mode: str = ""
    modifiers: list[str] = field(default_factory=list)
    map_snapshot: MapSnapshot | None = field(default=None, repr=False)


def get_local_steam_user_id(path: Path | None = None) -> str | None:
    """현재 세이브/워크스페이스 기준 로컬 Steam 유저 ID 추론."""
    if path is not None:
        match = _STEAM_ID_RE.search(str(path))
        if match:
            return match.group("steam_id")

    user_dir = _find_steam_user_dir()
    if user_dir is not None:
        return user_dir.name
    return None


def _normalize_player_id(value) -> str:
    return str(value) if value is not None else ""


def _select_player(players: list[dict], preferred_player_id: str | None) -> dict:
    if preferred_player_id:
        for player in players:
            player_id = _normalize_player_id(player.get("id") or player.get("player_id"))
            if player_id == preferred_player_id:
                return player
    return players[0]


def _extract_room_metadata(data: dict) -> tuple[str, str, list[str]]:
    room = data.get("current_room") or data.get("room") or data.get("pre_finished_room", {})
    if not isinstance(room, dict):
        room = {}
    return (
        room.get("room_type", ""),
        room.get("model_id", ""),
        list(room.get("monster_ids") or []),
    )


def _parse_coord(raw: dict | None) -> MapCoord | None:
    if not isinstance(raw, dict):
        return None
    if "col" not in raw or "row" not in raw:
        return None
    return MapCoord(col=int(raw["col"]), row=int(raw["row"]))


def _parse_map_snapshot(data: dict) -> MapSnapshot | None:
    acts = data.get("acts") or []
    act_index = int(data.get("current_act_index") or 0)
    if act_index < 0 or act_index >= len(acts):
        return None

    act = acts[act_index]
    saved_map = act.get("saved_map") or {}
    if not saved_map:
        return None

    points: list[MapPointInfo] = []
    for point in saved_map.get("points") or []:
        coord = _parse_coord(point.get("coord"))
        if coord is None:
            continue
        children = [
            child
            for child in (_parse_coord(raw_child) for raw_child in point.get("children") or [])
            if child is not None
        ]
        points.append(
            MapPointInfo(
                coord=coord,
                type=str(point.get("type") or ""),
                children=children,
                can_modify=bool(point.get("can_modify", True)),
            )
        )

    visited = data.get("visited_map_coords") or []
    current_coord = None
    if visited:
        current_coord = _parse_coord(visited[-1])
    if current_coord is None:
        current_coord = _parse_coord((saved_map.get("start") or {}).get("coord"))

    odds = data.get("odds") or {}
    unknown_odds = {
        "monster": float(odds.get("unknown_map_point_monster_odds_value") or 0.0),
        "elite": float(odds.get("unknown_map_point_elite_odds_value") or 0.0),
        "shop": float(odds.get("unknown_map_point_shop_odds_value") or 0.0),
        "treasure": float(odds.get("unknown_map_point_treasure_odds_value") or 0.0),
    }

    return MapSnapshot(
        act_id=str(act.get("id") or ""),
        width=int(saved_map.get("width") or 0),
        height=int(saved_map.get("height") or 0),
        start_coord=_parse_coord((saved_map.get("start") or {}).get("coord")),
        current_coord=current_coord,
        boss_coord=_parse_coord(saved_map.get("boss")),
        start_children=[
            coord
            for coord in (_parse_coord(raw_child) for raw_child in saved_map.get("start_coords") or [])
            if coord is not None
        ],
        points=points,
        unknown_odds=unknown_odds,
    )


def find_save_file() -> Path | None:
    """현재 런 세이브 파일 찾기. modded 우선, Steam 유저 자동 탐지."""
    user_dir = _find_steam_user_dir()
    if user_dir is None:
        return None
    # modded 우선, 그 다음 vanilla. 여러 프로필 탐색.
    for subdir in ["modded/profile1", "profile1", "modded/profile2", "profile2"]:
        path = user_dir / subdir / "saves" / "current_run.save"
        if path.exists():
            return path
    return None


def parse_save(path: Path | None = None) -> RunState | None:
    """세이브 파일 파싱."""
    if path is None:
        path = find_save_file()
    if path is None or not path.exists():
        return None

    with open(path) as f:
        data = json.load(f)

    players = data.get("players") or []
    if not players:
        return None

    preferred_player_id = get_local_steam_user_id(path)
    player = _select_player(players, preferred_player_id)
    room_type, room_model_id, monster_ids = _extract_room_metadata(data)
    character_id = player.get("character_id") or player.get("character", "")
    state = RunState(
        player_id=_normalize_player_id(player.get("id") or preferred_player_id),
        build_id=data.get("build_id", ""),
        character=character_id.replace("CHARACTER.", ""),
        current_hp=int(player.get("current_hp") or 0),
        max_hp=int(player.get("max_hp") or 0),
        max_energy=int(player.get("max_energy") or 3),
        gold=int(player.get("gold") or 0),
        act=int(data.get("current_act_index") or 0),
        max_potion_slots=player.get("max_potion_slot_count", 0),
        seed=(data.get("rng") or {}).get("seed", ""),
        room_type=room_type,
        room_model_id=room_model_id,
        monster_ids=monster_ids,
        ascension=int(data.get("ascension") or 0),
        game_mode=data.get("game_mode", ""),
        modifiers=list(data.get("modifiers") or []),
        map_snapshot=_parse_map_snapshot(data),
    )

    for card in player["deck"]:
        enchantment = card.get("enchantment") or {}
        state.deck.append(CardInfo(
            id=card["id"],
            upgrades=card.get("current_upgrade_level", card.get("upgrades", 0)),
            floor_added=card.get("floor_added_to_deck"),
            enchantment_id=enchantment.get("id"),
            enchantment_amount=enchantment.get("amount", 0),
            props_key=json.dumps(card.get("props", {}), sort_keys=True, ensure_ascii=False),
        ))

    for relic in player["relics"]:
        state.relics.append(relic["id"])

    for potion in player.get("potions") or []:
        potion_id = potion.get("id")
        if potion_id:
            state.potions.append(potion_id)

    state.floor = len(data.get("visited_map_coords", []))

    return state


if __name__ == "__main__":
    state = parse_save()
    if state is None:
        print("No active run found")
    else:
        print(f"Character: {state.character}")
        print(f"Player ID: {state.player_id}")
        print(f"Build: {state.build_id or '(unknown)'}")
        print(f"HP: {state.current_hp}/{state.max_hp}")
        print(f"Gold: {state.gold} | Act: {state.act} | Floor: {state.floor} | A{state.ascension}")
        print(f"Seed: {state.seed}")
        print(f"Room: {state.room_type} ({state.room_model_id})")
        print(f"\nDeck ({len(state.deck)} cards):")
        for card in state.deck:
            print(f"  {card.display_id}")
        print(f"\nRelics ({len(state.relics)}):")
        for relic in state.relics:
            print(f"  {relic}")
        print(f"\nPotions ({len(state.potions)}/{state.max_potion_slots}):")
        for potion in state.potions:
            print(f"  {potion}")
