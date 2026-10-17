"""May this request edit custom indicator code? The D2 gate (docs/ci0-spike-report.md).

Editing needs every one of:

- **the minimum isolation** (`worker/isolation.py`); below it, no setting turns it on;
- **the setting** `on` (`openchart settings set indicator-editing on`, run on the server's
  machine). `auto`, the default, never allows it (owner's decision after the security
  review, S1): it used to mean "from this machine" (a loopback TCP peer and no proxy
  header), but a local forwarder such as an SSH tunnel makes a remote browser's request
  arrive from 127.0.0.1 with nothing to tell it apart. `off` never allows it either. With
  `on`, any logged-in browser may edit, the phone included;
- **the usual login and same-origin check** (the middleware in `app.py`).

CI-1 shows the result in the catalog; CI-4's write endpoints call `require_editing`.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request

from openchart.indicators.worker.isolation import Isolation
from openchart.settings import INDICATOR_EDITING, SettingsStore

TURN_ON = "openchart settings set indicator-editing on"


@dataclass(frozen=True)
class Editing:
    allowed: bool
    setting: str
    reason: str

    def to_json(self) -> dict[str, object]:
        return {"allowed": self.allowed, "setting": self.setting, "reason": self.reason}


def decide(
    *,
    setting: str,
    isolation: Isolation | None,
    behind_proxy: bool = False,
    peer: str | None = None,
    headers: object = None,
) -> Editing:
    """The gate. Where the request comes from (`peer`, `headers`, `behind_proxy`) no longer
    plays a part (S1); the arguments stay so the tests can show that."""
    if isolation is None:
        return Editing(False, setting, "the indicator worker is still starting")
    if not isolation.minimum_met:
        return Editing(
            False, setting, f"this install does not isolate custom code ({isolation.reason})"
        )
    if setting == "on":
        return Editing(True, setting, "turned on on the server")
    if setting == "off":
        return Editing(
            False, setting, f"turned off on the server; to allow it, run on it: {TURN_ON}"
        )
    return Editing(
        False,
        setting,
        f"off by default ({setting}); to allow it, run on the server's machine: {TURN_ON}",
    )


async def editing_for(
    request: Request, settings: SettingsStore, isolation: Isolation | None
) -> Editing:
    setting = await settings.get(INDICATOR_EDITING, "auto")
    if setting not in ("auto", "on", "off"):
        setting = "off"  # an unreadable value never opens anything
    return decide(setting=setting, isolation=isolation)


async def require_editing(
    request: Request, settings: SettingsStore, isolation: Isolation | None
) -> None:
    """For the editing endpoints (CI-4): 403 with the reason unless allowed."""
    editing = await editing_for(request, settings, isolation)
    if not editing.allowed:
        raise HTTPException(403, f"Editing custom indicators is off: {editing.reason}.")
