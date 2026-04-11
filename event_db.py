"""STS2 이벤트 데이터베이스 - Spire Codex API에서 가져온 한국어 이벤트 데이터."""

from __future__ import annotations

import json
import os
import re
from difflib import SequenceMatcher
from pathlib import Path

from save_parser import RunState
from utils import clean_game_text, normalize_ocr_text


DB_PATH = Path(__file__).parent / "data" / "events_kor.json"


def download_event_db() -> None:
    """Spire Codex API에서 한국어 이벤트 데이터 다운로드."""
    import urllib.request

    url = "https://spire-codex.com/api/events?lang=kor"
    os.makedirs(DB_PATH.parent, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "STS2Tracker/1.0"})
    with urllib.request.urlopen(req) as resp:
        with open(DB_PATH, "wb") as f:
            f.write(resp.read())
    print(f"Downloaded {DB_PATH}")


def load_event_db() -> list[dict]:
    """이벤트 DB 로드. 없으면 다운로드."""
    if not DB_PATH.exists():
        download_event_db()
    with open(DB_PATH) as f:
        return json.load(f)


def _partial_similarity(query: str, target: str) -> float:
    if not query or not target:
        return 0.0
    if query in target or target in query:
        shorter = min(len(query), len(target))
        longer = max(len(query), len(target))
        return min(0.78 + shorter / max(longer, 1) * 0.22, 1.0)

    if len(query) > len(target):
        query, target = target, query

    window = len(query)
    if window <= 0:
        return 0.0

    best = SequenceMatcher(None, query, target[:window]).ratio()
    step = max(1, window // 4)
    for start in range(step, max(1, len(target) - window + 1), step):
        best = max(best, SequenceMatcher(None, query, target[start:start + window]).ratio())
    return best


def _build_page_search_text(event: dict, page: dict, options: list[dict]) -> str:
    parts = [event.get("name", ""), clean_game_text(event.get("description", ""))]
    page_title = clean_game_text(page.get("title", "") or "")
    page_text = clean_game_text(page.get("text", "") or "")
    if page_title:
        parts.append(page_title)
    if page_text:
        parts.append(page_text)
    for option in options:
        parts.append(option["title"])
        if option["description"]:
            parts.append(option["description"])
    return "\n".join(part for part in parts if part)


def flatten_event_pages(events: list[dict]) -> list[dict]:
    """이벤트 DB를 이벤트 페이지 단위로 펼친다."""
    pages: list[dict] = []
    for event in events:
        for page in event.get("pages") or []:
            options = []
            for option in page.get("options") or []:
                title = clean_game_text(option.get("title", "") or "")
                description = clean_game_text(option.get("description", "") or "")
                options.append(
                    {
                        "id": option.get("id", ""),
                        "title": title,
                        "description": description,
                        "title_norm": normalize_ocr_text(title),
                        "description_norm": normalize_ocr_text(description),
                    }
                )

            if not options:
                continue

            search_text = _build_page_search_text(event, page, options)
            pages.append(
                {
                    "event_id": event["id"],
                    "event_name": clean_game_text(event.get("name", "") or event["id"]),
                    "event_description": clean_game_text(event.get("description", "") or ""),
                    "page_id": page.get("id", ""),
                    "page_title": clean_game_text(page.get("title", "") or ""),
                    "page_text": clean_game_text(page.get("text", "") or ""),
                    "options": options,
                    "search_text": search_text,
                    "search_norm": normalize_ocr_text(search_text),
                    "event_name_norm": normalize_ocr_text(event.get("name", "") or event["id"]),
                }
            )
    return pages


def build_event_page_lookup(event_pages: list[dict]) -> dict[tuple[str, str], dict]:
    """(event_id, page_id) -> page 매핑."""
    return {(page["event_id"], page["page_id"]): page for page in event_pages}


def _score_option_visibility(query_norm: str, option: dict) -> float:
    title_score = _partial_similarity(query_norm, option["title_norm"])
    desc_score = _partial_similarity(query_norm, option["description_norm"])
    return max(title_score, desc_score * 0.92)


def match_event_page(
    query: str,
    event_pages: list[dict],
    *,
    threshold: float = 0.42,
) -> list[tuple[dict, float]]:
    """OCR 텍스트를 이벤트 페이지와 매칭."""
    query_norm = normalize_ocr_text(query)
    if not query_norm:
        return []

    results: list[tuple[dict, float]] = []
    for page in event_pages:
        event_score = _partial_similarity(query_norm, page["event_name_norm"])
        option_scores = sorted(
            (_score_option_visibility(query_norm, option) for option in page["options"]),
            reverse=True,
        )
        option_component = sum(option_scores[:2]) / max(1, min(2, len(option_scores)))
        page_score = _partial_similarity(query_norm, page["search_norm"])
        visible_count = sum(1 for score in option_scores if score >= 0.55)
        coverage = min(visible_count / max(1, min(2, len(page["options"]))), 1.0)

        score = (
            event_score * 0.28
            + option_component * 0.47
            + page_score * 0.17
            + coverage * 0.08
        )
        if score >= threshold:
            results.append((page, score))

    results.sort(key=lambda item: item[1], reverse=True)
    return results


def visible_options_for_query(page: dict, query: str | None) -> list[dict]:
    """현재 OCR에 실제로 보이는 선택지만 우선 추린다."""
    if not query:
        return list(page["options"])

    query_norm = normalize_ocr_text(query)
    if not query_norm:
        return list(page["options"])

    scored = [
        (option, _score_option_visibility(query_norm, option))
        for option in page["options"]
    ]
    visible = [option for option, score in scored if score >= 0.48]
    if visible:
        return visible

    non_locked = [option for option in page["options"] if not option["id"].endswith("_LOCKED")]
    return non_locked or list(page["options"])


def filter_options_for_state(
    page: dict,
    state: RunState,
    *,
    option_ids: list[str] | None = None,
) -> list[dict]:
    """현재 상태에서 실제 선택 가능한 이벤트 옵션만 추린다."""
    options = list(page["options"])
    if option_ids is not None:
        wanted = set(option_ids)
        options = [option for option in options if option["id"] in wanted]

    available = []
    has_real_option = False
    for option in options:
        title = clean_game_text(option.get("title", "") or "")
        desc = clean_game_text(option.get("description", "") or "")
        text = f"{title}\n{desc}"
        option_id = option.get("id", "")

        if option_id.endswith("_LOCKED") or "잠김" in title:
            continue

        numbers = [int(value) for value in re.findall(r"(\d+)", text)]
        if ("골드" in text and ("지불" in text or "준다" in text)) and numbers:
            if state.gold < max(numbers):
                continue

        if "Potion" in title or "포션" in text:
            if "준다" in title and not state.potions:
                continue

        if option_id == "NO_OPTIONS":
            continue

        available.append(option)
        has_real_option = True

    if has_real_option:
        return available

    fallback = [option for option in options if option.get("id") == "NO_OPTIONS"]
    return fallback or available or options


if __name__ == "__main__":
    events = load_event_db()
    pages = flatten_event_pages(events)
    print(f"Loaded {len(events)} events / {len(pages)} pages")

    test_queries = [
        "혼돈의 향기 향기에 몸을 맡긴다 정신을 붙잡는다",
        "땜질 시간 무기 보호대 도구",
        "장로 랜위드 Potion을 준다 골드를 100 준다 Relic을 준다",
    ]
    for query in test_queries:
        matches = match_event_page(query, pages)
        print(f"\n{query!r}")
        for page, score in matches[:3]:
            print(
                f"  [{score:.2f}] {page['event_id']}.{page['page_id']} "
                f"{page['event_name']} ({', '.join(opt['title'] for opt in page['options'][:3])})"
            )
