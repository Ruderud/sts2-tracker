"""런 히스토리 build_id 메타데이터 유틸."""

from __future__ import annotations

import json
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path


_BUILD_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def parse_build_id(build_id: str) -> tuple[int, int, int]:
    match = _BUILD_RE.search(build_id or "")
    if not match:
        return (-1, -1, -1)
    return tuple(int(group) for group in match.groups())


def build_sort_key(build_id: str) -> tuple[int, int, int, str]:
    major, minor, patch = parse_build_id(build_id)
    return (major, minor, patch, build_id or "")


@lru_cache(maxsize=None)
def load_history_file_meta(path_str: str) -> dict:
    path = Path(path_str)
    with open(path) as f:
        data = json.load(f)
    return {
        "build_id": data.get("build_id", ""),
        "schema_version": data.get("schema_version"),
        "start_time": data.get("start_time"),
    }


def load_history_build_id(path: Path) -> str:
    return str(load_history_file_meta(str(path)).get("build_id") or "")


def ordered_build_ids(paths: list[Path]) -> list[str]:
    builds = {load_history_build_id(path) for path in paths if load_history_build_id(path)}
    return sorted(builds, key=build_sort_key)


def latest_build_id(paths: list[Path]) -> str:
    builds = ordered_build_ids(paths)
    return builds[-1] if builds else ""


def split_latest_and_legacy_paths(paths: list[Path]) -> tuple[str, list[Path], list[Path]]:
    latest = latest_build_id(paths)
    latest_paths = [path for path in paths if load_history_build_id(path) == latest]
    legacy_paths = [path for path in paths if load_history_build_id(path) != latest]
    return latest, latest_paths, legacy_paths


def summarize_builds(paths: list[Path]) -> dict:
    counts = Counter(load_history_build_id(path) or "UNKNOWN" for path in paths)
    ordered = sorted(counts.items(), key=lambda item: build_sort_key(item[0]))
    return {
        "latest_build": latest_build_id(paths),
        "build_counts": {build_id: count for build_id, count in ordered},
    }


def build_step_distance(build_id: str, latest_build: str, build_order: list[str]) -> int:
    if not build_order or build_id not in build_order or latest_build not in build_order:
        return max(len(build_order) - 1, 0)
    return max(build_order.index(latest_build) - build_order.index(build_id), 0)


def build_decay_weight(build_id: str, latest_build: str, build_order: list[str], decay: float) -> float:
    distance = build_step_distance(build_id, latest_build, build_order)
    return decay ** distance
