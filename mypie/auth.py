"""웹페이지 로그인: 텔레그램으로 받은 코드를 넣으면 그 기기는 한동안 로그인된 상태로 둔다.
로그인한 IP도 하루 동안은 다시 묻지 않는다.

코드와 로그인 정보는 원문 대신 해시로만 DB에 남긴다. 웹 서버가 재시작돼도 로그인은 유지된다.
"""

import hashlib
import hmac
import secrets
import time

from .db import Database
from .timeutil import ts

CODE_KEY = "login_code"
SENDS_KEY = "login_sends"
SESSIONS_KEY = "web_sessions"
TRUSTED_KEY = "web_trusted_ips"
CODE_TTL_SEC = 300        # 코드는 5분 동안만 유효
MAX_ATTEMPTS = 3          # 한 코드로 틀릴 수 있는 횟수 (넘으면 코드를 지운다)
RESEND_SEC = 30           # 코드를 다시 보낼 수 있는 간격
MAX_SENDS_PER_HOUR = 10   # 텔레그램 폭탄 방지
MAX_SESSIONS = 20


class TooSoon(Exception):
    def __init__(self, wait: int, message: str):
        super().__init__(message)
        self.wait = wait


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def issue_code(db: Database, digits: int, force: bool = False) -> str:
    """새 로그인 코드를 만든다 (이전 코드는 무효). 너무 자주 요청하면 TooSoon."""
    now = time.time()
    sends = [t for t in (db.get_state(SENDS_KEY) or []) if now - t < 3600]
    if not force:
        if sends and now - sends[-1] < RESEND_SEC:
            wait = int(RESEND_SEC - (now - sends[-1])) + 1
            raise TooSoon(wait, f"방금 코드를 보냈어요. {wait}초 뒤에 다시 보낼 수 있어요.")
        if len(sends) >= MAX_SENDS_PER_HOUR:
            raise TooSoon(int(3600 - (now - sends[0])) + 1, "1시간에 보낼 수 있는 코드를 다 썼어요. 잠시 뒤에 다시 해 주세요.")
    code = f"{secrets.randbelow(10 ** digits):0{digits}d}"
    db.set_state(CODE_KEY, {"hash": _hash(code), "expires": now + CODE_TTL_SEC, "attempts": 0})
    db.set_state(SENDS_KEY, sends + [now])
    return code


def check_code(db: Database, code: str) -> tuple[bool, str]:
    """(맞았는지, 틀렸을 때 안내 문구)."""
    stored = db.get_state(CODE_KEY)
    if not stored or time.time() > stored["expires"]:
        return False, "코드가 없거나 만료됐어요. '코드 다시 받기'를 눌러 주세요."
    if hmac.compare_digest(_hash(code.strip()), stored["hash"]):
        db.delete_state(CODE_KEY)  # 한 번 쓴 코드는 다시 못 쓴다
        return True, ""
    stored["attempts"] += 1
    if stored["attempts"] >= MAX_ATTEMPTS:
        db.delete_state(CODE_KEY)
        return False, "여러 번 틀려서 코드를 지웠어요. 새 코드를 받아 주세요."
    db.set_state(CODE_KEY, stored)
    return False, f"코드가 틀렸어요. (남은 기회 {MAX_ATTEMPTS - stored['attempts']}번)"


def create_session(db: Database, hours: float, info: dict) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    sessions = {k: v for k, v in (db.get_state(SESSIONS_KEY) or {}).items() if v["expires"] > now}
    sessions[_hash(token)] = {"expires": now + hours * 3600, "started": now, "created": ts(), **info}
    newest = sorted(sessions.items(), key=lambda kv: kv[1]["expires"])[-MAX_SESSIONS:]
    db.set_state(SESSIONS_KEY, dict(newest))
    return token


def valid_session(db: Database, token: str | None, hours: float) -> bool:
    """로그인한 지 hours 시간이 안 지났는지. 설정을 줄이면 예전에 길게 받은 로그인도 거기에 맞춰 끝난다."""
    if not token:
        return False
    session = (db.get_state(SESSIONS_KEY) or {}).get(_hash(token))
    now = time.time()
    return bool(session and session["expires"] > now and session.get("started", 0) + hours * 3600 > now)


def end_session(db: Database, token: str | None) -> None:
    if not token:
        return
    sessions = db.get_state(SESSIONS_KEY) or {}
    if sessions.pop(_hash(token), None) is not None:
        db.set_state(SESSIONS_KEY, sessions)


# 로그인한 IP는 한동안 쿠키 없이도 들여보낸다 (아이폰 앱 안 브라우저처럼 쿠키를 자꾸 잃는 기기 때문에)

def trust_ip(db: Database, ip: str, hours: float) -> None:
    now = time.time()
    trusted = {k: v for k, v in (db.get_state(TRUSTED_KEY) or {}).items() if v > now}
    trusted[ip] = now + hours * 3600
    db.set_state(TRUSTED_KEY, trusted)


def trusted_ip(db: Database, ip: str) -> bool:
    return (db.get_state(TRUSTED_KEY) or {}).get(ip, 0) > time.time()


def untrust_ip(db: Database, ip: str) -> None:
    trusted = db.get_state(TRUSTED_KEY) or {}
    if trusted.pop(ip, None) is not None:
        db.set_state(TRUSTED_KEY, trusted)
