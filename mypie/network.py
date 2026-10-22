"""집 공인 IP 확인. 업비트 키는 등록한 IP에서만 쓸 수 있어서, IP가 바뀌면 실거래 주문이 막힌다."""

import ipaddress
import urllib.request

IP_SERVICES = ("https://checkip.amazonaws.com", "https://api.ipify.org")


def public_ip(timeout: float = 5) -> str | None:
    """지금 인터넷에 보이는 집 IP. 확인 못 하면 None."""
    for url in IP_SERVICES:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                text = response.read(64).decode("ascii", "replace").strip()
            return str(ipaddress.ip_address(text))
        except (OSError, ValueError):
            continue
    return None


KEY_ERRORS = ("expired_access_key", "invalid_access_key")  # 키가 만료됐거나 지워져서 더 못 쓴다


def is_key_error(error: BaseException | None) -> bool:
    """업비트가 'API 키가 만료됐다/없는 키다'라고 거절한 오류인지 (원인 오류까지 따라가며 확인)."""
    while error is not None:
        if (getattr(error, "name", "") or "") in KEY_ERRORS:
            return True
        error = error.__cause__
    return False


def is_ip_error(error: BaseException | None) -> bool:
    """업비트가 '허용되지 않은 IP'라고 거절한 오류인지 (원인 오류까지 따라가며 확인)."""
    while error is not None:
        if "no_authorization_i" in (getattr(error, "name", "") or ""):  # no_authorization_i_p
            return True
        error = error.__cause__
    return False
