"""업비트 API 키 관리. 키는 파이의 ~/My_Pie/.env 에만 저장되고 GitHub에는 올라가지 않는다.

키 등록/변경 (파이에서): python3 -m mypie.keys
키 상태·만료일 확인:      python3 -m mypie.keys --check
"""

import argparse
import getpass
import os
import sys
from datetime import datetime
from pathlib import Path

from .config import ROOT

ENV_PATH = ROOT / ".env"
ACCESS = "UPBIT_ACCESS_KEY"
SECRET = "UPBIT_SECRET_KEY"


def load_keys(path: Path = ENV_PATH) -> tuple[str, str] | None:
    """(access_key, secret_key). 없으면 None."""
    if not path.exists():
        return None
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    if values.get(ACCESS) and values.get(SECRET):
        return values[ACCESS], values[SECRET]
    return None


def save_keys(access: str, secret: str, path: Path = ENV_PATH) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # 나만 읽을 수 있게
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"{ACCESS}={access}\n{SECRET}={secret}\n")
    os.chmod(path, 0o600)


def days_left(expire_at: str) -> int:
    """만료까지 남은 날 수."""
    expires = datetime.fromisoformat(expire_at)
    return (expires - datetime.now(expires.tzinfo)).days


def check() -> None:
    from .upbit import UpbitClient, UpbitError

    keys = load_keys()
    if not keys:
        sys.exit("등록된 키가 없어요. python3 -m mypie.keys 로 등록하세요.")
    try:
        expire_at = UpbitClient(*keys).key_expiry()
    except UpbitError as e:
        sys.exit(f"업비트 연결 실패: {e}")
    if not expire_at:
        sys.exit("키는 연결되지만 만료일을 찾지 못했어요. 업비트 Open API 관리에서 확인하세요.")
    print(f"키 연결 정상 · 만료 {expire_at[:10]} (앞으로 {days_left(expire_at)}일)")


def main() -> None:
    parser = argparse.ArgumentParser(description="업비트 API 키 등록/확인")
    parser.add_argument("--check", action="store_true", help="등록된 키가 되는지와 만료일만 확인")
    if parser.parse_args().check:
        check()
        return

    from .upbit import UpbitClient, UpbitError

    print("업비트 Open API 키를 등록해요. 붙여넣어도 화면에는 안 보여요.")
    access = getpass.getpass("Access Key: ").strip()
    secret = getpass.getpass("Secret Key: ").strip()
    if not access or not secret:
        sys.exit("키가 비어 있어요. 다시 해 주세요.")

    print("업비트에 연결해 보는 중...")
    try:
        accounts = UpbitClient(access, secret).accounts()
    except UpbitError as e:
        sys.exit(f"연결 실패: {e}\n키를 다시 확인하고, 업비트에 등록한 허용 IP가 지금 집 IP와 같은지 확인하세요.")

    save_keys(access, secret)
    print(f"연결 성공! 키를 {ENV_PATH} 에 저장했어요 (나만 읽기 가능).")
    for a in accounts:
        if float(a["balance"]) + float(a["locked"]) > 0:
            print(f"  {a['currency']}: {float(a['balance']):,.8g} (주문 중 {float(a['locked']):,.8g})")
    print("이제 웹페이지에서 계좌를 '실거래'로 바꾸면 돼요.")


if __name__ == "__main__":
    main()
