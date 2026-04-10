"""STS2 트래커 TUI 앱 - Textual 기반."""

import time
import threading
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, Container
from textual.widgets import Header, Footer, Static, Label, RichLog
from textual.reactive import reactive
from textual import work

from save_parser import parse_save, find_save_file, RunState
from card_db import load_card_db, fuzzy_match
from screen_capture import (
    find_game_window,
    capture_window,
    detect_card_reward_screen,
    extract_card_regions,
    ocr_card_names,
)
from tracker import score_card
from utils import clean_description


CSS = """
Screen {
    layout: grid;
    grid-size: 2 3;
    grid-rows: auto 1fr auto;
    grid-columns: 1fr 1fr;
}

#status-bar {
    column-span: 2;
    height: 3;
    background: $primary-darken-2;
    padding: 0 1;
    content-align: center middle;
}

#deck-panel {
    height: 100%;
    border: solid $primary;
    padding: 0 1;
    overflow-y: auto;
}

#info-panel {
    height: 100%;
    border: solid $secondary;
    padding: 0 1;
    overflow-y: auto;
}

#recommend-panel {
    column-span: 2;
    height: auto;
    max-height: 16;
    border: heavy $warning;
    padding: 0 1;
    overflow-y: auto;
}

.panel-title {
    text-style: bold;
    color: $text;
    padding: 0 0 1 0;
}

.card-pick {
    color: $success;
    text-style: bold;
}

.score-high {
    color: $success;
}

.score-mid {
    color: $warning;
}

.score-low {
    color: $text-muted;
}

#loading {
    column-span: 2;
    row-span: 3;
    content-align: center middle;
    text-style: bold;
    color: $warning;
}
"""


class StatusBar(Static):
    """상단 상태바 - 캐릭터/HP/골드/시드."""

    def render(self) -> str:
        app = self.app
        state = app.run_state
        if state is None:
            game = "게임 대기 중..." if app.window_id is None else "런 대기 중..."
            return f"STS2 Tracker │ {game}"
        hp_pct = state.current_hp / max(state.max_hp, 1)
        hp_color = "green" if hp_pct > 0.6 else "yellow" if hp_pct > 0.3 else "red"
        return (
            f"  {state.character} │ "
            f"HP: {state.current_hp}/{state.max_hp} │ "
            f"Gold: {state.gold} │ "
            f"Act {state.act + 1} Floor {state.floor} │ "
            f"Seed: {state.seed}"
        )


class DeckPanel(Static):
    """덱 패널."""

    def render(self) -> str:
        state = self.app.run_state
        if state is None:
            return "DECK\n─────\n(대기 중)"

        card_counts: dict[str, int] = {}
        for card in state.deck:
            name = card.display_id
            card_counts[name] = card_counts.get(name, 0) + 1

        lines = [f"DECK ({len(state.deck)})", "─" * 20]
        for name, count in sorted(card_counts.items()):
            suffix = f" x{count}" if count > 1 else ""
            lines.append(f"  {name}{suffix}")
        return "\n".join(lines)


class InfoPanel(Static):
    """유물 + 기타 정보 패널."""

    def render(self) -> str:
        state = self.app.run_state
        if state is None:
            return "INFO\n─────\n(대기 중)"

        lines = [f"RELICS ({len(state.relics)})", "─" * 20]
        for relic in state.relics:
            lines.append(f"  {relic.replace('RELIC.', '')}")

        lines.append("")
        ocr_status = self.app.ocr_status
        lines.append(f"OCR: {ocr_status}")
        lines.append(f"Window: {'Connected' if self.app.window_id else 'Not found'}")
        return "\n".join(lines)


class RecommendPanel(Static):
    """카드 추천 패널."""

    def render(self) -> str:
        recs = self.app.recommendations
        if not recs:
            return "CARD REWARD\n─────────────\n  (카드 보상 화면 대기 중...)"

        lines = ["CARD REWARD", "─" * 50]
        best_idx = -1
        best_score = -999.0
        for i, (card, match_pct, game_score) in enumerate(recs):
            if game_score > best_score:
                best_score = game_score
                best_idx = i

        for i, (card, match_pct, game_score) in enumerate(recs):
            star = "★" if i == best_idx else " "
            cost = card.get("cost", "?")
            rarity = card.get("rarity", "")
            name = card["name"]
            desc = clean_description(card.get("description", "")).replace("\n", " ")

            lines.append(
                f"  {star} [{cost}] {name}  ({rarity})  Score: {game_score}"
            )
            lines.append(f"      {desc[:70]}")
            lines.append(f"      Match: {match_pct:.0%} │ {card['id']}")
            lines.append("")

        if best_idx >= 0:
            best_card = recs[best_idx][0]
            lines.append(f"  >>> 추천: {best_card['name']} <<<")

        return "\n".join(lines)


class STS2TrackerApp(App):
    """STS2 트래커 메인 앱."""

    TITLE = "STS2 Tracker"
    CSS = CSS
    BINDINGS = [
        ("q", "quit", "종료"),
        ("r", "refresh", "새로고침"),
        ("s", "scan", "화면 스캔"),
    ]

    run_state: reactive[RunState | None] = reactive(None)
    window_id: reactive[int | None] = reactive(None)
    ocr_status: reactive[str] = reactive("초기화 중...")
    recommendations: reactive[list] = reactive(list)

    def __init__(self):
        super().__init__()
        self.cards_db = []
        self.ocr_reader = None
        self.last_mtime = 0.0
        self.last_card_screen = False
        self._tracking = True

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatusBar(id="status-bar")
        yield DeckPanel(id="deck-panel")
        yield InfoPanel(id="info-panel")
        yield RecommendPanel(id="recommend-panel")
        yield Footer()

    def on_mount(self) -> None:
        self.cards_db = load_card_db()
        self.init_ocr()
        self.track_loop()

    @work(thread=True)
    def init_ocr(self) -> None:
        """EasyOCR 초기화 (백그라운드 스레드)."""
        import easyocr

        self.ocr_status = "모델 로딩..."
        self.mutate_reactive(STS2TrackerApp.ocr_status)
        self.ocr_reader = easyocr.Reader(["ko", "en"], gpu=False, verbose=False)
        self.ocr_status = "준비 완료"
        self.mutate_reactive(STS2TrackerApp.ocr_status)

    @work(thread=True)
    def track_loop(self) -> None:
        """메인 트래킹 루프 (백그라운드 스레드)."""
        save_path = find_save_file()

        while self._tracking:
            try:
                # 1) 게임 창 찾기
                if self.window_id is None:
                    wid = find_game_window()
                    if wid:
                        self.window_id = wid
                        self.mutate_reactive(STS2TrackerApp.window_id)

                # 2) 세이브 파일 감시
                if save_path is None:
                    save_path = find_save_file()
                if save_path and save_path.exists():
                    mtime = save_path.stat().st_mtime
                    if mtime != self.last_mtime:
                        self.last_mtime = mtime
                        state = parse_save(save_path)
                        if state:
                            self.run_state = state
                            self.mutate_reactive(STS2TrackerApp.run_state)

                # 3) 화면 캡처 + 카드 보상 감지
                if self.window_id and self.ocr_reader:
                    img = capture_window(self.window_id)
                    if img is not None:
                        is_card_screen = detect_card_reward_screen(img)
                        if is_card_screen and not self.last_card_screen:
                            self._do_card_scan(img)
                        elif not is_card_screen and self.last_card_screen:
                            self.recommendations = []
                            self.mutate_reactive(STS2TrackerApp.recommendations)
                        self.last_card_screen = is_card_screen

                time.sleep(1.5)
            except Exception as e:
                self.ocr_status = f"Error: {e}"
                self.mutate_reactive(STS2TrackerApp.ocr_status)
                time.sleep(3)

    def _do_card_scan(self, img) -> None:
        """카드 보상 스캔 + 추천."""
        self.ocr_status = "카드 인식 중..."
        self.mutate_reactive(STS2TrackerApp.ocr_status)

        regions = extract_card_regions(img)
        detected = ocr_card_names(regions, self.ocr_reader)

        state = self.run_state
        if state is None:
            return

        recs = []
        for det in detected:
            if not det.ocr_text:
                continue
            matches = fuzzy_match(det.ocr_text, self.cards_db, threshold=0.3)
            if matches:
                card, match_score = matches[0]
                game_score = score_card(card, state)
                recs.append((card, match_score, game_score))

        self.recommendations = recs
        self.mutate_reactive(STS2TrackerApp.recommendations)
        self.ocr_status = "준비 완료"
        self.mutate_reactive(STS2TrackerApp.ocr_status)

    def action_refresh(self) -> None:
        """수동 새로고침."""
        save_path = find_save_file()
        if save_path:
            state = parse_save(save_path)
            if state:
                self.run_state = state
                self.mutate_reactive(STS2TrackerApp.run_state)
        wid = find_game_window()
        if wid:
            self.window_id = wid
            self.mutate_reactive(STS2TrackerApp.window_id)

    def action_scan(self) -> None:
        """수동 화면 스캔."""
        if self.window_id and self.ocr_reader:
            self.do_manual_scan()

    @work(thread=True)
    def do_manual_scan(self) -> None:
        img = capture_window(self.window_id)
        if img is not None:
            self._do_card_scan(img)

    def on_unmount(self) -> None:
        self._tracking = False


def main():
    app = STS2TrackerApp()
    app.run()


if __name__ == "__main__":
    main()
