def get_current_portfolio() -> dict:
    try:
        from moomoo import OpenSecTradeContext, TrdMarket, RET_OK
        with OpenSecTradeContext(filter_trdmarket=TrdMarket.HK, host='127.0.0.1', port=11111) as ctx:
            ret, data = ctx.position_list_query()
            if ret == RET_OK and not data.empty:
                return dict(zip(data["code"].str.replace("HK.", ""), data["qty"].astype(int)))
    except ImportError:
        print("[futu_trader] moomoo 未安裝，返回空持倉（dry run 模式）")
    except Exception as e:
        print(f"[futu_trader] 取得持倉失敗: {e}")
    return {}

def execute_trades(decisions: list[dict], dry_run=True) -> list[dict]:
    results = []

    if dry_run:
        for d in decisions:
            if d["action"] != "HOLD":
                print(f"[DRY RUN] {d['action']} HK.{d['ticker']} x{d['quantity']}")
                results.append({"status": "simulated", **d})
        return results

    try:
        from moomoo import OpenSecTradeContext, TrdSide, OrderType, TrdMarket, RET_OK  # noqa: F401
        with OpenSecTradeContext(filter_trdmarket=TrdMarket.HK, host='127.0.0.1', port=11111) as ctx:
            for d in decisions:
                if d["action"] == "HOLD" or d["quantity"] == 0:
                    continue
                side = TrdSide.BUY if d["action"] == "BUY" else TrdSide.SELL
                ret, data = ctx.place_order(
                    price=0,
                    qty=d["quantity"],
                    code=f"HK.{d['ticker']}",
                    trd_side=side,
                    order_type=OrderType.MARKET,
                )
                results.append({"status": "ok" if ret == RET_OK else "error", **d})
    except Exception as e:
        print(f"[futu_trader] 下單失敗: {e}")

    return results
