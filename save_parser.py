"""STS2 세이브 파일 파서 - current_run.save에서 게임 상태 추출."""

import json
from dataclasses import dataclass, field
from pathlib import Path

STS2_DATA_DIR = Path.home() / "Library/Application Support/SlayTheSpire2"


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

    @property
    def display_id(self) -> str:
        base = self.id.replace("CARD.", "")
        return f"{base}+{self.upgrades}" if self.upgrades > 0 else base


@dataclass
class RunState:
    character: str = ""
    current_hp: int = 0
    max_hp: int = 0
    gold: int = 0
    act: int = 0
    deck: list[CardInfo] = field(default_factory=list)
    relics: list[str] = field(default_factory=list)
    seed: str = ""
    potions: list[str] = field(default_factory=list)
    room_type: str = ""
    floor: int = 0


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

    player = data["players"][0]
    state = RunState(
        character=player["character_id"].replace("CHARACTER.", ""),
        current_hp=player["current_hp"],
        max_hp=player["max_hp"],
        gold=player["gold"],
        act=data["current_act_index"],
        seed=data["rng"]["seed"],
    )

    for card in player["deck"]:
        state.deck.append(CardInfo(
            id=card["id"],
            upgrades=card.get("upgrades", 0),
        ))

    for relic in player["relics"]:
        state.relics.append(relic["id"])

    pre_room = data.get("pre_finished_room", {})
    state.room_type = pre_room.get("room_type", "")
    state.floor = len(data.get("visited_map_coords", []))

    return state


if __name__ == "__main__":
    state = parse_save()
    if state is None:
        print("No active run found")
    else:
        print(f"Character: {state.character}")
        print(f"HP: {state.current_hp}/{state.max_hp}")
        print(f"Gold: {state.gold} | Act: {state.act} | Floor: {state.floor}")
        print(f"Seed: {state.seed}")
        print(f"Room: {state.room_type}")
        print(f"\nDeck ({len(state.deck)} cards):")
        for card in state.deck:
            print(f"  {card.display_id}")
        print(f"\nRelics ({len(state.relics)}):")
        for relic in state.relics:
            print(f"  {relic}")
