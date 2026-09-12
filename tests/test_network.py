"""The network watchdog: what it takes to make it act, and what stops it.

These tests exist because the watchdog this replaces was itself the outage.
It pinged a server on the internet, restarted NetworkManager whenever a single
packet went missing, and the restart guaranteed the next check would fail too --
so one lost packet became a night of five-minute restarts that only a power
cycle ended.  Every test below pins one of the brakes that were missing:
consecutive failures, several pings per check, the cooldown, the gentle rung
before the hard one, and never counting the boot as an outage.
"""

import asyncio

import pytest

from picframe3 import network
from picframe3.network import NetworkWatch

# -- finding something to ping ---------------------------------------------

ROUTE_TABLE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
    "wlan0\t0000FEA9\t00000000\t0001\t0\t0\t1000\t0000FFFF\n"
    "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\n"
)


def test_the_gateway_is_read_from_the_routing_table(tmp_path, monkeypatch):
    table = tmp_path / "route"
    table.write_text(ROUTE_TABLE)
    monkeypatch.setattr("builtins.open", lambda path, **kw: table.open(**kw)
                        if path == "/proc/net/route" else open(path, **kw))
    assert network.default_route() == ("192.168.1.1", "wlan0")


def test_a_link_local_route_is_not_a_default_route(tmp_path, monkeypatch):
    """The 169.254 route has no gateway and is listed first.  Picking it would
    leave the frame pinging 0.0.0.0 for ever."""
    table = tmp_path / "route"
    table.write_text(ROUTE_TABLE.replace(
        "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\n", ""))
    monkeypatch.setattr("builtins.open", lambda path, **kw: table.open(**kw)
                        if path == "/proc/net/route" else open(path, **kw))
    assert network.default_route() is None


def test_no_route_file_at_all_is_not_an_error(monkeypatch):
    def missing(path, **kw):
        raise OSError("no such file")
    monkeypatch.setattr("builtins.open", missing)
    assert network.default_route() is None


# -- the machinery ----------------------------------------------------------

@pytest.fixture
def watch(monkeypatch):
    """A watcher pointed at a fixed address, with the network and the repair
    commands replaced by recorders."""
    w = NetworkWatch(target="192.168.1.1", interface="wlan0", interval=10,
                     failures=3, attempts=1, cooldown=1800, settle=0)
    w.reachable_answers: list[bool] = []
    w.commands: list[tuple[str, ...]] = []

    async def fake_ping(host, **kw):
        return w.reachable_answers.pop(0) if w.reachable_answers else True

    async def fake_run(*argv, **kw):
        w.commands.append(argv)
        return True

    monkeypatch.setattr(network, "ping", fake_ping)
    monkeypatch.setattr(network, "_run", fake_run)
    return w


async def _checks(w, results):
    w.reachable_answers = list(results)
    for _ in results:
        await w.check_once()


async def test_one_failed_check_repairs_nothing(watch):
    await _checks(watch, [False])
    assert watch.commands == []
    assert watch.online is False


async def test_three_failed_checks_reconnect_the_device_first(watch):
    """The gentle rung: one second of downtime on one interface, rather than
    taking every connection on the machine down with NetworkManager."""
    await _checks(watch, [False, False, False])
    assert watch.commands == [("nmcli", "device", "reconnect", "wlan0")]


async def test_the_second_round_restarts_networkmanager(watch):
    watch.cooldown = 0
    await _checks(watch, [False, False, False, False, False, False])
    assert watch.commands[-1] == ("systemctl", "restart", "NetworkManager")


async def test_the_cooldown_is_the_safety_catch(watch):
    """The failure mode that cost a night: repair, fail, repair, fail.  Within
    the cooldown the watcher counts and complains, but keeps its hands off."""
    await _checks(watch, [False] * 12)
    assert len(watch.commands) == 1


async def test_recovery_resets_the_ladder(watch):
    await _checks(watch, [False, False, False])      # reconnect
    await _checks(watch, [True])
    watch._last_action_at = 0.0                      # as if the cooldown passed
    await _checks(watch, [False, False, False])
    assert watch.commands == [("nmcli", "device", "reconnect", "wlan0")] * 2


async def test_an_outage_is_measured_and_remembered(watch):
    await _checks(watch, [False, False])
    assert watch.outages == 1 and watch.online is False
    await _checks(watch, [True])
    assert watch.online is True
    assert watch.last_outage_seconds is not None
    assert watch.snapshot()["outages"] == 1


async def test_repair_off_only_watches(watch):
    watch.repair = False
    await _checks(watch, [False] * 6)
    assert watch.commands == []
    assert watch.snapshot()["repair_enabled"] is False


async def test_disabled_reports_nothing(watch):
    watch.enabled = False
    assert watch.snapshot() == {}


async def test_a_frame_with_no_route_repairs_nothing(watch, monkeypatch):
    """No default route means no gateway to ping -- and nothing sensible to
    reconnect to either.  It is recorded as an outage, not acted on."""
    watch.target = ""
    monkeypatch.setattr(network, "default_route", lambda: None)
    for _ in range(5):
        await watch.check_once()
    assert watch.commands == []
    assert watch.online is False


async def test_several_ping_runs_before_a_check_counts_as_failed(watch, monkeypatch):
    """A single lost packet is not an outage.  One answer in three is enough."""
    watch.attempts = 3
    real_sleep = asyncio.sleep                     # the waits between runs, skipped
    monkeypatch.setattr(asyncio, "sleep", lambda *a, **kw: real_sleep(0))
    watch.reachable_answers = [False, False, True]
    assert await watch.check_once() is True
    assert watch.online is True


async def test_the_first_interval_is_not_checked(watch, monkeypatch):
    """At boot the frame is up long before the network is.  A watchdog that
    counts those seconds starts its life by breaking something."""
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await watch.run()
    assert slept == [watch.interval]
    assert watch.commands == []
