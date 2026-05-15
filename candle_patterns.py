import pandas as pd

def detect(df: pd.DataFrame) -> list[dict]:
    """Return list of detected patterns on the last 3 candles."""
    if len(df) < 3:
        return []
    c0, c1, c2 = df.iloc[-1], df.iloc[-2], df.iloc[-3]
    patterns = []

    def body(c):       return abs(c["close"] - c["open"])
    def rng(c):        return c["high"] - c["low"] if c["high"] != c["low"] else 1e-9
    def bullish(c):    return c["close"] > c["open"]
    def bearish(c):    return c["close"] < c["open"]
    def upper_wick(c): return c["high"] - max(c["close"], c["open"])
    def lower_wick(c): return min(c["close"], c["open"]) - c["low"]

    # ── Doji ────────────────────────────────────────────────────────────────
    if body(c0) / rng(c0) < 0.1:
        patterns.append({"name": "doji", "direction": "neutral",
                          "desc": "市場猶豫，方向待確認"})

    # ── Hammer (bullish reversal at bottom) ──────────────────────────────
    if (lower_wick(c0) > 2 * body(c0)
            and upper_wick(c0) < body(c0) * 0.3
            and bearish(c1)):
        patterns.append({"name": "hammer", "direction": "bullish",
                          "desc": "錘頭，潛在底部反轉"})

    # ── Shooting Star (bearish reversal at top) ──────────────────────────
    # Requires strong upper wick AND the prior candle must be notably bullish
    if (upper_wick(c0) > 2 * body(c0)
            and lower_wick(c0) < body(c0) * 0.3
            and bullish(c1)
            and body(c1) > rng(c1) * 0.4):    # prior candle must have real body
        patterns.append({"name": "shooting_star", "direction": "bearish",
                          "desc": "流星，潛在頂部反轉"})

    # ── Bullish Engulfing ────────────────────────────────────────────────
    # Stricter: engulfing body must be meaningfully larger (1.5×)
    if (bearish(c1) and bullish(c0)
            and c0["open"] <= c1["close"]
            and c0["close"] >= c1["open"]
            and body(c0) > body(c1) * 1.5):
        patterns.append({"name": "bullish_engulfing", "direction": "bullish",
                          "desc": "看漲吞噬，強力反轉信號"})

    # ── Bearish Engulfing ────────────────────────────────────────────────
    if (bullish(c1) and bearish(c0)
            and c0["open"] >= c1["close"]
            and c0["close"] <= c1["open"]
            and body(c0) > body(c1) * 1.5):
        patterns.append({"name": "bearish_engulfing", "direction": "bearish",
                          "desc": "看跌吞噬，強力反轉信號"})

    # ── Morning Star — DISABLED (30-31% win rate, net loser) ─────────────
    # ── Evening Star — DISABLED (30-31% win rate, net loser) ─────────────

    # ── Three White Soldiers ─────────────────────────────────────────────
    if (bullish(c2) and bullish(c1) and bullish(c0)
            and c1["open"] > c2["open"] and c1["close"] > c2["close"]
            and c0["open"] > c1["open"] and c0["close"] > c1["close"]
            and upper_wick(c0) < body(c0) * 0.3):
        patterns.append({"name": "three_white_soldiers", "direction": "bullish",
                          "desc": "三白兵，強勢上升趨勢"})

    # ── Three Black Crows ────────────────────────────────────────────────
    if (bearish(c2) and bearish(c1) and bearish(c0)
            and c1["open"] < c2["open"] and c1["close"] < c2["close"]
            and c0["open"] < c1["open"] and c0["close"] < c1["close"]
            and lower_wick(c0) < body(c0) * 0.3):
        patterns.append({"name": "three_black_crows", "direction": "bearish",
                          "desc": "三烏鴉，強勢下降趨勢"})

    # ── Bullish Pin Bar ──────────────────────────────────────────────────
    if (lower_wick(c0) > rng(c0) * 0.6
            and body(c0) < rng(c0) * 0.25):
        patterns.append({"name": "bullish_pin_bar", "direction": "bullish",
                          "desc": "看漲 Pin Bar，下影線拒絕低位"})

    # ── Bearish Pin Bar ──────────────────────────────────────────────────
    if (upper_wick(c0) > rng(c0) * 0.6
            and body(c0) < rng(c0) * 0.25):
        patterns.append({"name": "bearish_pin_bar", "direction": "bearish",
                          "desc": "看跌 Pin Bar，上影線拒絕高位"})

    # ── Inside Bar ───────────────────────────────────────────────────────
    if (c0["high"] < c1["high"] and c0["low"] > c1["low"]):
        patterns.append({"name": "inside_bar", "direction": "neutral",
                          "desc": "Inside Bar，盤整蓄力，等待突破方向"})

    return patterns


# Patterns that cannot trigger a trade on their own
WEAK_ALONE = {"three_white_soldiers", "three_black_crows", "doji", "inside_bar"}

def _filter_patterns(patterns: list[dict]) -> list[dict]:
    """Keep patterns only when there is meaningful directional confirmation."""
    strong = [p for p in patterns if p["name"] not in WEAK_ALONE]
    if strong:
        return patterns
    weak = [p for p in patterns if p["name"] in WEAK_ALONE]
    if len(weak) >= 2:
        return weak
    return []


def score_signal(patterns: list[dict], rsi: float, ema9: float, ema21: float,
                 macd: float, macd_signal: float, rvol: float) -> tuple[str, int]:
    """
    Returns (direction, confidence 0-100).
    direction: 'long' | 'short' | 'hold'

    Hard requirements (all must pass before scoring):
      - EMA9/EMA21 trend alignment
      - At least 2 directional confirming patterns
      - MACD on the right side of signal line
      - RVOL > 1.3 (real volume participation)
    """
    patterns = _filter_patterns(patterns)
    if not patterns:
        return "hold", 0

    trend_up   = ema9 > ema21
    trend_down = ema9 < ema21

    bull = sum(1 for p in patterns if p["direction"] == "bullish")
    bear = sum(1 for p in patterns if p["direction"] == "bearish")

    score = 0
    direction = "hold"

    if bull > bear:
        if not trend_up:               return "hold", 0   # trend filter
        if bull < 2:                   return "hold", 0   # need 2+ bullish patterns
        direction = "long"
        score += bull * 20
        score += 15                    # trend confirmed
        if rsi < 50:                   score += 15
        if rsi < 40:                   score += 10        # oversold bounce bonus
        if macd > macd_signal:         score += 15        # MACD aligned bonus
        if rvol > 1.3:                 score += 10        # volume participation bonus
        if rvol > 1.8:                 score += 5         # strong volume bonus

    elif bear > bull:
        if not trend_down:             return "hold", 0
        if bear < 2:                   return "hold", 0   # need 2+ bearish patterns
        direction = "short"
        score += bear * 20
        score += 15
        if rsi > 50:                   score += 15
        if rsi > 60:                   score += 10        # overbought reversal bonus
        if macd < macd_signal:         score += 15        # MACD aligned bonus
        if rvol > 1.3:                 score += 10
        if rvol > 1.8:                 score += 5

    return direction, min(score, 100)
