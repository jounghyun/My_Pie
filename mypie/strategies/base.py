"""전략 공통 틀.

모든 전략은 새 봉이 시작될 때(일봉이면 매일 09:00 KST) 직전까지의 봉과 이번 봉 시가를 보고 계획(DayPlan)을 세운다.
전략의 숫자(5일선, 20일 최고가 등)는 "봉 개수"라서, 판단 주기가 15분봉이면 5봉 = 1시간 15분이다.
봇(bot.py)과 백테스트(backtest.py)는 같은 계획을 같은 규칙으로 실행한다:

  1) 09시(시가): 보유 중이고 sell_at_open이면 매도 / 미보유이고 buy_at_open이면 매수 (방금 판 경우는 다시 사지 않음)
  2) 장중: 보유 중이고 가격이 sell_stop 이하로 내려가면 매도 (그날 산 건 제외)
           미보유이고 가격이 buy_stop 이상으로 올라가면 매수 (그날 이미 샀거나 손절했으면 제외)
"""

import re
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
    window = 150            # plan()에 넘기는 과거 봉 개수 (긴 기간을 보는 전략은 늘린다)

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


# 설명 문구의 자리표시. 판단 주기에 맞게 바꿔서 보여준다 (웹페이지 index.html의 fmtGuide와 같은 규칙).
#   {N:5}  → 일봉 "5일", 15분봉 "5봉(1시간 15분)"
#   {prev} → 어제 / 직전 봉     {cur} → 오늘 / 이번 봉
#   {next} → 다음 날 아침 9시에 / 다음 봉이 시작될 때     {hold} → 하루 / 한 봉
TOKEN_PATTERN = re.compile(r"\{(N:\d+|prev|cur|next|hold)\}")


def period_text(n: int, minutes: int) -> str:
    if minutes == 1440:
        return f"{n}일"
    total = n * minutes
    days, hours, mins = total // 1440, total % 1440 // 60, total % 60
    span = " ".join(part for part in (f"{days}일" if days else "", f"{hours}시간" if hours else "",
                                      f"{mins}분" if mins else "") if part)
    return f"{n}봉({span})"


def render_text(text: str, minutes: int = 1440) -> str:
    daily = minutes == 1440
    words = {"prev": "어제" if daily else "직전 봉", "cur": "오늘" if daily else "이번 봉",
             "next": "다음 날 아침 9시에" if daily else "다음 봉이 시작될 때", "hold": "하루" if daily else "한 봉"}

    def replace(match: re.Match) -> str:
        token = match.group(1)
        return period_text(int(token[2:]), minutes) if token.startswith("N:") else words[token]

    return TOKEN_PATTERN.sub(replace, text)
