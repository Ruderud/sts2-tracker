"""STS2 전투 조언 모듈 - 덱 분석 기반 일반 전투 팁 생성."""

from save_parser import RunState
from recommender import analyze_deck


def generate_combat_advice(state: RunState, cards_db: list[dict]) -> list[str]:
    """현재 덱과 상태를 분석해서 전투 팁 생성.

    Returns: 우선순위 순 팁 리스트 (최대 5개).
    """
    analysis = analyze_deck(state, cards_db)
    tips: list[str] = []

    # 1) ★ 시너지 팁
    if analysis["star_generators"] >= 2 and analysis["star_consumers"] >= 1:
        tips.append("★ 생성 카드를 먼저 사용해 ★ 축적")
    elif analysis["star_generators"] >= 1 and analysis["star_consumers"] >= 1:
        tips.append("★ 생성 카드로 ★ 확보 후 소비 카드 사용")

    # 2) 파워 카드 팁
    if analysis["powers"] >= 2:
        tips.append("파워 카드 우선 사용 (초반 턴 활용)")
    elif analysis["powers"] == 1:
        tips.append("파워 카드를 첫 턴에 사용")

    # 3) 방어 부족 경고
    total = max(analysis["total"], 1)
    block_ratio = analysis["total_block"] / total
    attack_ratio = analysis["attacks"] / total
    if block_ratio < 3.0 and analysis["skills"] < analysis["attacks"]:
        tips.append("방어 카드 부족 - 방어 우선 고려")

    # 4) 멀티히트 팁
    if analysis["multi_hit"] >= 2:
        tips.append("멀티히트 카드로 딜 극대화 (힘 버프 시 효과 증폭)")

    # 5) 에너지 관리 팁
    if analysis["avg_cost"] > 1.8:
        tips.append("평균 코스트 높음 - 에너지 관리 주의")
    elif analysis["avg_cost"] <= 1.0:
        tips.append("저코스트 덱 - 많은 카드를 매 턴 사용")

    # 6) 드로우 팁
    if analysis["total_draw"] >= 3:
        tips.append("드로우 카드 먼저 사용해 핸드 확장")

    # 7) HP 기반 팁
    hp_pct = state.current_hp / max(state.max_hp, 1)
    if hp_pct < 0.3:
        tips.insert(0, "⚠ HP 위험 - 방어 최우선, 장기전 회피")
    elif hp_pct < 0.5:
        tips.append("HP 낮음 - 불필요한 피해 최소화")

    # 8) 덱 크기 팁
    if analysis["total"] <= 12:
        tips.append("슬림 덱 - 핵심 카드 빠르게 순환")
    elif analysis["total"] > 25:
        tips.append("덱 과대 - 핵심 카드 드로우 확률 낮음")

    # 9) 기본 카드 비율 팁
    basic_ratio = analysis["basics"] / total
    if basic_ratio > 0.5:
        tips.append("기본 카드 비율 높음 - 스트라이크/수비 의존 주의")

    return tips[:5]


if __name__ == "__main__":
    from save_parser import parse_save
    from card_db import load_card_db

    state = parse_save()
    if state is None:
        print("No active run found")
    else:
        cards_db = load_card_db()
        analysis = analyze_deck(state, cards_db)

        print(f"=== 전투 조언 ({state.character}) ===")
        print(f"HP: {state.current_hp}/{state.max_hp}")
        print(f"덱: {analysis['total']}장 (공격 {analysis['attacks']} / 스킬 {analysis['skills']} / 파워 {analysis['powers']})")
        print(f"★생성: {analysis['star_generators']} | ★소비: {analysis['star_consumers']}")
        print(f"멀티히트: {analysis['multi_hit']} | 평균코스트: {analysis['avg_cost']:.1f}")
        print(f"기본카드: {analysis['basics']}장")
        print()

        tips = generate_combat_advice(state, cards_db)
        if tips:
            print("전투 팁:")
            for i, tip in enumerate(tips, 1):
                print(f"  {i}. {tip}")
        else:
            print("(특별한 팁 없음)")
