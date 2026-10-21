"""기술적 지표. 입력 리스트는 모두 오래된 것 → 최신 순서이고, 데이터가 모자라면 None을 돌려준다."""


def sma(values: list[float], n: int) -> float | None:
    """단순 이동평균."""
    if n <= 0 or len(values) < n:
        return None
    return sum(values[-n:]) / n


def ema_series(values: list[float], n: int) -> list[float]:
    """지수 이동평균 시계열. 첫 값은 앞 n개의 단순 평균으로 시작한다."""
    if n <= 0 or len(values) < n:
        return []
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    out = [e]
    for v in values[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes: list[float], n: int = 14) -> float | None:
    """RSI (와일더 방식)."""
    if len(closes) < n + 1:
        return None
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    avg_gain, avg_loss = gain / n, loss / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(d, 0.0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-d, 0.0)) / n
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def bollinger(closes: list[float], n: int = 20, k: float = 2.0) -> tuple[float, float, float] | None:
    """볼린저 밴드 (하단, 중심, 상단)."""
    if len(closes) < n:
        return None
    window = closes[-n:]
    mid = sum(window) / n
    sd = (sum((x - mid) ** 2 for x in window) / n) ** 0.5
    return mid - k * sd, mid, mid + k * sd


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float] | None:
    """MACD선과 시그널선의 마지막 값."""
    if len(closes) < slow + signal:
        return None
    fast_ema = ema_series(closes, fast)[slow - fast:]
    slow_ema = ema_series(closes, slow)
    line = [f - s for f, s in zip(fast_ema, slow_ema)]
    return line[-1], ema_series(line, signal)[-1]


def stochastic(highs: list[float], lows: list[float], closes: list[float],
               n: int = 14, smooth: int = 3) -> tuple[float, float] | None:
    """슬로우 스토캐스틱 (%K, %D)의 마지막 값."""
    m = 2 * smooth - 1  # %D까지 구하는 데 필요한 패스트 %K 개수
    if len(closes) < n + m - 1:
        return None
    fast = []
    for i in range(len(closes) - m, len(closes)):
        hh = max(highs[i - n + 1:i + 1])
        ll = min(lows[i - n + 1:i + 1])
        fast.append(100 * (closes[i] - ll) / (hh - ll) if hh > ll else 50.0)
    slow_k = [sum(fast[j:j + smooth]) / smooth for j in range(smooth)]
    return slow_k[-1], sum(slow_k) / smooth


def true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    """True Range 시계열: 그 봉의 고가−저가와, 직전 종가에서 고가·저가까지 거리 중 가장 큰 값 (첫 봉은 고가−저가)."""
    out = [highs[0] - lows[0]] if closes else []
    for i in range(1, len(closes)):
        out.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    return out


def rma_series(values: list[float], n: int) -> list[float]:
    """와일더 이동평균 시계열 (ATR에 쓰는 방식). 첫 값은 앞 n개의 단순 평균, [0]은 values[n-1] 자리."""
    if n <= 0 or len(values) < n:
        return []
    r = sum(values[:n]) / n
    out = [r]
    for v in values[n:]:
        r = (r * (n - 1) + v) / n
        out.append(r)
    return out


def linreg(values: list[float], n: int) -> float | None:
    """최근 n개 값에 직선을 맞췄을 때 마지막 지점의 값 (TradingView의 linreg(src, n, 0)과 같음)."""
    if n <= 1 or len(values) < n:
        return None
    ys = values[-n:]
    x_mean, y_mean = (n - 1) / 2, sum(ys) / n
    sxx = sum((x - x_mean) ** 2 for x in range(n))
    slope = sum((x - x_mean) * (y - y_mean) for x, y in enumerate(ys)) / sxx
    return y_mean + slope * (n - 1 - x_mean)
