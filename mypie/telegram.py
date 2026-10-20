"""텔레그램 연결: 웹페이지 로그인 코드와 매매 알림을 보낸다. 표준 라이브러리만 쓴다.

연결하기 (파이에서):  python3 -m mypie.telegram           봇 토큰 입력 → 내 채팅 자동 연결 → 테스트 메시지
테스트 메시지:        python3 -m mypie.telegram --test
비상용 로그인 코드:   python3 -m mypie.telegram --code    텔레그램이 안 될 때 화면에 로그인 코드를 출력
연결 끊기:            python3 -m mypie.telegram --off     로그인 없이 웹페이지가 열리게 된다
"""

import argparse
import getpass
import json
import logging
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque

from .keys import read_env, write_env

log = logging.getLogger("mypie.telegram")

API = "https://api.telegram.org/bot{token}/{method}"
TOKEN = "TELEGRAM_BOT_TOKEN"
CHAT = "TELEGRAM_CHAT_ID"


class TelegramError(Exception):
    pass


def load() -> tuple[str, str] | None:
    """(봇 토큰, 내 채팅 번호). 연결 안 했으면 None."""
    env = read_env()
    return (env[TOKEN], env[CHAT]) if env.get(TOKEN) and env.get(CHAT) else None


def call(token: str, method: str, params: dict | None = None, timeout: float = 8):
    request = urllib.request.Request(API.format(token=token, method=method), data=json.dumps(params or {}).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # 오류 메시지에 토큰이 든 주소를 넣지 않는다
        try:
            body = json.loads(e.read().decode("utf-8"))
        except ValueError:
            raise TelegramError(f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TelegramError(f"텔레그램 통신 오류 ({getattr(e, 'reason', e)})") from None
    if not body.get("ok"):
        raise TelegramError(body.get("description", "알 수 없는 오류"))
    return body["result"]


def send(text: str, creds: tuple[str, str] | None = None) -> None:
    creds = creds or load()
    if not creds:
        raise TelegramError("텔레그램이 연결되지 않았어요 (python3 -m mypie.telegram)")
    token, chat = creds
    call(token, "sendMessage", {"chat_id": chat, "text": text, "disable_web_page_preview": True})


class Notifier:
    """매매·오류 알림을 텔레그램으로 보낸다. 따로 도는 스레드에서 보내서 매매를 늦추지 않는다."""

    MAX_PER_HOUR = 40  # 알림 폭탄 방지

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.queue: queue.Queue[str] = queue.Queue(maxsize=100)
        self.sent: deque[float] = deque()
        threading.Thread(target=self._worker, name="telegram", daemon=True).start()

    def __call__(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            self.queue.put_nowait(text)
        except queue.Full:
            pass

    def _worker(self) -> None:
        while True:
            text = self.queue.get()
            now = time.monotonic()
            while self.sent and now - self.sent[0] > 3600:
                self.sent.popleft()
            if len(self.sent) >= self.MAX_PER_HOUR:
                continue
            creds = load()
            if not creds:
                continue
            try:
                send(text, creds)
                self.sent.append(now)
            except TelegramError as e:
                log.warning("텔레그램 알림 실패: %s", e)


def setup() -> None:
    print("텔레그램 봇을 연결해요.\n"
          "  1) 텔레그램에서 @BotFather 를 찾아 /newbot 을 보내고, 이름을 정하면 '봇 토큰'을 줘요.\n"
          "  2) 그 토큰을 아래에 붙여넣으세요 (화면에는 안 보여요).")
    token = getpass.getpass("봇 토큰: ").strip()
    if not token:
        sys.exit("토큰이 비어 있어요. 다시 해 주세요.")
    try:
        me = call(token, "getMe")
        call(token, "deleteWebhook")
        offset = max([u["update_id"] for u in call(token, "getUpdates")] or [0]) + 1  # 예전 메시지는 무시
    except TelegramError as e:
        sys.exit(f"토큰이 맞지 않거나 연결할 수 없어요: {e}")
    print(f"\n좋아요! 이제 텔레그램에서 @{me['username']} 을(를) 찾아서 아무 메시지나 보내 주세요 (예: /start).\n"
          "기다리는 중... (2분 안에)")
    chat = None
    deadline = time.monotonic() + 120
    while chat is None and time.monotonic() < deadline:
        for update in call(token, "getUpdates", {"offset": offset, "timeout": 10}, timeout=15):
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            if message.get("chat", {}).get("type") == "private":
                chat = message["chat"]["id"]
    if chat is None:
        sys.exit("메시지를 못 받았어요. 봇에게 메시지를 보낸 뒤 다시 실행해 주세요.")
    write_env({TOKEN: token, CHAT: str(chat)})
    send("🥧 My_Pie와 연결됐어요!\n이제 웹페이지 로그인 코드와 매매 알림이 여기로 와요.", (token, str(chat)))
    print("연결 완료! 텔레그램에 테스트 메시지가 갔어요.\n"
          "이제 웹페이지에 들어가면 로그인 코드가 텔레그램으로 와요. (봇을 재시작하면 매매 알림도 와요: "
          "systemctl --user restart mypie-bot)")


def main() -> None:
    parser = argparse.ArgumentParser(description="텔레그램 연결 (로그인 코드·매매 알림)")
    parser.add_argument("--test", action="store_true", help="테스트 메시지 보내기")
    parser.add_argument("--code", action="store_true", help="비상용: 로그인 코드를 화면에 출력")
    parser.add_argument("--off", action="store_true", help="텔레그램 연결 끊기 (로그인 없이 열림)")
    args = parser.parse_args()
    if args.test:
        try:
            send("🥧 My_Pie 테스트 메시지예요.")
            print("보냈어요. 텔레그램을 확인하세요.")
        except TelegramError as e:
            sys.exit(f"실패: {e}")
    elif args.code:
        from .auth import issue_code
        from .config import load_config
        from .db import Database
        cfg = load_config()
        code = issue_code(Database(cfg["general"]["db_path"]), int(cfg["web"]["code_digits"]), force=True)
        print(f"로그인 코드: {code}  (5분 안에 웹페이지에 입력하세요)")
    elif args.off:
        write_env({TOKEN: "", CHAT: ""})
        print("텔레그램 연결을 끊었어요. 이제 웹페이지가 로그인 없이 열려요.")
    else:
        setup()


if __name__ == "__main__":
    main()
