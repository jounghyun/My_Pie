"""텔레그램으로 보내는 글: 매일 아침 리포트, /기록 · /상태 · /주식 · /도움 명령에 대한 답장."""

from pathlib import Path

from .db import Database

HELP = ("🥧 My_Pie 명령어\n"
        "/상태 (/status) — 지금 포트폴리오와 총 자산\n"
        "/기록 (/trades) — 최근 매매 기록 10건\n"
        "/주식 (/stock) — 주식 금고와 종목 계좌\n"
        "/재부팅 (/reboot) — 라즈베리파이 재부팅 (뒤에 '확인'을 붙여야 해요)\n"
        "/도움 (/help) — 이 안내")

COMMANDS = {  # 텔레그램 메뉴에 보일 영어 명령 (한글 명령도 똑같이 알아듣는다)
    "status": "지금 포트폴리오와 총 자산",
    "trades": "최근 매매 기록 10건",
    "stock": "주식 금고와 종목 계좌",
    "reboot": "라즈베리파이 재부팅 (뒤에 '확인')",
    "help": "명령어 안내",
}


def _sym(market: str) -> str:
    return market.split("-")[-1]


def _price(v: float) -> str:
    return f"{v:,.0f}" if v >= 100 else f"{v:,.4g}"


def trade_line(t: dict) -> str:
    side = "🔴 매수" if t["side"] == "buy" else "🔵 매도"
    tag = "[실거래] " if t.get("mode") == "live" else ""
    line = f"{t['ts'][5:16]} {tag}{side} {_sym(t['market'])} @{_price(t['price'])}원 · {t['krw']:,.0f}원"
    if t.get("profit") is not None:
        line += f" · 손익 {t['profit']:+,.0f}원"
    return line


def trades_text(trades: list[dict], title: str = "🧾 최근 매매 기록") -> str:
    if not trades:
        return f"{title}\n아직 매매 기록이 없어요."
    return title + "\n" + "\n".join(trade_line(t) for t in trades)


def status_text(status: dict | None) -> str:
    if not status:
        return "봇이 아직 상태를 남기지 않았어요. 잠시 뒤에 다시 물어봐 주세요."
    a = status["account"]
    mode = "실거래" if status["mode"] == "live" else "모의투자"
    ret = f"{a['return_pct']:+.2f}%" if a.get("return_pct") is not None else "-"
    lines = [f"💰 총 자산 {a['equity']:,.0f}원 ({ret}) · {mode}",
             f"넣은 돈 {a['contributions']:,.0f}원 · 금고 {a.get('vault', a.get('reserve', 0)):,.0f}원"]
    for s in status["slots"]:
        held = "🟢 보유" if s["holding"] else "⏳ 대기"
        slot_ret = f" · 계좌 수익률 {s['return_pct']:+.2f}%" if s.get("return_pct") is not None else ""
        lines.append(f"\n• {s.get('name') or _sym(s['market'])} {s.get('tf_label', '일봉')} · {s['strategy']['label']}"
                     f" · 원화 {s['book']['krw']:,.0f}원")
        lines.append(f"  {held} · 현재가 {_price(s['price'])}원{slot_ret}")
        lines.append(f"  {s['state_text']}")
    lines.append(f"\n({status['updated_at'][5:16]} 기준)")
    return "\n".join(lines)


def daily_text(trades: list[dict], status: dict | None) -> str:
    """아침 리포트: 지난 24시간 매매와 지금 상태."""
    sells = [t for t in trades if t["side"] == "sell" and t.get("profit") is not None]
    realized = sum(t["profit"] for t in sells)
    wins = sum(t["profit"] > 0 for t in sells)
    head = [f"📊 My_Pie 아침 리포트",
            f"지난 24시간 매매 {len(trades)}건" + (f" · 확정 손익 {realized:+,.0f}원 ({wins}승 {len(sells) - wins}패)" if sells else "")]
    body = [trade_line(t) for t in trades[-15:]]
    if len(trades) > 15:
        body.insert(0, f"(앞의 {len(trades) - 15}건 생략)")
    return "\n".join(head + body) + "\n\n" + status_text(status)


def reboot_step(text: str) -> str | None:
    """재부팅 명령인지: 'ask'(확인 안내만) | 'do'(확인까지 붙음) | None(다른 말). 실수로 눌러도 안 꺼지게 '확인'을 요구한다."""
    parts = (text or "").strip().split()
    if not parts or parts[0].split("@")[0].lstrip("/").lower() not in ("재부팅", "reboot"):
        return None
    return "do" if len(parts) > 1 and parts[1] in ("확인", "confirm", "yes") else "ask"


REBOOT_ASK = ("🔄 라즈베리파이를 재부팅할까요? 정말 하려면 아래처럼 보내세요.\n/재부팅 확인\n"
              "(1~2분 동안 매매·웹페이지가 멈췄다가 자동으로 다시 시작해요)")


def command_reply(text: str, db: Database, stock_db: str | None = None) -> str | None:
    """텔레그램 메시지 → 답장. 명령이 아닌 그냥 말에는 안내를 보낸다. stock_db: 주식 봇 DB 파일."""
    word = (text or "").strip().split()[0].split("@")[0].lstrip("/").lower() if (text or "").strip() else ""
    if word in ("상태", "status"):
        return status_text(db.get_state("status"))
    if word in ("기록", "trades", "매매"):
        return trades_text(db.recent_trades(10))
    if word in ("주식", "stock"):
        from .stockbot import status_text as stock_status_text

        if not stock_db or not Path(stock_db).exists():
            return stock_status_text(None)
        sdb = Database(stock_db, create=False)
        try:
            return stock_status_text(sdb.get_state("status"))
        finally:
            sdb.close()
    return HELP
