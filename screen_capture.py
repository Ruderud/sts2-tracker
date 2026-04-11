"""STS2 화면 캡처 및 카드/선택지 인식 - Quartz + EasyOCR."""

import cv2
import numpy as np
from dataclasses import dataclass
import re

import Quartz
from AppKit import NSBitmapImageRep

from utils import clean_game_text


GAME_TITLE = "Slay the Spire 2"


@dataclass
class DetectedCard:
    ocr_text: str
    confidence: float
    position: int  # 0-based index (left to right)


@dataclass
class DetectedChoice:
    title: str
    description: str
    confidence: float
    position: int
    screen_anchor: dict | None = None


@dataclass
class OCRTextLine:
    text: str
    confidence: float
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2


_COMBAT_NOISE_RE = re.compile(r"^[0-9xX+*/.,:()\-\s]+$")


def find_game_window() -> int | None:
    """게임 창 Window ID 찾기."""
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID
    )
    for w in windows:
        owner = w.get("kCGWindowOwnerName", "")
        title = w.get("kCGWindowName", "")
        if GAME_TITLE in owner and title == GAME_TITLE:
            return w.get("kCGWindowNumber")
    return None


def capture_window(window_id: int) -> np.ndarray | None:
    """Window ID로 스크린샷 캡처, numpy array 반환."""
    image_ref = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,
        Quartz.kCGWindowListOptionIncludingWindow,
        window_id,
        Quartz.kCGWindowImageDefault,
    )
    if image_ref is None:
        return None

    bitmap = NSBitmapImageRep.alloc().initWithCGImage_(image_ref)
    png_data = bitmap.representationUsingType_properties_(4, None)  # NSPNGFileType = 4

    arr = np.frombuffer(bytes(png_data), dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img


def detect_card_reward_screen(img: np.ndarray) -> bool:
    """카드 보상 화면인지 감지 (배너 영역 색상 패턴).

    해상도 변화에 대응하기 위해 y=22-36% 범위를 슬라이딩 윈도우로 탐색.
    """
    h, w = img.shape[:2]
    # 배너 "카드를 선택하세요"는 해상도에 따라 y 위치가 달라짐
    # y=22-36% 범위에서 4% 높이 윈도우를 슬라이딩하며 탐색
    x1, x2 = int(w * 0.3), int(w * 0.7)
    for y_pct in (0.27, 0.29, 0.25, 0.31, 0.23, 0.33):
        y1 = int(h * y_pct)
        y2 = int(h * (y_pct + 0.04))
        banner = img[y1:y2, x1:x2]
        hsv = cv2.cvtColor(banner, cv2.COLOR_BGR2HSV)
        # 갈색/베이지 배너 감지 (H=5-40, S=30+, V=80+)
        mask = cv2.inRange(hsv, (5, 30, 80), (40, 255, 255))
        ratio = np.count_nonzero(mask) / mask.size
        if ratio > 0.15:
            return True
    return False


def detect_combat_screen(img: np.ndarray) -> bool:
    """전투 화면인지 감지 (End Turn 버튼 영역 + 에너지 오브 영역).

    End Turn 버튼: 우하단 (x=79-96%, y=80-97%) - 짙은 파란 버튼 + 텍스트
    에너지 오브: 좌하단 (x=2-14%, y=76-97%) - 주황색 에너지 문양
    둘 중 하나라도 감지되면 전투 화면으로 판단.
    """
    h, w = img.shape[:2]

    # End Turn 버튼 영역 감지 (우하단)
    end_turn = img[int(h * 0.80):int(h * 0.97), int(w * 0.79):int(w * 0.96)]
    hsv_btn = cv2.cvtColor(end_turn, cv2.COLOR_BGR2HSV)
    # 실제 게임 버튼은 짙은 파란색 본체 + 밝은 글자/테두리 조합이다.
    mask_btn_blue = cv2.inRange(hsv_btn, (85, 40, 40), (125, 255, 220))
    mask_btn_bright = cv2.inRange(hsv_btn, (0, 0, 170), (180, 80, 255))
    btn_blue_ratio = np.count_nonzero(mask_btn_blue) / max(mask_btn_blue.size, 1)
    btn_bright_ratio = np.count_nonzero(mask_btn_bright) / max(mask_btn_bright.size, 1)

    # 에너지 오브 영역 감지 (좌하단)
    energy_orb = img[int(h * 0.76):int(h * 0.97), int(w * 0.02):int(w * 0.14)]
    hsv_orb = cv2.cvtColor(energy_orb, cv2.COLOR_BGR2HSV)
    # 전투 에너지 UI의 주황색 문양/숫자 영역.
    mask_orb_orange = cv2.inRange(hsv_orb, (5, 120, 120), (30, 255, 255))
    mask_orb_bright = cv2.inRange(hsv_orb, (0, 0, 170), (180, 80, 255))
    orb_orange_ratio = np.count_nonzero(mask_orb_orange) / max(mask_orb_orange.size, 1)
    orb_bright_ratio = np.count_nonzero(mask_orb_bright) / max(mask_orb_bright.size, 1)

    button_detected = btn_blue_ratio > 0.032 and btn_bright_ratio > 0.0006
    orb_detected = orb_orange_ratio > 0.018 or (orb_orange_ratio > 0.01 and orb_bright_ratio > 0.02)
    return button_detected or orb_detected


def _find_card_columns(img: np.ndarray) -> list[tuple[float, float]]:
    """밝기 프로파일로 카드 x 범위를 동적 탐지."""
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # y=35-70% 영역의 열 평균 밝기
    strip = gray[int(h * 0.35):int(h * 0.70), :]
    col_means = strip.mean(axis=0)

    # 이동 평균으로 스무딩
    kernel = np.ones(15) / 15
    smooth = np.convolve(col_means, kernel, mode="same")

    bg_level = np.median(smooth)
    threshold = bg_level + 20
    bright = smooth > threshold

    # 밝은 구간 찾기
    diff = np.diff(bright.astype(int))
    starts = np.where(diff == 1)[0]
    ends = np.where(diff == -1)[0]
    if bright[0]:
        starts = np.insert(starts, 0, 0)
    if bright[-1]:
        ends = np.append(ends, len(bright) - 1)

    min_width = w * 0.05
    columns = []
    for s, e in zip(starts, ends):
        if e - s > min_width:
            columns.append((s / w, e / w))

    return columns


def _find_card_rows(img: np.ndarray) -> tuple[float, float]:
    """밝기 프로파일로 카드 y 범위를 동적 탐지."""
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # x=25-75% 영역의 행 평균 밝기
    strip = gray[:, int(w * 0.25):int(w * 0.75)]
    row_means = strip.mean(axis=1)

    kernel = np.ones(10) / 10
    smooth = np.convolve(row_means, kernel, mode="same")

    bg_level = np.percentile(smooth, 30)
    threshold = bg_level + 20
    bright = smooth > threshold

    diff = np.diff(bright.astype(int))
    starts = np.where(diff == 1)[0]
    ends = np.where(diff == -1)[0]
    if bright[0]:
        starts = np.insert(starts, 0, 0)
    if bright[-1]:
        ends = np.append(ends, len(bright) - 1)

    # 가장 높이가 큰 밝은 구간 = 카드 영역
    best = (0.34, 0.70)  # fallback
    max_height = 0
    for s, e in zip(starts, ends):
        height = e - s
        if height > max_height and s / h > 0.25:  # 상단 UI 제외
            max_height = height
            best = (s / h, e / h)

    return best


def extract_card_regions(img: np.ndarray) -> list[np.ndarray]:
    """카드 보상 화면에서 개별 카드 영역 추출 (동적 밝기 분석)."""
    h, w = img.shape[:2]

    columns = _find_card_columns(img)
    y1_pct, y2_pct = _find_card_rows(img)

    # 카드 3장이 감지되지 않으면 폴백
    if len(columns) != 3:
        columns = [(0.26, 0.39), (0.44, 0.56), (0.61, 0.73)]

    regions = []
    for x1_pct, x2_pct in columns:
        crop = img[int(h * y1_pct):int(h * y2_pct), int(w * x1_pct):int(w * x2_pct)]
        regions.append(crop)
    return regions


def extract_card_name_regions(img: np.ndarray) -> list[np.ndarray]:
    """카드 이름 배너 영역만 추출 (OCR 정확도 향상용, 동적 감지)."""
    h, w = img.shape[:2]
    columns = _find_card_columns(img)

    if len(columns) != 3:
        columns = [(0.26, 0.39), (0.44, 0.56), (0.61, 0.73)]

    # 이름 리본은 카드 본체 상단 직전 ~4% 높이 영역
    card_y1, _ = _find_card_rows(img)
    name_y_start = card_y1 - 0.04
    name_y_end = card_y1

    regions = []
    for x1_pct, x2_pct in columns:
        crop = img[int(h * name_y_start):int(h * name_y_end), int(w * x1_pct):int(w * x2_pct)]
        regions.append(crop)
    return regions


def ocr_card_names(card_regions: list[np.ndarray], reader) -> list[DetectedCard]:
    """EasyOCR로 카드 텍스트 인식. 모든 텍스트를 합쳐서 반환."""
    detected = []
    for i, region in enumerate(card_regions):
        results = reader.readtext(region)
        if results:
            # 모든 텍스트를 합침 (이름+설명 동시 매칭용)
            all_text = " ".join(r[1] for r in results)
            avg_conf = sum(r[2] for r in results) / len(results)
            detected.append(DetectedCard(
                ocr_text=all_text,
                confidence=avg_conf,
                position=i,
            ))
        else:
            detected.append(DetectedCard(ocr_text="", confidence=0, position=i))
    return detected


def extract_combat_hand_region(img: np.ndarray) -> np.ndarray:
    """전투 화면의 손패 영역 추출."""
    h, w = img.shape[:2]
    return img[int(h * 0.60):int(h * 0.96), int(w * 0.11):int(w * 0.89)]


def _normalize_combat_line(text: str) -> str:
    text = clean_game_text(text).strip()
    if not text:
        return ""
    replacements = {
        "피해틀": "피해를",
        "드로우": "뽑습니다",
        "습니더": "습니다",
        "밤어": "방어",
        "비용O": "비용 0",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text.strip()


def ocr_combat_hand_cards(img: np.ndarray, reader) -> list[DetectedCard]:
    """전투 손패 영역을 OCR해서 카드별 텍스트로 묶는다."""
    region = extract_combat_hand_region(img)
    if region.size == 0:
        return []

    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    boosted = cv2.convertScaleAbs(gray, alpha=1.35, beta=14)
    thresholded = cv2.adaptiveThreshold(
        boosted,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        11,
    )

    collected: dict[tuple[int, int, str], dict] = {}
    for candidate in (region, thresholded):
        results = reader.readtext(candidate)
        for box, text, conf in results:
            text = _normalize_combat_line(text)
            if not text or _COMBAT_NOISE_RE.match(text):
                continue
            xs = [pt[0] for pt in box]
            ys = [pt[1] for pt in box]
            x1, x2 = float(min(xs)), float(max(xs))
            y1, y2 = float(min(ys)), float(max(ys))
            width = x2 - x1
            height = y2 - y1
            if width < 16 or height < 8:
                continue
            cx = (x1 + x2) / 2
            cy = (y1 + y2) / 2
            key = (int(round(cx / 18)), int(round(cy / 14)), text[:18])
            existing = collected.get(key)
            line = {
                "text": text,
                "confidence": float(conf),
                "cx": cx,
                "cy": cy,
                "x1": x1,
                "y1": y1,
            }
            if existing is None or line["confidence"] > existing["confidence"]:
                collected[key] = line

    lines = sorted(collected.values(), key=lambda item: (item["cx"], item["y1"]))
    if not lines:
        return []

    cluster_threshold = max(region.shape[1] * 0.075, 46)
    clusters: list[list[dict]] = []
    for line in lines:
        if not clusters:
            clusters.append([line])
            continue
        last_cluster = clusters[-1]
        center = sum(item["cx"] for item in last_cluster) / len(last_cluster)
        if abs(line["cx"] - center) > cluster_threshold:
            clusters.append([line])
        else:
            last_cluster.append(line)

    detected: list[DetectedCard] = []
    for position, cluster in enumerate(clusters):
        cluster.sort(key=lambda item: (item["y1"], item["x1"]))
        texts: list[str] = []
        for line in cluster:
            if texts and line["text"] == texts[-1]:
                continue
            texts.append(line["text"])
        joined = " ".join(texts).strip()
        if len(joined) < 2:
            continue
        confidence = sum(item["confidence"] for item in cluster) / len(cluster)
        detected.append(
            DetectedCard(
                ocr_text=joined,
                confidence=confidence,
                position=position,
            )
        )

    return detected[:10]


def extract_event_text_regions(img: np.ndarray) -> list[np.ndarray]:
    """이벤트 화면에서 본문/선택지 텍스트 영역 추출."""
    h, w = img.shape[:2]
    regions = [
        img[int(h * 0.08):int(h * 0.62), int(w * 0.05):int(w * 0.64)],
        img[int(h * 0.45):int(h * 0.92), int(w * 0.04):int(w * 0.72)],
    ]
    return [region for region in regions if region.size > 0]


def extract_opening_choice_region(img: np.ndarray) -> np.ndarray:
    """오프닝 선택지 패널 영역 추출."""
    h, w = img.shape[:2]
    return img[int(h * 0.66):int(h * 0.96), int(w * 0.28):int(w * 0.76)]


def extract_event_choice_region(img: np.ndarray) -> np.ndarray:
    """일반 이벤트 선택지 버튼 영역 추출."""
    h, w = img.shape[:2]
    return img[int(h * 0.44):int(h * 0.92), int(w * 0.58):int(w * 0.96)]


def extract_shop_region(img: np.ndarray) -> np.ndarray:
    """상점 상품 텍스트가 주로 있는 영역 추출."""
    h, w = img.shape[:2]
    return img[int(h * 0.18):int(h * 0.92), int(w * 0.10):int(w * 0.90)]


def extract_card_choice_prompt_region(img: np.ndarray) -> np.ndarray:
    """카드 선택 안내 문구(하단 중앙) 영역 추출."""
    h, w = img.shape[:2]
    return img[int(h * 0.86):int(h * 0.98), int(w * 0.20):int(w * 0.80)]


def _normalize_choice_line(text: str) -> str:
    replacements = {
        "댁": "덱",
        "카드틀": "카드를",
        "항금": "황금",
        "확득": "획득",
        "골드틀": "골드를",
        "여느": "여는",
        "정말한": "정밀한",
        "강화원": "강화된",
        "비z엎": "비어 있",
        "연습하다": "얻습니다",
    }
    normalized = clean_game_text(text)
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    return normalized.strip()


def ocr_opening_choices(img: np.ndarray, reader) -> list[DetectedChoice]:
    """오프닝 시작 선택지의 제목/설명 OCR."""
    region = extract_opening_choice_region(img)
    if region.size == 0:
        return []

    results = reader.readtext(region)
    if len(results) < 4:
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        boosted = cv2.convertScaleAbs(gray, alpha=1.35, beta=12)
        thresholded = cv2.adaptiveThreshold(
            boosted,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            31,
            11,
        )
        fallback_results = reader.readtext(thresholded)
        if len(fallback_results) > len(results):
            results = fallback_results

    lines = []
    for box, text, conf in results:
        text = _normalize_choice_line(text)
        if not text:
            continue
        ys = [pt[1] for pt in box]
        xs = [pt[0] for pt in box]
        lines.append(
            {
                "text": text,
                "confidence": float(conf),
                "y1": float(min(ys)),
                "y2": float(max(ys)),
                "x1": float(min(xs)),
            }
        )

    lines.sort(key=lambda item: (item["y1"], item["x1"]))
    clusters: list[list[dict]] = []
    for line in lines:
        if not clusters or line["y1"] - clusters[-1][-1]["y2"] > 18:
            clusters.append([line])
        else:
            clusters[-1].append(line)

    choices: list[DetectedChoice] = []
    h, w = img.shape[:2]
    region_x1 = float(w * 0.28)
    region_y1 = float(h * 0.66)
    for cluster in clusters[:4]:
        if not cluster:
            continue
        title = cluster[0]["text"]
        description = " ".join(line["text"] for line in cluster[1:])
        confidence = sum(line["confidence"] for line in cluster) / len(cluster)
        if len(title) < 2:
            continue
        cluster_x1 = min(line["x1"] for line in cluster)
        cluster_y1 = min(line["y1"] for line in cluster)
        cluster_y2 = max(line["y2"] for line in cluster)
        anchor = {
            "x": min(0.96, (region_x1 + cluster_x1) / w + 0.16),
            "y": min(0.96, max(0.04, (region_y1 + (cluster_y1 + cluster_y2) / 2.0) / h)),
        }
        choices.append(
            DetectedChoice(
                title=title,
                description=description,
                confidence=confidence,
                position=len(choices),
                screen_anchor=anchor,
            )
        )

    return choices


def ocr_event_choices(img: np.ndarray, reader) -> list[DetectedChoice]:
    """이벤트 선택지 버튼의 제목/설명과 화면 앵커를 OCR."""
    region = extract_event_choice_region(img)
    if region.size == 0:
        return []

    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    boosted = cv2.convertScaleAbs(gray, alpha=1.35, beta=14)
    thresholded = cv2.adaptiveThreshold(
        boosted,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        11,
    )

    lines = []
    for candidate in (region, thresholded):
        results = reader.readtext(candidate)
        for box, text, conf in results:
            text = _normalize_choice_line(text)
            if not text:
                continue
            ys = [pt[1] for pt in box]
            xs = [pt[0] for pt in box]
            x1 = float(min(xs))
            x2 = float(max(xs))
            y1 = float(min(ys))
            y2 = float(max(ys))
            if (x2 - x1) < 14 or (y2 - y1) < 8:
                continue
            lines.append(
                {
                    "text": text,
                    "confidence": float(conf),
                    "y1": y1,
                    "y2": y2,
                    "x1": x1,
                    "x2": x2,
                }
            )

    if not lines:
        return []

    lines.sort(key=lambda item: (item["y1"], item["x1"]))
    deduped = []
    for line in lines:
        duplicate = next(
            (
                existing
                for existing in deduped
                if abs(existing["y1"] - line["y1"]) < 10
                and abs(existing["x1"] - line["x1"]) < 18
                and existing["text"] == line["text"]
            ),
            None,
        )
        if duplicate is None or line["confidence"] > duplicate["confidence"]:
            if duplicate is not None:
                deduped.remove(duplicate)
            deduped.append(line)

    deduped.sort(key=lambda item: (item["y1"], item["x1"]))
    clusters: list[list[dict]] = []
    for line in deduped:
        if not clusters or line["y1"] - clusters[-1][-1]["y2"] > 20:
            clusters.append([line])
        else:
            clusters[-1].append(line)

    h, w = img.shape[:2]
    region_x1 = float(w * 0.58)
    region_y1 = float(h * 0.44)
    choices: list[DetectedChoice] = []
    for cluster in clusters[:4]:
        if not cluster:
            continue
        title = cluster[0]["text"]
        if len(title) < 1:
            continue
        description = " ".join(line["text"] for line in cluster[1:])
        confidence = sum(line["confidence"] for line in cluster) / len(cluster)
        cluster_x2 = max(line["x2"] for line in cluster)
        cluster_y1 = min(line["y1"] for line in cluster)
        cluster_y2 = max(line["y2"] for line in cluster)
        anchor = {
            "x": min(0.98, max(0.04, (region_x1 + cluster_x2) / w + 0.03)),
            "y": min(0.96, max(0.04, (region_y1 + (cluster_y1 + cluster_y2) / 2.0) / h)),
        }
        choices.append(
            DetectedChoice(
                title=title,
                description=description,
                confidence=confidence,
                position=len(choices),
                screen_anchor=anchor,
            )
        )

    return choices


def ocr_event_text(img: np.ndarray, reader) -> str:
    """이벤트 화면 OCR. 본문과 선택지를 합쳐서 반환."""
    collected: list[str] = []
    for region in extract_event_text_regions(img):
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        boosted = cv2.convertScaleAbs(gray, alpha=1.3, beta=10)
        thresholded = cv2.adaptiveThreshold(
            boosted,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            31,
            11,
        )

        for candidate in (region, thresholded):
            results = reader.readtext(candidate)
            for _, text, _ in results:
                text = text.strip()
                if text and text not in collected:
                    collected.append(text)
    return " ".join(collected)


def ocr_card_choice_prompt(img: np.ndarray, reader) -> str:
    """하단 카드 선택 안내 문구 OCR."""
    region = extract_card_choice_prompt_region(img)
    if region.size == 0:
        return ""

    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    boosted = cv2.convertScaleAbs(gray, alpha=1.45, beta=18)
    thresholded = cv2.adaptiveThreshold(
        boosted,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        7,
    )

    collected: list[str] = []
    for candidate in (region, thresholded):
        results = reader.readtext(candidate)
        for _, text, _ in results:
            cleaned = clean_game_text(text).strip()
            if cleaned and cleaned not in collected:
                collected.append(cleaned)
    return " ".join(collected)


def ocr_regent_star_count(img: np.ndarray, reader) -> int | None:
    """Regent 전투/맵 HUD의 현재 별 자원 수를 읽는다."""
    if img.size == 0:
        return None

    h, w = img.shape[:2]
    region = img[int(h * 0.035):int(h * 0.115), int(w * 0.43):int(w * 0.50)]
    if region.size == 0:
        return None

    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    enlarged = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    thresholded = cv2.threshold(enlarged, 150, 255, cv2.THRESH_BINARY)[1]

    candidates: list[tuple[float, int]] = []
    for candidate in (enlarged, thresholded):
        results = reader.readtext(candidate, detail=1, paragraph=False, allowlist="0123456789")
        for box, text, conf in results:
            digits = "".join(ch for ch in text if ch.isdigit())
            if not digits or len(digits) > 2:
                continue
            ys = [pt[1] for pt in box]
            cy = (min(ys) + max(ys)) / 2
            height = max(ys) - min(ys)
            if cy < candidate.shape[0] * 0.45:
                continue
            if height < candidate.shape[0] * 0.16:
                continue

            value = int(digits)
            if value < 0 or value > 20:
                continue

            score = float(conf) - max(len(digits) - 1, 0) * 0.18 + min(height / candidate.shape[0], 0.45)
            candidates.append((score, value))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def ocr_shop_text_lines(img: np.ndarray, reader) -> list[OCRTextLine]:
    """상점 화면 상품명/가격/제거 버튼용 OCR 라인 추출."""
    region = extract_shop_region(img)
    if region.size == 0:
        return []

    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    boosted = cv2.convertScaleAbs(gray, alpha=1.35, beta=10)
    thresholded = cv2.adaptiveThreshold(
        boosted,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        9,
    )

    lines: dict[tuple[int, int, str], OCRTextLine] = {}
    for candidate in (region, thresholded):
        results = reader.readtext(candidate)
        for box, text, conf in results:
            text = clean_game_text(text).strip()
            if not text:
                continue
            xs = [pt[0] for pt in box]
            ys = [pt[1] for pt in box]
            x1, y1, x2, y2 = float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))
            if (x2 - x1) < 12 or (y2 - y1) < 8:
                continue
            line = OCRTextLine(
                text=text,
                confidence=float(conf),
                x1=x1 / max(region.shape[1], 1),
                y1=y1 / max(region.shape[0], 1),
                x2=x2 / max(region.shape[1], 1),
                y2=y2 / max(region.shape[0], 1),
            )
            key = (int(round(line.cx * 40)), int(round(line.cy * 60)), text[:18])
            existing = lines.get(key)
            if existing is None or line.confidence > existing.confidence:
                lines[key] = line

    result = sorted(lines.values(), key=lambda item: (item.cy, item.cx))
    return result


def detect_map_current_anchor(img: np.ndarray) -> dict | None:
    """맵 화면의 현재 위치 오렌지 마커 중심을 찾는다.

    맵은 세로 스크롤이 가능하므로, 오버레이 쪽 고정 비율 대신
    실제 화면에서 현재 노드 앵커를 잡아 마커 위치를 보정한다.
    """
    if img.size == 0:
        return None

    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    # 맵 현재 위치의 주황색 다이아/점등 마커를 노린다.
    mask = cv2.inRange(hsv, (5, 120, 120), (30, 255, 255))
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    # 상단 UI, 좌측 뒤로가기 버튼, 우측 범례 쪽은 제외한다.
    mask[: int(h * 0.52), :] = 0
    mask[:, : int(w * 0.16)] = 0
    mask[:, int(w * 0.86):] = 0

    component_count, _, stats, centroids = cv2.connectedComponentsWithStats(mask)
    best: tuple[float, float, float, float, int] | None = None
    for idx in range(1, component_count):
        x, y, comp_w, comp_h, area = stats[idx]
        if area < 80 or area > 420:
            continue
        if comp_w < 8 or comp_h < 10 or comp_w > 36 or comp_h > 40:
            continue

        cx, cy = centroids[idx]
        if cy < h * 0.55:
            continue

        # 아래쪽에 있고, 가운데 쪽에 가까울수록 가점.
        center_bias = 1.0 - min(abs(cx - (w / 2)) / max(w * 0.35, 1), 1.0)
        vertical_bias = cy / max(h, 1)
        score = area * 0.8 + vertical_bias * 120 + center_bias * 40
        if best is None or score > best[0]:
            best = (score, float(cx), float(cy), center_bias, int(area))

    if best is None:
        return None

    _, cx, cy, center_bias, area = best
    confidence = float(min(0.99, 0.45 + center_bias * 0.2 + min(area / 260.0, 0.34)))
    return {
        "x": float(cx / w),
        "y": float(cy / h),
        "confidence": float(round(confidence, 3)),
        "area": area,
    }


def detect_map_node_rows(img: np.ndarray) -> list[dict]:
    """맵 위에 보이는 노드 행들의 중심 좌표를 추출한다."""
    if img.size == 0:
        return []

    h, w = img.shape[:2]
    x1, x2 = int(w * 0.15), int(w * 0.84)
    y1, y2 = int(h * 0.12), int(h * 0.95)
    crop = img[y1:y2, x1:x2]
    if crop.size == 0:
        return []

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    mask = (gray < 120).astype(np.uint8) * 255
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    component_count, _, stats, centroids = cv2.connectedComponentsWithStats(mask)
    points: list[tuple[float, float]] = []
    for idx in range(1, component_count):
        x, y, comp_w, comp_h, area = stats[idx]
        norm_y = float((y1 + centroids[idx][1]) / h)
        max_area = 1500 if norm_y > 0.7 else 900
        max_size = 48 if norm_y > 0.7 else 40
        if area < 420 or area > max_area:
            continue
        if comp_w < 20 or comp_w > max_size or comp_h < 20 or comp_h > max_size:
            continue
        cx, cy = centroids[idx]
        points.append((float(x1 + cx), float(y1 + cy)))

    points.sort(key=lambda point: point[1])
    rows: list[dict] = []
    row_merge_threshold = max(36.0, h * 0.045)
    for px, py in points:
        if not rows or abs(py - rows[-1]["y_px"]) > row_merge_threshold:
            rows.append({"y_px": py, "nodes_px": [(px, py)]})
            continue
        rows[-1]["nodes_px"].append((px, py))
        rows[-1]["y_px"] = sum(node[1] for node in rows[-1]["nodes_px"]) / len(rows[-1]["nodes_px"])

    normalized_rows: list[dict] = []
    for row in rows:
        nodes = sorted(row["nodes_px"], key=lambda node: node[0])
        normalized_rows.append(
            {
                "y": float(row["y_px"] / h),
                "nodes": [
                    {
                        "x": float(px / w),
                        "y": float(py / h),
                    }
                    for px, py in nodes
                ],
            }
        )

    return normalized_rows


if __name__ == "__main__":
    window_id = find_game_window()
    if window_id is None:
        print("Game window not found")
    else:
        print(f"Game window ID: {window_id}")
        img = capture_window(window_id)
        if img is not None:
            print(f"Captured: {img.shape[1]}x{img.shape[0]}")
            is_card_screen = detect_card_reward_screen(img)
            print(f"Card reward screen: {is_card_screen}")

            if is_card_screen:
                import easyocr
                reader = easyocr.Reader(["ko", "en"], gpu=False, verbose=False)
                regions = extract_card_regions(img)
                cards = ocr_card_names(regions, reader)
                for card in cards:
                    print(f"  Card {card.position + 1}: '{card.ocr_text}' ({card.confidence:.2f})")
            else:
                import easyocr
                reader = easyocr.Reader(["ko", "en"], gpu=False, verbose=False)
                print("Event OCR:", ocr_event_text(img, reader))
