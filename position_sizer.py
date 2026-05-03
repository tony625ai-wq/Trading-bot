def calculate_position(portfolio_value: float, entry_price: float,
                        stop_loss: float, risk_pct: float = 2.0) -> dict:
    """
    固定風險倉位計算：每次最多虧損 portfolio 的 risk_pct%
    e.g. $50,000 portfolio, 2% risk = max $1,000 loss per trade
    """
    if entry_price <= 0 or stop_loss <= 0 or entry_price == stop_loss:
        return {"shares": 0, "risk_amount": 0, "position_value": 0}

    risk_amount = portfolio_value * (risk_pct / 100)
    risk_per_share = abs(entry_price - stop_loss)
    shares = int(risk_amount / risk_per_share)
    position_value = round(shares * entry_price, 2)
    actual_risk_pct = round((risk_per_share * shares / portfolio_value) * 100, 2)

    return {
        "shares": shares,
        "risk_amount": round(risk_amount, 2),
        "position_value": position_value,
        "risk_per_share": round(risk_per_share, 2),
        "actual_risk_pct": actual_risk_pct,
    }
