"""SSH 로그인 감시: 파이의 SSH 기록(journalctl)을 따라 읽다가 로그인을 텔레그램으로 알린다. 코인 봇 안에서 따로 돈다.

  - 집 밖(공인 IP)에서 로그인 성공 → 바로 알림
  - 로그인 실패 → 같은 IP는 하루에 한 번만 바로 알리고, 그 뒤 실패는 모아서 1시간마다 횟수만 알린다
    (공격 봇이 수백 번 두드려도 알림 폭탄이 안 되게)
  - 집 안(192.168.x.x 등)에서 성공한 로그인은 알리지 않는다 (PC에서 자주 들어오니까)

SSH 기록은 adm 그룹이면 sudo 없이 읽을 수 있다 (라즈베리파이 기본 사용자는 adm 그룹).
"""

import ipaddress
import json
import logging
import re
import selectors
import subprocess
import time

from .db import Database
from .timeutil import now_kst

log = logging.getLogger("mypie.ssh")

CURSOR_KEY = "ssh_cursor"   # 어디까지 읽었는지 (재시작하면 그 뒤부터 이어 읽는다)
SUMMARY_SEC = 3600
ACCEPTED = re.compile(r"Accepted (\S+) for (\S+) from (\S+) port \d+")
FAILED = (re.compile(r"Failed \S+ for (?:invalid user )?(\S+) from (\S+) port \d+"),
          re.compile(r"Invalid user (\S*) from (\S+) port \d+"))
METHODS = {"publickey": "키", "password": "비밀번호", "keyboard-interactive/pam": "비밀번호"}


def is_home(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


class SshWatch:
    def __init__(self, notify, clock=time.monotonic):
        self.notify, self.clock = notify, clock
        self.alerted: dict[str, str] = {}  # IP → 실패를 바로 알린 날
        self.pending: dict[str, int] = {}  # IP → 아직 안 알린 실패 횟수
        self.pending_since: float | None = None

    def handle(self, message: str, day: str) -> None:
        m = ACCEPTED.search(message)
        if m:
            method, user, ip = m.groups()
            if not is_home(ip):
                self.notify(f"🔐 SSH 로그인 · {user} · {ip} ({METHODS.get(method, method)})\n내가 아니면 바로 비밀번호를 바꾸고 알려 주세요.")
            return
        for pattern in FAILED:
            m = pattern.search(message)
            if not m:
                continue
            user, ip = m.groups()
            if self.alerted.get(ip) != day:
                self.alerted[ip] = day
                where = " (집 안)" if is_home(ip) else ""
                self.notify(f"⚠️ SSH 로그인 실패 · {user or '?'} · {ip}{where}\n같은 곳에서 또 실패하면 1시간마다 모아서 알려요.")
            else:
                self.pending[ip] = self.pending.get(ip, 0) + 1
                if self.pending_since is None:
                    self.pending_since = self.clock()
            return

    def summarize(self) -> None:
        """모아 둔 실패가 1시간 넘게 쌓였으면 횟수만 알린다."""
        if not self.pending or self.clock() - self.pending_since < SUMMARY_SEC:
            return
        top = sorted(self.pending.items(), key=lambda x: -x[1])
        self.notify(f"⚠️ 지난 1시간 SSH 로그인 실패 {sum(self.pending.values())}번 더 · "
                    + ", ".join(f"{ip} {n}번" for ip, n in top[:3]) + (" 외" if len(top) > 3 else ""))
        self.pending.clear()
        self.pending_since = None

    def run(self, db_path, stop_event) -> None:
        """SSH 기록을 계속 따라 읽는다 (journalctl -f). 끊기면 잠시 뒤 다시 붙는다."""
        db = Database(db_path)  # 스레드마다 DB 연결을 따로 연다
        while not stop_event.is_set():
            cursor = db.get_state(CURSOR_KEY)
            cmd = ["journalctl", "-t", "sshd", "-t", "sshd-session", "-o", "json", "-f", "--no-pager"]
            cmd += [f"--after-cursor={cursor}"] if cursor else ["-n", "0"]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            except OSError as e:
                log.warning("SSH 기록을 읽지 못했어요: %s", e)
                stop_event.wait(600)
                continue
            selector = selectors.DefaultSelector()
            selector.register(proc.stdout, selectors.EVENT_READ)
            last_save, latest = time.monotonic(), None
            try:
                while not stop_event.is_set() and proc.poll() is None:
                    if selector.select(timeout=30):
                        line = proc.stdout.readline()
                        if not line:
                            break
                        try:
                            entry = json.loads(line)
                        except ValueError:
                            continue
                        latest = entry.get("__CURSOR") or latest
                        self.handle(str(entry.get("MESSAGE", "")), now_kst().date().isoformat())
                    self.summarize()
                    if latest and time.monotonic() - last_save > 60:
                        db.set_state(CURSOR_KEY, latest)
                        last_save = time.monotonic()
            finally:
                if latest:
                    db.set_state(CURSOR_KEY, latest)
                proc.kill()
                selector.close()
            stop_event.wait(30)
