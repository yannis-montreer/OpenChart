"""May this request edit custom indicator code? The D2 gate (docs/ci0-spike-report.md).

Editing needs every one of:

- **the minimum isolation** (`worker/isolation.py`); below it, no setting turns it on;
- **the setting** (`openchart settings set indicator-editing ...`): `on`, or `auto` with a
  request from this machine: the TCP peer is loopback (127.0.0.0/8, ::1), the request
  carries none of the headers a proxy adds, and `behind-proxy` is off. So on the owner's
  service, which listens on 0.0.0.0 for Tailscale, editing works in the browser on the PC
  (http://127.0.0.1:8000, the viewer) and not from the phone; in Docker, requests come
  from the bridge, so `auto` is off;
- **the usual login and same-origin check** (the middleware in `app.py`).

CI-1 shows the result in the catalog; CI-4's write endpoints call `require_editing`.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from fastapi import HTTPException, Request

from openchart.indicators.worker.isolation import Isolation
from openchart.settings import BEHIND_PROXY, INDICATOR_EDITING, SettingsStore

# Any of these means a proxy forwarded the request, so its peer says nothing about the
# browser (`tailscale serve` adds Tailscale-User-Login).
FORWARDING_HEADERS = ("forwarded", "x-forwarded-for", "x-real-ip", "tailscale-user-login")


@dataclass(frozen=True)
class Editing:
    allowed: bool
    setting: str
    reason: str

    def to_json(self) -> dict[str, object]:
        return {"allowed": self.allowed, "setting": self.setting, "reason": self.reason}


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def decide(
    *,
    setting: str,
    behind_proxy: bool,
    isolation: Isolation | None,
    peer: str | None,
    headers: dict[str, str] | object,
) -> Editing:
    how = "openchart settings set indicator-editing on|auto|off"
    if isolation is None:
        return Editing(False, setting, "the indicator worker is still starting")
    if not isolation.minimum_met:
        return Editing(
            False, setting, f"this install does not isolate custom code ({isolation.reason})"
        )
    if setting == "off":
        return Editing(False, setting, f"turned off on the server ({how})")
    if setting == "on":
        return Editing(True, setting, "turned on on the server")
    forwarded = [h for h in FORWARDING_HEADERS if headers.get(h) is not None]  # type: ignore[union-attr]
    if behind_proxy:
        return Editing(False, setting, "the server is set as behind a proxy (behind-proxy on)")
    if forwarded:
        return Editing(False, setting, f"the request came through a proxy ({forwarded[0]})")
    if not is_loopback(peer):
        return Editing(
            False,
            setting,
            "only from the browser on the server's own machine (http://127.0.0.1); "
            f"to allow it from elsewhere: {how}",
        )
    return Editing(True, setting, "from the server's own machine")


async def editing_for(
    request: Request, settings: SettingsStore, isolation: Isolation | None
) -> Editing:
    setting = await settings.get(INDICATOR_EDITING, "auto")
    if setting not in ("auto", "on", "off"):
        setting = "off"  # an unreadable value never opens anything
    return decide(
        setting=setting,
        behind_proxy=bool(await settings.get(BEHIND_PROXY, False)),
        isolation=isolation,
        peer=request.client.host if request.client else None,
        headers=request.headers,
    )


async def require_editing(
    request: Request, settings: SettingsStore, isolation: Isolation | None
) -> None:
    """For the editing endpoints (CI-4): 403 with the reason unless allowed."""
    editing = await editing_for(request, settings, isolation)
    if not editing.allowed:
        raise HTTPException(403, f"Editing custom indicators is off: {editing.reason}.")
