"""Apple Vision 프레임워크 기반 OCR - EasyOCR 대체."""

import cv2
import numpy as np
from dataclasses import dataclass

import Vision
from Foundation import NSData


@dataclass
class DetectedCard:
    ocr_text: str
    confidence: float
    position: int


def _vision_ocr(image: np.ndarray, languages: list[str] | None = None) -> list[tuple[str, float]]:
    """numpy 이미지에 Vision OCR 실행. [(text, confidence)] 반환."""
    if languages is None:
        languages = ["ko", "en"]

    _, png_buf = cv2.imencode(".png", image)
    ns_data = NSData.dataWithBytes_length_(png_buf.tobytes(), len(png_buf))

    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(ns_data, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLanguages_(languages)
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)

    success, error = handler.performRequests_error_([request], None)
    if not success:
        return []

    results = []
    for obs in request.results() or []:
        candidates = obs.topCandidates_(1)
        if candidates:
            results.append((candidates[0].string(), candidates[0].confidence()))
    return results


def warmup():
    """Vision 모델 워밍업 (첫 호출 ~2초 → 이후 ~0.1초)."""
    dummy = np.zeros((50, 200, 3), dtype=np.uint8)
    cv2.putText(dummy, "warmup", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    _vision_ocr(dummy)


def ocr_card_names(card_regions: list[np.ndarray]) -> list[DetectedCard]:
    """카드 영역에서 텍스트 인식. reader 파라미터 불필요."""
    detected = []
    for i, region in enumerate(card_regions):
        results = _vision_ocr(region)
        if results:
            all_text = " ".join(r[0] for r in results)
            avg_conf = sum(r[1] for r in results) / len(results)
            detected.append(DetectedCard(
                ocr_text=all_text,
                confidence=avg_conf,
                position=i,
            ))
        else:
            detected.append(DetectedCard(ocr_text="", confidence=0, position=i))
    return detected


def ocr_region(image: np.ndarray) -> str:
    """단일 이미지 영역 OCR → 텍스트 반환."""
    results = _vision_ocr(image)
    return " ".join(r[0] for r in results)


if __name__ == "__main__":
    import time

    print("Warming up Vision...")
    start = time.time()
    warmup()
    print(f"Warmup: {time.time() - start:.2f}s")

    from screen_capture import extract_card_regions

    img = cv2.imread("/tmp/sts2_current.png")
    if img is None:
        print("No test image at /tmp/sts2_current.png")
    else:
        regions = extract_card_regions(img)
        start = time.time()
        cards = ocr_card_names(regions)
        elapsed = time.time() - start
        print(f"\nOCR {len(cards)} cards in {elapsed:.2f}s:")
        for card in cards:
            print(f"  Card {card.position + 1} [{card.confidence:.2f}]: {card.ocr_text[:60]}")
