"""Whether the frame can still reach the network, and what to do when it cannot.

A picture frame is the one machine in the house nobody logs into.  When its
Wi-Fi goes, the screen keeps showing the picture it already had, so nothing
looks wrong from the sofa -- meanwhile Home Assistant has lost the frame, and
anything else that depended on it has gone quiet too.  The frame is in the best
position to notice: it knows it stopped hearing from the broker, and it is
sitting right next to the radio that failed.

The dangerous part of a watchdog is the watchdog.  The one this replaces pinged
8.8.8.8 once, and restarted NetworkManager whenever that single packet went
missing -- so a hiccup at the far end of the internet, or the router's nightly
reconnect, tore down the local Wi-Fi.  The restart made the next check fail as
well, which triggered another restart, and the frame spent the night in a loop
that nothing but a power cycle broke.  Every default here is chosen against
that failure mode:

* **The gateway, not the internet.**  What is being tested is the frame's own
  link to the house.  A router that is up but has lost its uplink is not a
  reason to touch the Wi-Fi.
* **Several attempts, several times.**  One lost packet means nothing; three
  failed checks a minute apart, each of them several pings, means something.
* **A cooldown.**  Never intervene more than once every half hour, however bad
  it looks.  An intervention that did not help will not help more often.
* **The gentlest thing first.**  Reconnecting the wireless device is a second
  of downtime; restarting NetworkManager is ten and takes every other
  connection with it.  The second is only tried when the first did not work.
* **It never reboots.**  A frame that is merely offline still shows
  photographs; a frame in a reboot loop shows a rainbow square.

Nothing here needs root.  ``nmcli`` and ``systemctl`` both ask polkit, and the
rule shipped in ``packaging/`` grants the frame's own user exactly these two
verbs -- which is why the service can keep ``NoNewPrivileges=yes`` and why
there is no ``sudo`` anywhere in this file.  Where the rule is not installed the
commands simply fail, and the watchdog goes on reporting without repairing.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from typing import Any

_log = logging.getLogger(__name__)

#: Flags in /proc/net/route.  RTF_UP | RTF_GATEWAY is "a route out of here".
_RTF_UP = 0x0001
_RTF_GATEWAY = 0x0002


def default_route() -> tuple[str, str] | None:
    """The IPv4 gateway and the interface it is reached through, or None.

    Read from ``/proc/net/route`` rather than by running ``ip route``: it is
    one open() on a file the kernel keeps current, it cannot be slowed down by
    a busy system, and it works in the minimal container the tests run in.
    """
    try:
        with open("/proc/net/route", encoding="ascii") as fh:
            next(fh)                                   # header
            for line in fh:
                parts = line.split()
                if len(parts) < 4 or parts[1] != "00000000":
                    continue                           # not the default route
                try:
                    flags = int(parts[3], 16)
                except ValueError:
                    continue
                if not (flags & _RTF_UP and flags & _RTF_GATEWAY):
                    continue
                # Little-endian hex, as the kernel stores it.
                packed = struct.pack("<L", int(parts[2], 16))
                return socket.inet_ntoa(packed), parts[0]
    except (OSError, StopIteration, ValueError):
        return None
    return None


async def ping(host: str, *, timeout: float = 3.0, count: int = 2) -> bool:
    """One ping run.  True when anything came back."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-n", "-c", str(count), "-W", str(int(max(1, timeout))), host,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        return False
    try:
        code = await asyncio.wait_for(proc.wait(), timeout=timeout * count + 5)
    except TimeoutError:
        proc.kill()
        return False
    return code == 0


async def _run(*argv: str, timeout: float = 30.0) -> bool:
    """Run a command, return whether it succeeded.  Failure is logged, never raised."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError as exc:
        _log.warning("%s: %s", argv[0], exc)
        return False
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        _log.warning("%s timed out", " ".join(argv))
        return False
    if proc.returncode != 0:
        _log.warning("%s failed: %s", " ".join(argv),
                     (out or b"").decode("utf-8", "replace").strip() or proc.returncode)
        return False
    return True


class NetworkWatch:
    """Checks reachability on a timer and, if asked to, repairs the link.

    The loop is deliberately boring: check, count, wait.  All the judgement is
    in *when* it is allowed to act -- three consecutive failures, and not
    within the cooldown of the last attempt.
    """

    def __init__(self, *, enabled: bool = True, target: str = "",
                 interval: float = 60.0, failures: int = 3, attempts: int = 3,
                 timeout: float = 3.0, cooldown: float = 1800.0,
                 settle: float = 45.0, repair: bool = True,
                 interface: str = ""):
        self.enabled = enabled
        self.target = target
        self.interval = max(10.0, float(interval))
        self.failures = max(1, int(failures))
        self.attempts = max(1, int(attempts))
        self.timeout = max(1.0, float(timeout))
        self.cooldown = max(0.0, float(cooldown))
        self.settle = max(0.0, float(settle))
        self.repair = repair
        self.interface = interface

        self.online: bool | None = None
        self._consecutive = 0
        self._changed_at = time.monotonic()
        self._last_action_at = 0.0
        self._offline_since: float | None = None
        #: Which rung of the ladder the next repair uses.  Reset on recovery.
        self._step = 0
        self.outages = 0
        self.repairs = 0
        self.last_action = ""
        self.last_outage_at: float | None = None
        self.last_outage_seconds: float | None = None
        self.resolved_target = ""

    # -- what we are pinging ----------------------------------------------
    def _resolve(self) -> tuple[str, str]:
        """The host to ping and the interface to reconnect.

        Resolved on every check rather than once at startup: a frame that moves
        between networks, or comes up before DHCP has finished, would otherwise
        keep pinging an address that stopped being its gateway hours ago.
        """
        if self.target:
            return self.target, self.interface
        route = default_route()
        if route is None:
            return "", self.interface
        gateway, iface = route
        return gateway, self.interface or iface

    # -- the loop ----------------------------------------------------------
    async def run(self) -> None:
        # Nothing is checked for the first interval: at boot the frame is up
        # long before the network is, and a watchdog that counts those seconds
        # as an outage starts its life by breaking something.
        await asyncio.sleep(self.interval)
        while True:
            try:
                # Read on every pass rather than captured at startup, so
                # switching the watcher off in the settings stops it at the
                # next check instead of at the next restart.
                if self.enabled:
                    await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:                      # pragma: no cover - never fatal
                _log.debug("network check failed", exc_info=True)
            await asyncio.sleep(self.interval)

    async def check_once(self) -> bool:
        """One check, and whatever it leads to.  Returns reachability."""
        host, iface = self._resolve()
        self.resolved_target = host
        if not host:
            # No default route at all.  That is a real outage, but there is
            # nothing to ping and nothing useful to reconnect to.
            _log.debug("no default route; nothing to check")
            self._note(False)
            return False

        reachable = False
        for attempt in range(self.attempts):
            if await ping(host, timeout=self.timeout):
                reachable = True
                break
            if attempt + 1 < self.attempts:
                await asyncio.sleep(5.0)

        self._note(reachable)
        if reachable or not self.repair:
            return reachable
        if self._consecutive < self.failures:
            return False

        waited = time.monotonic() - self._last_action_at
        if self._last_action_at and waited < self.cooldown:
            _log.info("network still unreachable; %.0fs of cooldown left",
                      self.cooldown - waited)
            return False

        await self._repair(iface)
        return False

    def _note(self, reachable: bool) -> None:
        """Record the result, and log only the transitions."""
        now = time.monotonic()
        if reachable:
            if self.online is False and self._offline_since is not None:
                self.last_outage_seconds = round(now - self._offline_since, 1)
                _log.warning("network back after %.0fs", self.last_outage_seconds)
            self._consecutive = 0
            self._step = 0
            self._offline_since = None
        else:
            self._consecutive += 1
            if self.online is not False:
                self.outages += 1
                self._offline_since = now
                self.last_outage_at = time.time()
                _log.warning("cannot reach %s", self.resolved_target or "the gateway")
        if self.online is not reachable:
            self._changed_at = now
        self.online = reachable

    async def _repair(self, iface: str) -> None:
        """One rung of the ladder: reconnect the device, else restart the service."""
        self._last_action_at = time.monotonic()
        self.repairs += 1
        self._consecutive = 0                 # start counting towards the next rung

        if self._step == 0 and iface:
            self.last_action = f"reconnect {iface}"
            _log.warning("reconnecting %s", iface)
            await _run("nmcli", "device", "reconnect", iface)
            # Whether or not it worked, the next rung is the harder one: if it
            # worked the check after the cooldown finds the link up and resets
            # the ladder anyway.
            self._step = 1
        else:
            self.last_action = "restart NetworkManager"
            _log.warning("restarting NetworkManager")
            await _run("systemctl", "restart", "NetworkManager", timeout=60.0)
            self._step = 0                    # next time start gently again
        await asyncio.sleep(self.settle)

    # -- what the rest of the frame sees -----------------------------------
    def snapshot(self) -> dict[str, Any]:
        """The current picture, for MQTT, the web UI and ``doctor``."""
        if not self.enabled:
            return {}
        return {
            "online": self.online,
            "target": self.resolved_target,
            "interface": self._resolve()[1],
            "for_seconds": round(time.monotonic() - self._changed_at, 1),
            "failed_checks": self._consecutive,
            "outages": self.outages,
            "repairs": self.repairs,
            "repair_enabled": self.repair,
            "last_action": self.last_action,
            "last_outage_at": self.last_outage_at,
            "last_outage_seconds": self.last_outage_seconds,
        }
