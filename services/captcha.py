"""滑塊人機驗證：華為雲雲商店「行為驗證碼」（杭州怜花科技 / jumdata）。

流程：瀏覽器載入服務商腳本並完成滑塊 → 取得 ticket + randstr → 隨登入請求提交
→ 後端以 API 網關 APP 認證（SDK-HMAC-SHA256）調用校驗接口，code 200 才放行。

captchaAppId / captchaAppSecret 需服務商開通；未填寫時整個功能自動停用，
登入流程與原來完全相同。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, unquote, urlencode, urlsplit

import httpx

import config.settings as settings

logger = logging.getLogger(__name__)

CAPTCHA_VERIFY_URL = "https://lhtyxwyzm.apistore.huaweicloud.com/captcha/action"
# 雲商店連結的服務商接入文檔指定的公開入口；須動態載入，不可本地快取。
# https://file.jumdata.com/support/captcha/captcha_action.pdf
CAPTCHA_SCRIPT_URL = "https://api-js.jumdata.com/action-captcha/action-captcha.min.js"
CAPTCHA_TIMEOUT = httpx.Timeout(connect=5.0, read=8.0, write=5.0, pool=5.0)

# 票據本身無效：一律拒絕。
REJECT_CODES = {
    301: "人機驗證未通過，請重新滑動驗證。",
    302: "人機驗證已過期，請重新滑動驗證。",
    400: "人機驗證資料不完整，請重新滑動驗證。",
    607: "目前網絡被限制登入，請稍後再試。",
    609: "驗證過於頻繁，請稍後再試。",
}
# 已配置驗證時，供應商故障也必須拒絕，不能退回成繞過驗證。

_ALGORITHM = "SDK-HMAC-SHA256"
_DATE_FORMAT = "%Y%m%dT%H%M%SZ"


@dataclass(frozen=True)
class CaptchaResult:
    ok: bool
    message: str = ""
    code: Optional[int] = None
    bypassed: bool = False


def _setting(name: str) -> str:
    return str(getattr(settings, name, "") or "").strip()


def captcha_app_id() -> str:
    return _setting("CAPTCHA_APP_ID")


def is_captcha_enabled() -> bool:
    return all(_setting(name) for name in (
        "CAPTCHA_APP_ID", "CAPTCHA_APP_ID_SECRET", "CAPTCHA_APP_KEY", "CAPTCHA_APP_SECRET",
    ))


def public_captcha_config() -> dict:
    """瀏覽器只需要公開的 captchaAppId 與腳本地址。"""
    if not is_captcha_enabled():
        return {"enabled": False}
    return {"enabled": True, "app_id": captcha_app_id(), "script_url": CAPTCHA_SCRIPT_URL}


def captcha_sign(app_id: str, app_secret: str, ticket: str, randstr: str) -> str:
    """服務商文檔：sha256(captchaAppId + captchaAppSecret + ticket + randstr)。"""
    return hashlib.sha256((app_id + app_secret + ticket + randstr).encode("utf-8")).hexdigest()


def _encode_path(path: str) -> str:
    encoded = "/".join(quote(part, safe="~") for part in unquote(path or "/").split("/"))
    return encoded if encoded.endswith("/") else encoded + "/"


def sign_apig_request(method: str, url: str, headers: dict, body: bytes,
                      app_key: str, app_secret: str, now: Optional[datetime] = None) -> dict:
    """華為雲 API 網關 APP 認證簽名（與雲商店示例的 apig-sdk Signer 一致）。"""
    parts = urlsplit(url)
    signed = dict(headers)
    signed["X-Sdk-Date"] = (now or datetime.now(timezone.utc)).strftime(_DATE_FORMAT)
    signed["Host"] = parts.netloc
    lowered = {key.lower(): str(value).strip() for key, value in signed.items()}
    signed_headers = sorted(lowered)
    query = "&".join(sorted(
        f"{quote(k, safe='~')}={quote(v, safe='~')}"
        for k, _, v in (item.partition("=") for item in parts.query.split("&") if item)
    ))
    canonical = "\n".join([
        method.upper(),
        _encode_path(parts.path),
        query,
        "".join(f"{name}:{lowered[name]}\n" for name in signed_headers),
        ";".join(signed_headers),
        hashlib.sha256(body).hexdigest(),
    ])
    string_to_sign = "\n".join([
        _ALGORITHM, signed["X-Sdk-Date"], hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(app_secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    signed["Authorization"] = (f"{_ALGORITHM} Access={app_key}, "
                               f"SignedHeaders={';'.join(signed_headers)}, Signature={signature}")
    return signed


async def verify_captcha(ticket: Optional[str], randstr: Optional[str],
                         *, client: Optional[httpx.AsyncClient] = None) -> CaptchaResult:
    """校驗一次滑塊票據。未啟用時直接放行，保持原有登入行為。

    服務商文檔中來源 IP 為選填。刻意不傳：同一用戶連服務商走 IPv6、連本站走 IPv4
    （或經代理）時會被判為「來源 IP 不匹配」，導致無法登入。
    """
    if not is_captcha_enabled():
        return CaptchaResult(ok=True, bypassed=True)
    ticket, randstr = (ticket or "").strip(), (randstr or "").strip()
    if not ticket or not randstr or len(ticket) > 4096 or len(randstr) > 256:
        return CaptchaResult(ok=False, message="請先完成人機驗證。")

    app_id = captcha_app_id()
    form = {
        "captchaAppId": app_id,
        "ticket": ticket,
        "randstr": randstr,
        "captchaSign": captcha_sign(app_id, _setting("CAPTCHA_APP_ID_SECRET"), ticket, randstr),
    }
    body = urlencode(form).encode("utf-8")
    headers = sign_apig_request(
        "POST", CAPTCHA_VERIFY_URL, {"Content-Type": "application/x-www-form-urlencoded"}, body,
        _setting("CAPTCHA_APP_KEY"), _setting("CAPTCHA_APP_SECRET"),
    )
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=CAPTCHA_TIMEOUT) as owned:
                response = await owned.post(CAPTCHA_VERIFY_URL, content=body, headers=headers)
        else:
            response = await client.post(CAPTCHA_VERIFY_URL, content=body, headers=headers)
        data = response.json()
        code = int(data.get("code"))
    except Exception as exc:  # 網絡、網關鑑權失敗或非 JSON 回覆
        logger.warning("人機驗證服務暫時不可用：%s", type(exc).__name__)
        return CaptchaResult(ok=False, message="人機驗證服務暫時不可用，請稍後重試。")

    if code == 200:
        return CaptchaResult(ok=True, code=code)
    if code in REJECT_CODES:
        return CaptchaResult(ok=False, code=code, message=REJECT_CODES[code])
    logger.warning("人機驗證服務拒絕或未就緒 code=%s", code)
    return CaptchaResult(ok=False, code=code, message="人機驗證服務暫時不可用，請稍後重試。")

