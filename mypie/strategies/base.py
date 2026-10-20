"""전략 공통 틀.

모든 전략은 매일 09:00(KST)에 '어제까지의 일봉'과 '오늘 시가'를 보고 하루 계획(DayPlan)을 세운다.
봇(bot.py)과 백테스트(backtest.py)는 같은 계획을 같은 규칙으로 실행한다:

  1) 09시(시가): 보유 중이고 sell_at_open이면 매도 / 미보유이고 buy_at_open이면 매수
  2) 장중: 보유 중이고 가격이 sell_stop 이하로 내려가면 매도 (그날 산 건 제외)
           미보유이고 가격이 buy_stop 이상으로 올라가면 매수 (그날 이미 샀거나 손절했으면 제외)
"""

from dataclasses import dataclass


@dataclass
class DayPlan:
    buy_at_open: bool = False        # 09시에 매수
    sell_at_open: bool = False       # 09시에 매도
    buy_stop: float | None = None    # 장중 이 가격 이상이면 매수 (돌파)
    sell_stop: float | None = None   # 장중 이 가격 이하이면 매도 (이탈)
    note: str = ""                   # 화면에 보여줄 한 줄 설명


class Strategy:
    name = ""               # 설정 파일과 API에서 쓰는 이름
    label = ""              # 화면에 보이는 이름
    description = ""        # 한 줄 설명
    guide = ""              # 초보자용 쉬운 설명 (원리 · 장점 · 약점)
    exit_next_open = False  # 산 다음 날 09시에 무조건 파는 전략인지 (화면 안내용)
    defaults: dict = {}

    def __init__(self, params: dict | None = None):
        self.params = {**self.defaults, **(params or {})}
        self.bar = str(self.params.pop("_bar", "일"))  # 화면 설명에 쓰는 봉 단위 ("일" 또는 "봉")

    def plan(self, history: list[dict], today_open: float) -> DayPlan:
        """history: 어제까지의 완성된 일봉 (오래된 것 → 최신). today_open: 오늘 시가."""
        raise NotImplementedError


class SignalStrategy(Strategy):
    """종가 기준 전략: 어제 종가까지 보고 '보유해야 하는지' 판단해서 09시에 사고판다."""

    def want(self, history: list[dict]) -> tuple[bool | None, str]:
        """(True=보유 / False=비보유 / None=현재 상태 유지, 화면용 설명)."""
        raise NotImplementedError

    def plan(self, history: list[dict], today_open: float) -> DayPlan:
        want, note = self.want(history)
        return DayPlan(buy_at_open=want is True, sell_at_open=want is False, note=note)


def closes(history: list[dict]) -> list[float]:
    return [c["close"] for c in history]
