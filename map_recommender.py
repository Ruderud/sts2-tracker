"""STS2 맵 경로 추천 엔진."""

from __future__ import annotations

from functools import lru_cache
from itertools import combinations
import numpy as np

from recommender import analyze_deck, assess_needs
from save_parser import MapCoord, MapPointInfo, MapSnapshot, RunState


ROOM_LABELS = {
    "monster": "전투",
    "elite": "엘리트",
    "unknown": "미지수",
    "rest_site": "휴식",
    "shop": "상점",
    "treasure": "보물",
    "boss": "보스",
    "ancient": "시작",
}

COL_STEP = 0.0752
ROW_STEP = 0.068
START_ROW_STEP = 0.145


def _coord_key(coord: MapCoord) -> tuple[int, int]:
    return coord.col, coord.row


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _room_score(
    room_type: str,
    *,
    state: RunState,
    needs: dict,
    basics_ratio: float,
    hp_pct: float,
    gold: int,
    potion_count: int,
    unknown_odds: dict[str, float],
    step_index: int,
) -> float:
    act1 = state.act == 0

    if room_type == "monster":
        early_bonus = 0.45 if act1 and step_index <= 3 else 0.15
        return 1.0 + early_bonus + needs["damage"] * 0.18 + needs["scaling"] * 0.06

    if room_type == "elite":
        reward = 2.35 + (0.25 if act1 else 0.05)
        danger = max(0.0, 0.8 - hp_pct) * 2.0
        if step_index <= 2:
            danger += 0.45
        if potion_count > 0:
            reward += 0.3
        return reward - danger

    if room_type == "rest_site":
        heal_value = 0.4 + max(0.75 - hp_pct, 0.0) * 2.4
        setup_value = 0.2 + basics_ratio * 0.45
        return heal_value + setup_value

    if room_type == "shop":
        cash_value = 0.25
        if gold >= 150:
            cash_value = 1.9
        elif gold >= 100:
            cash_value = 1.35
        elif gold >= 75:
            cash_value = 0.9
        return cash_value + basics_ratio * 0.35

    if room_type == "treasure":
        return 1.55

    if room_type == "unknown":
        event_share = max(0.0, 1.0 - sum(unknown_odds.values()))
        expected = (
            unknown_odds.get("monster", 0.0)
            * _room_score(
                "monster",
                state=state,
                needs=needs,
                basics_ratio=basics_ratio,
                hp_pct=hp_pct,
                gold=gold,
                potion_count=potion_count,
                unknown_odds=unknown_odds,
                step_index=step_index,
            )
            + unknown_odds.get("elite", 0.0)
            * _room_score(
                "elite",
                state=state,
                needs=needs,
                basics_ratio=basics_ratio,
                hp_pct=hp_pct,
                gold=gold,
                potion_count=potion_count,
                unknown_odds=unknown_odds,
                step_index=step_index,
            )
            + unknown_odds.get("shop", 0.0)
            * _room_score(
                "shop",
                state=state,
                needs=needs,
                basics_ratio=basics_ratio,
                hp_pct=hp_pct,
                gold=gold,
                potion_count=potion_count,
                unknown_odds=unknown_odds,
                step_index=step_index,
            )
            + unknown_odds.get("treasure", 0.0)
            * _room_score(
                "treasure",
                state=state,
                needs=needs,
                basics_ratio=basics_ratio,
                hp_pct=hp_pct,
                gold=gold,
                potion_count=potion_count,
                unknown_odds=unknown_odds,
                step_index=step_index,
            )
            + event_share * (1.0 + basics_ratio * 0.1)
        )
        return expected

    if room_type == "boss":
        return 0.0

    return 0.6


def _count_rooms(path: list[MapPointInfo]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for point in path:
        counts[point.type] = counts.get(point.type, 0) + 1
    return counts


def _path_summary(counts: dict[str, int]) -> str:
    ordered_types = ["monster", "unknown", "elite", "rest_site", "shop", "treasure", "boss"]
    parts = []
    for room_type in ordered_types:
        count = counts.get(room_type, 0)
        if count:
            parts.append(f"{ROOM_LABELS.get(room_type, room_type)} {count}")
    return " · ".join(parts)


def _route_reasons(
    next_point: MapPointInfo,
    path: list[MapPointInfo],
    counts: dict[str, int],
    *,
    state: RunState,
    hp_pct: float,
    basics_ratio: float,
) -> list[str]:
    reasons: list[str] = []

    if next_point.type == "monster" and state.act == 0:
        reasons.append("초반 카드보상 수급")
    elif next_point.type == "shop" and state.gold >= 100:
        reasons.append("초반 상점 가치 높음")
    elif next_point.type == "elite" and hp_pct >= 0.72:
        reasons.append("체력 넉넉해 엘리트 가능")
    elif next_point.type == "rest_site" and hp_pct < 0.65:
        reasons.append("안정적으로 체력 관리")
    elif next_point.type == "unknown":
        reasons.append("미지수로 이벤트 가치 탐색")

    if counts.get("elite", 0) >= 1 and hp_pct >= 0.72:
        reasons.append("유물 노릴 수 있음")
    elif counts.get("elite", 0) >= 1 and hp_pct < 0.55:
        reasons.append("엘리트가 있어 위험")

    if counts.get("shop", 0) >= 1 and state.gold >= 75:
        reasons.append("보유 골드 활용 가능")

    if counts.get("rest_site", 0) >= 1 and (hp_pct < 0.7 or basics_ratio > 0.45):
        reasons.append("휴식/강화 타이밍 확보")

    if counts.get("monster", 0) >= 3 and state.act == 0:
        reasons.append("초반 성장 경로")

    return reasons[:4] or ["무난한 경로"]


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


def _estimate_target_screen(
    snapshot: MapSnapshot,
    current_coord: MapCoord,
    target_coord: MapCoord,
    next_coords: list[MapCoord],
    node_rows: list[dict] | None,
    anchor_screen: dict | None,
) -> dict | None:
    if not node_rows:
        return None

    reachable_row_cols = sorted(
        coord.col for coord in next_coords if coord.row == target_coord.row
    )
    if not reachable_row_cols or target_coord.col not in reachable_row_cols:
        return None

    candidate_rows = list(node_rows)
    expected_x = None
    expected_y = None
    row_tolerance = 0.075
    row_delta = max(1, target_coord.row - current_coord.row)
    immediate_next = row_delta == 1
    if anchor_screen and isinstance(anchor_screen.get("x"), (int, float)) and isinstance(anchor_screen.get("y"), (int, float)):
        anchor_x = float(anchor_screen["x"])
        anchor_y = float(anchor_screen["y"])
        col_delta = target_coord.col - current_coord.col
        is_start_step = (
            snapshot.start_coord is not None
            and current_coord == snapshot.start_coord
            and row_delta == 1
        )
        expected_x = anchor_x + col_delta * COL_STEP
        above_rows = [row for row in candidate_rows if float(row.get("y", 0.0)) < anchor_y - 0.01]
        if not above_rows:
            return None

        if immediate_next:
            # 다음으로 갈 수 있는 노드는 항상 현재 위치 바로 위 줄에 있으므로
            # 첫 단계에서는 고정 row_step보다 "앵커에 가장 가까운 줄"이 더 안정적이다.
            above_rows.sort(key=lambda row: float(row.get("y", 0.0)), reverse=True)
            nearest_above = above_rows[0]
            nearest_gap = anchor_y - float(nearest_above.get("y", 0.0))
            if nearest_gap < 0.015 or nearest_gap > 0.12:
                return None
            candidate_rows = [nearest_above]
            expected_y = float(nearest_above.get("y", 0.0))
            row_tolerance = 0.03
        else:
            expected_y = anchor_y - (START_ROW_STEP if is_start_step else ROW_STEP * row_delta)
            row_tolerance = 0.085 if is_start_step else 0.06
            candidate_rows = above_rows
            candidate_rows.sort(key=lambda row: abs(float(row.get("y", 0.0)) - expected_y))
            close_rows = [
                row
                for row in candidate_rows
                if abs(float(row.get("y", 0.0)) - expected_y) <= row_tolerance
            ]
            if close_rows:
                candidate_rows = close_rows[:2]
            else:
                nearest_row = candidate_rows[:1]
                if not nearest_row:
                    return None
                if abs(float(nearest_row[0].get("y", 0.0)) - expected_y) > row_tolerance * 1.35:
                    return None
                candidate_rows = nearest_row

    best: tuple[float, dict] | None = None
    for row in candidate_rows:
        nodes = row.get("nodes") or []
        if len(nodes) < len(reachable_row_cols):
            # 다음 줄이 덜 검출된 프레임에 다른 줄로 튀는 것보다,
            # 이번 프레임은 마커를 보수적으로 숨기는 편이 안전하다.
            continue

        if immediate_next and len(nodes) == len(reachable_row_cols):
            subset_nodes = sorted(nodes, key=lambda node: float(node["x"]))
            target_index = reachable_row_cols.index(target_coord.col)
            matched_target = subset_nodes[target_index]
            score = 0.0
            if expected_y is not None:
                score += abs(float(row.get("y", 0.0)) - expected_y) * 1.2
            if expected_x is not None:
                score += abs(float(matched_target["x"]) - expected_x) * 0.9
            candidate = {
                "x": float(matched_target["x"]),
                "y": float(matched_target["y"]),
                "confidence": float(max(0.0, min(0.99, 0.985 - score * 10))),
                "fit_error": 0.0,
            }
            if best is None or score < best[0]:
                best = (score, candidate)
            continue

        index_sets = [tuple(range(len(nodes)))]
        if len(nodes) > len(reachable_row_cols):
            index_sets = list(combinations(range(len(nodes)), len(reachable_row_cols)))

        cols = np.array(reachable_row_cols, dtype=float)
        for subset_indexes in index_sets:
            subset_nodes = [nodes[idx] for idx in subset_indexes]
            xs = np.array([float(node["x"]) for node in subset_nodes], dtype=float)
            matrix = np.vstack([cols, np.ones(len(cols))]).T
            slope, intercept = np.linalg.lstsq(matrix, xs, rcond=None)[0]
            predicted = slope * cols + intercept
            mae = float(np.abs(predicted - xs).mean())
            score = mae
            target_index = reachable_row_cols.index(target_coord.col)
            matched_target = subset_nodes[target_index]

            if expected_y is not None:
                score += abs(float(row.get("y", 0.0)) - expected_y) * 2.2

            if expected_x is not None:
                expected_positions = np.array(
                    [expected_x + (col - target_coord.col) * COL_STEP for col in reachable_row_cols],
                    dtype=float,
                )
                score += float(np.abs(xs - expected_positions).mean()) * 0.75
                score += abs(float(matched_target["x"]) - expected_x) * 1.25

            candidate = {
                "x": float(matched_target["x"]),
                "y": float(matched_target["y"]),
                "confidence": float(max(0.0, min(0.99, 0.98 - score * 14))),
                "fit_error": round(mae, 5),
            }
            if best is None or score < best[0]:
                best = (score, candidate)

    if best is None:
        return None

    score, candidate = best
    if expected_y is not None and abs(candidate["y"] - expected_y) > row_tolerance:
        return None
    if expected_x is not None and abs(candidate["x"] - expected_x) > 0.12:
        return None
    if score > 0.42:
        return None
    return candidate


def recommend_map_route(
    state: RunState,
    cards_db: list[dict],
    *,
    max_routes: int = 3,
    anchor_screen: dict | None = None,
    node_rows: list[dict] | None = None,
) -> dict | None:
    snapshot = state.map_snapshot
    if snapshot is None or snapshot.current_coord is None:
        return None

    lookup = _lookup_points(snapshot)
    next_coords = _children_for_coord(snapshot, lookup, snapshot.current_coord)
    if not next_coords:
        return None

    card_index = {card["id"]: card for card in cards_db}
    analysis = analyze_deck(state, cards_db, card_index=card_index)
    needs = assess_needs(analysis, state)
    hp_pct = state.current_hp / max(state.max_hp, 1)
    basics_ratio = analysis["basics"] / max(analysis["total"], 1)

    @lru_cache(maxsize=None)
    def best_path_from(coord_key: tuple[int, int], step_index: int) -> tuple[float, tuple[tuple[int, int], ...]]:
        point = lookup.get(coord_key)
        if point is None:
            return 0.0, ()

        room_value = _room_score(
            point.type,
            state=state,
            needs=needs,
            basics_ratio=basics_ratio,
            hp_pct=hp_pct,
            gold=state.gold,
            potion_count=len(state.potions),
            unknown_odds=snapshot.unknown_odds,
            step_index=step_index,
        )

        if not point.children:
            return room_value, (coord_key,)

        best_score = None
        best_path: tuple[tuple[int, int], ...] = ()
        for child in point.children:
            child_score, child_path = best_path_from(_coord_key(child), step_index + 1)
            score = room_value + child_score * 0.93
            if best_score is None or score > best_score:
                best_score = score
                best_path = (coord_key,) + child_path

        if best_score is None:
            return room_value, (coord_key,)
        return best_score, best_path

    routes = []
    for next_coord in next_coords:
        next_key = _coord_key(next_coord)
        next_point = lookup.get(next_key)
        if next_point is None:
            continue
        score, coord_path = best_path_from(next_key, 1)
        path_points = [lookup[key] for key in coord_path if key in lookup]
        counts = _count_rooms(path_points)
        routes.append(
            {
                "next_coord": next_coord.to_dict(),
                "next_type": next_point.type,
                "next_label": ROOM_LABELS.get(next_point.type, next_point.type),
                "score": round(score, 2),
                "summary": _path_summary(counts),
                "reasons": _route_reasons(
                    next_point,
                    path_points,
                    counts,
                    state=state,
                    hp_pct=hp_pct,
                    basics_ratio=basics_ratio,
                ),
                "path_types": [ROOM_LABELS.get(point.type, point.type) for point in path_points[:6]],
                "counts": counts,
            }
        )

    routes.sort(key=lambda item: item["score"], reverse=True)
    if not routes:
        return None

    target_screen = _estimate_target_screen(
        snapshot,
        snapshot.current_coord,
        MapCoord(**routes[0]["next_coord"]),
        next_coords,
        node_rows,
        anchor_screen,
    )

    return {
        "act_id": snapshot.act_id,
        "current_coord": snapshot.current_coord.to_dict(),
        "anchor_screen": anchor_screen,
        "target_screen": target_screen,
        "geometry": {
            "col_step": COL_STEP,
            "row_step": ROW_STEP,
            "start_row_step": START_ROW_STEP,
        },
        "best_idx": 0,
        "routes": routes[:max_routes],
    }
