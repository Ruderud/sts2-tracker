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
    """카드 보상 화면인지 감지 (배너 영역 색상 패턴)."""
    h, w = img.shape[:2]
    # 배너 "카드를 선택하세요"는 y=29-33%, x=30-70% 영역
    banner = img[int(h * 0.29):int(h * 0.33), int(w * 0.3):int(w * 0.7)]
    hsv = cv2.cvtColor(banner, cv2.COLOR_BGR2HSV)
    # 갈색/베이지 배너 감지 (H=10-30, S=30+, V=100+)
    mask = cv2.inRange(hsv, (5, 30, 80), (40, 255, 255))
    ratio = np.count_nonzero(mask) / mask.size
    return ratio > 0.10


def extract_card_regions(img: np.ndarray) -> list[np.ndarray]:
    """카드 보상 화면에서 개별 카드 영역 추출."""
    h, w = img.shape[:2]
    # 카드 위치 (밝기 프로파일 기반)
    card_bounds = [
        (0.24, 0.34, 0.40, 0.68),  # card 1
        (0.42, 0.34, 0.58, 0.68),  # card 2
        (0.59, 0.34, 0.75, 0.68),  # card 3
    ]
    regions = []
    for x1, y1, x2, y2 in card_bounds:
        crop = img[int(h * y1):int(h * y2), int(w * x1):int(w * x2)]
        regions.append(crop)
    return regions


def extract_card_name_regions(img: np.ndarray) -> list[np.ndarray]:
    """카드 이름 배너 영역만 추출 (OCR 정확도 향상용)."""
    h, w = img.shape[:2]
    # 카드 이름 리본: y=33-36%
    name_bounds = [
        (0.27, 0.33, 0.40, 0.37),  # card 1 name
        (0.44, 0.33, 0.57, 0.37),  # card 2 name
        (0.61, 0.33, 0.74, 0.37),  # card 3 name
    ]
    regions = []
    for x1, y1, x2, y2 in name_bounds:
        crop = img[int(h * y1):int(h * y2), int(w * x1):int(w * x2)]
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
