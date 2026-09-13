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

Alongside the watching it reports what the radio already knows -- the rate the
link negotiated and the signal it is receiving.  Neither costs anything to
read and neither is a throughput test: a frame that measured its own bandwidth
would be the noisiest device on the network it is supposed to be looking after.

Nothing here needs root.  ``nmcli`` and ``systemctl`` both ask polkit, and the
rule shipped in ``packaging/`` grants the frame's own user exactly these two
verbs -- which is why the service can keep ``NoNewPrivileges=yes`` and why
there is no ``sudo`` anywhere in this file.  Where the rule is not installed the
commands simply fail, and the watchdog goes on reporting without repairing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import struct
import time
from typing import Any

_log = logging.getLogger(__name__)

#: Flags in /proc/net/route.  RTF_UP | RTF_GATEWAY is "a route out of here".
_RTF_UP = 0x0001
_RTF_GATEWAY = 0x0002

#: The three things a check can conclude, kept apart on purpose.  The watchdog
#: this replaces had only "reachable" and "not reachable", so a network that
#: silently drops ICMP, and a machine with no ``ping`` binary, both read as a
#: permanent outage -- and the escalation ladder then reconnected the radio and
#: restarted NetworkManager every cooldown, for ever, which is exactly the
#: failure the watchdog exists to prevent.
ONLINE = "online"            # a reply came back
OFFLINE = "offline"          # the check ran and nothing came back
UNKNOWN = "unknown"          # nothing has been measured yet
UNMEASURABLE = "unmeasurable"  # no route to test, or no way to test it


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


#: Where the kernel keeps the wireless statistics.  Read rather than shelled
#: out to for the same reason as ``/proc/net/route`` above: one open() on a
#: file the kernel keeps current, no package to install, and it still answers
#: on a machine too busy to fork.
_PROC_WIRELESS = "/proc/net/wireless"


def is_wireless(iface: str) -> bool:
    """Whether this interface is a radio.  A wired frame has no signal to report."""
    return bool(iface) and os.path.isdir(f"/sys/class/net/{iface}/wireless")


def signal_level(iface: str) -> tuple[float, float] | None:
    """``(dBm, link quality)`` for a wireless interface, or None.

    The columns of ``/proc/net/wireless`` are quality, level and noise, and the
    kernel writes them with a trailing full stop.  Anything unexpected in that
    line is treated as "no reading" rather than parsed optimistically: a
    watchdog that reports a signal it invented is worse than one that reports
    nothing.
    """
    try:
        with open(_PROC_WIRELESS, encoding="ascii") as fh:
            for line in fh:
                name, sep, rest = line.partition(":")
                if not sep or name.strip() != iface:
                    continue
                parts = rest.split()
                quality = float(parts[1].rstrip("."))
                level = float(parts[2].rstrip("."))
                return level, quality
    except (OSError, IndexError, ValueError):
        return None
    return None


def signal_percent(dbm: float) -> int:
    """dBm as the 0-100 that every router page shows.

    The scale is the usual one: -50 dBm and better is full marks, -100 is
    nothing.  It is a convention rather than a measurement, which is why the
    dBm figure is published alongside it and not replaced by it.
    """
    return max(0, min(100, int(round(2 * (dbm + 100)))))


async def _output(*argv: str, timeout: float = 10.0) -> str | None:
    """Run a command for its output.  None when it is not installed or fails."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
    except (FileNotFoundError, OSError):
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return None
    if proc.returncode != 0:
        return None
    return (out or b"").decode("utf-8", "replace")


#: The three numbers worth having out of ``iw dev <iface> link``, and the
#: prefix each of them sits behind.
_IW_FIELDS = (("tx bitrate:", "tx_mbit"), ("rx bitrate:", "rx_mbit"),
              ("freq:", "frequency_mhz"), ("SSID:", "ssid"))


def _parse_iw_link(text: str) -> dict[str, Any]:
    """Pull SSID, frequency and the two bitrates out of ``iw dev … link``."""
    found: dict[str, Any] = {}
    for raw in text.splitlines():
        line = raw.strip()
        for prefix, key in _IW_FIELDS:
            if not line.startswith(prefix):
                continue
            value = line[len(prefix):].strip()
            if key == "ssid":
                found[key] = value
            else:
                try:
                    found[key] = float(value.split()[0])
                except (IndexError, ValueError):
                    pass
            break
    return found


async def link_details(iface: str) -> dict[str, Any]:
    """How fast the link underneath negotiated, and how well the frame hears.

    This is not a throughput measurement and must never become one: a frame
    that downloads a file every minute to see how fast its Wi-Fi is would be
    the noisiest device in the house.  What is reported is what the radio
    already knows -- the rate it agreed with the access point, and the signal
    it is receiving -- which is exactly the pair that explains a frame in the
    wrong corner of the flat long before the connectivity sensor ever goes off.

    Everything is best-effort and nothing is required.  The signal comes from
    ``/proc/net/wireless`` and so needs no package at all; the bitrates come
    from ``iw``, and on a system without it they are simply absent, the same
    way a missing ``ping`` makes reachability unmeasurable rather than false.
    A wired frame reports the link speed the kernel has in sysfs and no signal,
    because there is none to report.
    """
    if not iface:
        return {}
    if not is_wireless(iface):
        # -1 is what the kernel writes for a link that is down, and 0 for a
        # driver that does not know; neither is a speed.
        raw = None
        try:
            with open(f"/sys/class/net/{iface}/speed", encoding="ascii") as fh:
                raw = int(fh.read().strip())
        except (OSError, ValueError):
            raw = None
        details: dict[str, Any] = {"link_kind": "wired"}
        if raw is not None and raw > 0:
            details["link_mbit"] = float(raw)
        return details

    details = {"link_kind": "wifi"}
    reading = signal_level(iface)
    if reading is not None:
        level, quality = reading
        details["signal_dbm"] = round(level)
        details["signal_percent"] = signal_percent(level)
        details["link_quality"] = round(quality)
    text = await _output("iw", "dev", iface, "link")
    if text:
        found = _parse_iw_link(text)
        if "tx_mbit" in found:
            details["link_mbit"] = found["tx_mbit"]
        for key in ("rx_mbit", "frequency_mhz", "ssid"):
            if key in found:
                details[key] = found[key]
        freq = found.get("frequency_mhz")
        if freq:
            details["band"] = ("6 GHz" if freq >= 5925 else
                               "5 GHz" if freq >= 4900 else "2.4 GHz")
    return details


async def ping(host: str, *, timeout: float = 3.0, count: int = 2) -> bool | None:
    """One ping run.

    Three answers, not two, because the difference between them decides
    whether the watchdog is allowed to touch anything:

    * ``True``  -- a reply came back.
    * ``False`` -- the run finished and nothing came back.
    * ``None``  -- the check could not be made at all, because there is no
      ``ping`` binary on this system (iputils is not installed, or the image
      is a minimal one).  That is not an outage and must never be repaired:
      the link may be perfectly healthy and simply unmeasurable from here.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-n", "-c", str(count), "-W", str(int(max(1, timeout))), host,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        # No ping at all.  Reported once by the caller, never acted on.
        return None
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
        #: Which of the three states the last check left the watcher in.  What
        #: the log and Home Assistant say, and what ``_may_repair`` reads.
        self.status: str = UNKNOWN
        #: Whether a check has ever succeeded since this process started.
        #: Nothing is repaired before it has: without a known-good reading the
        #: watcher cannot tell a broken link from a network that filters ICMP,
        #: and repairing the second one is how the frame ends up reconnecting
        #: its radio every half hour for the rest of its life.
        self._ever_online = False
        #: So "cannot check" and "never seen the network up" are each said once
        #: rather than on every pass through the loop.
        self._said: set[str] = set()
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
        #: The last reading of the link underneath -- speed, signal, SSID.
        #: Taken on the timer with everything else rather than when somebody
        #: asks, so that the web UI polling its status page cannot make the
        #: frame run ``iw`` several times a second.
        self.link: dict[str, Any] = {}

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

    def _say_once(self, key: str, message: str, *args) -> None:
        """Log something that is true for as long as the condition lasts.

        A frame with no ``ping`` binary would otherwise write the same line
        every interval for months; saying it once, and again after the
        condition clears, is what keeps the journal readable.
        """
        if key not in self._said:
            self._said.add(key)
            _log.warning(message, *args)

    async def check_once(self) -> bool:
        """One check, and whatever it leads to.  Returns reachability."""
        host, iface = self._resolve()
        self.resolved_target = host
        # Before the reachability test rather than after it: a frame that has
        # lost its route still has a radio, and "no route, signal -87 dBm" is
        # the reading that says why.
        self.link = await link_details(iface)
        if not host:
            # No default route at all.  The frame is certainly not on a
            # network, but there is also nothing to ping and nothing useful to
            # reconnect *to*, so this is recorded and never acted on.
            self.status = UNMEASURABLE
            self._say_once("noroute", "no default route; nothing to check")
            self._note(False)
            return False
        self._said.discard("noroute")

        reachable = False
        measurable = False
        for attempt in range(self.attempts):
            answer = await ping(host, timeout=self.timeout)
            if answer is None:
                # No ping binary: the link is not measurable from here.  Not
                # an outage, and above all not something to repair.
                self.status = UNMEASURABLE
                self._say_once(
                    "noping",
                    "cannot check the network: no 'ping' command on this system "
                    "(install iputils-ping); watching is disabled until there is one")
                return bool(self.online)
            measurable = True
            if answer:
                reachable = True
                break
            if attempt + 1 < self.attempts:
                await asyncio.sleep(5.0)
        self._said.discard("noping")

        self._note(reachable)
        if reachable:
            self.status = ONLINE
            self._ever_online = True
            self._said.discard("neverup")
            return True
        self.status = OFFLINE if measurable else UNMEASURABLE
        if not self.repair:
            return False
        if not self._may_repair():
            return False
        if self._consecutive < self.failures:
            return False

        waited = time.monotonic() - self._last_action_at
        if self._last_action_at and waited < self.cooldown:
            _log.info("network still unreachable; %.0fs of cooldown left",
                      self.cooldown - waited)
            return False

        await self._repair(iface)
        return False

    def _may_repair(self) -> bool:
        """Whether the watcher has earned the right to touch the connection.

        One successful check since startup is the whole test, and it is what
        separates "the link went down" from "this network has never answered a
        ping".  On a LAN that filters ICMP the second is permanent, and a
        watchdog that repairs it reconnects the radio for ever without once
        mending anything.
        """
        if self._ever_online:
            return True
        self._say_once(
            "neverup",
            "%s has never answered since startup, so this may be a network that "
            "filters ICMP rather than an outage; watching and reporting only",
            self.resolved_target or "the gateway")
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
            # online / offline / unknown / unmeasurable.  The web UI and
            # ``doctor`` say which of the three it is rather than turning
            # "cannot tell" into "broken".
            "status": self.status,
            "ever_online": self._ever_online,
            "target": self.resolved_target,
            "interface": self._resolve()[1],
            # Flat rather than nested, so Home Assistant can template a single
            # field out of the attributes without reaching through two levels.
            **self.link,
            "for_seconds": round(time.monotonic() - self._changed_at, 1),
            "failed_checks": self._consecutive,
            "outages": self.outages,
            "repairs": self.repairs,
            "repair_enabled": self.repair,
            "last_action": self.last_action,
            "last_outage_at": self.last_outage_at,
            "last_outage_seconds": self.last_outage_seconds,
        }
