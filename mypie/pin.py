"""웹페이지에서 전략을 바꿀 때 쓰는 PIN. 원문은 저장하지 않고 해시만 DB에 남긴다.

PIN 만들기/바꾸기 (파이에서): python3 -m mypie.pin
"""

import getpass
import hashlib
import hmac
import os
import sys

STATE_KEY = "web_pin"
ITERATIONS = 100_000


def _digest(pin: str, salt: bytes, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations).hex()


def make(pin: str) -> dict:
    salt = os.urandom(16)
    return {"salt": salt.hex(), "hash": _digest(pin, salt, ITERATIONS), "iterations": ITERATIONS}


def verify(pin: str, stored: dict) -> bool:
    expected = _digest(pin, bytes.fromhex(stored["salt"]), int(stored["iterations"]))
    return hmac.compare_digest(expected, stored["hash"])


def main() -> None:
    from .config import load_config
    from .db import Database

    pin = getpass.getpass("새 PIN (4자리 이상, 입력해도 화면에 안 보여요): ")
    if len(pin) < 4:
        sys.exit("4자리 이상으로 정해 주세요.")
    if getpass.getpass("한 번 더 입력: ") != pin:
        sys.exit("두 번 입력한 PIN이 달라요. 다시 해 주세요.")
    db = Database(load_config()["general"]["db_path"])
    db.set_state(STATE_KEY, make(pin))
    db.close()
    print("PIN을 저장했어요. 웹페이지에서 전략을 바꿀 때 입력하세요.")


if __name__ == "__main__":
    main()
