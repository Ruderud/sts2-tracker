"""STS2 화면 캡처 및 카드 인식 - Quartz + EasyOCR."""

import cv2
import numpy as np
from dataclasses import dataclass

import Quartz
from AppKit import NSBitmapImageRep


GAME_TITLE = "Slay the Spire 2"


@dataclass
class DetectedCard:
    ocr_text: str
    confidence: float
    position: int  # 0-based index (left to right)


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

    End Turn 버튼: 우하단 (x=82-95%, y=82-93%) - 밝은 베이지/골드 영역
    에너지 오브: 좌하단 (x=3-10%, y=78-90%) - 밝은 원형 영역
    둘 중 하나라도 감지되면 전투 화면으로 판단.
    """
    h, w = img.shape[:2]

    # End Turn 버튼 영역 감지 (우하단)
    end_turn = img[int(h * 0.82):int(h * 0.93), int(w * 0.82):int(w * 0.95)]
    hsv_btn = cv2.cvtColor(end_turn, cv2.COLOR_BGR2HSV)
    # 밝은 베이지/골드 버튼 (H=10-45, S=30+, V=120+)
    mask_btn = cv2.inRange(hsv_btn, (10, 30, 120), (45, 255, 255))
    btn_ratio = np.count_nonzero(mask_btn) / max(mask_btn.size, 1)

    # 에너지 오브 영역 감지 (좌하단)
    energy_orb = img[int(h * 0.78):int(h * 0.90), int(w * 0.03):int(w * 0.10)]
    hsv_orb = cv2.cvtColor(energy_orb, cv2.COLOR_BGR2HSV)
    # 밝은 에너지 오브 (높은 밝기, 낮은 채도 = 흰색/밝은 색)
    mask_orb = cv2.inRange(hsv_orb, (0, 0, 160), (180, 80, 255))
    orb_ratio = np.count_nonzero(mask_orb) / max(mask_orb.size, 1)

    return btn_ratio > 0.05 or orb_ratio > 0.08


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
