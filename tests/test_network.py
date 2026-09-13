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
from picframe3.network import ping as real_ping

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
    await _checks(watch, [True])                     # the link was up once
    await _checks(watch, [False, False, False])
    assert watch.commands == [("nmcli", "device", "reconnect", "wlan0")]


async def test_the_second_round_restarts_networkmanager(watch):
    watch.cooldown = 0
    await _checks(watch, [True])
    await _checks(watch, [False, False, False, False, False, False])
    assert watch.commands[-1] == ("systemctl", "restart", "NetworkManager")


async def test_the_cooldown_is_the_safety_catch(watch):
    """The failure mode that cost a night: repair, fail, repair, fail.  Within
    the cooldown the watcher counts and complains, but keeps its hands off."""
    await _checks(watch, [True])
    await _checks(watch, [False] * 12)
    assert len(watch.commands) == 1


async def test_recovery_resets_the_ladder(watch):
    await _checks(watch, [True])
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


# -- knowing what it cannot know -------------------------------------------
# The three states the watchdog has to keep apart.  Collapsing them into one
# boolean is what turned the previous watchdog into the outage it was meant to
# prevent: a LAN that filters ICMP, and a machine without a `ping` binary, both
# read as "offline for ever", and the ladder then reconnected the radio and
# restarted NetworkManager every cooldown, all night, every night.

async def test_nothing_is_repaired_before_the_link_has_ever_been_up(watch):
    """The ICMP-filtered LAN.  Without a single successful check there is no
    evidence the link is broken rather than merely unmeasurable, so the
    watchdog reports and keeps its hands off -- however long it goes on."""
    await _checks(watch, [False] * 12)
    assert watch.commands == []
    assert watch.online is False
    assert watch.status == network.OFFLINE
    assert watch.snapshot()["ever_online"] is False


async def test_one_good_check_earns_the_right_to_repair(watch):
    await _checks(watch, [True])
    assert watch.status == network.ONLINE
    await _checks(watch, [False, False, False])
    assert watch.commands == [("nmcli", "device", "reconnect", "wlan0")]


async def test_a_missing_ping_binary_is_cannot_check_not_an_outage(watch, monkeypatch):
    """iputils is not installed.  That says nothing about the network, so the
    frame must not count it as an outage and must never repair on it."""
    async def no_ping(*a, **kw):
        raise FileNotFoundError("ping")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_ping)
    monkeypatch.setattr(network, "ping", real_ping)      # the real one, again
    for _ in range(6):
        await watch.check_once()
    assert watch.commands == []
    assert watch.status == network.UNMEASURABLE
    assert watch.outages == 0, "a check that could not be made is not an outage"


async def test_the_missing_ping_binary_is_said_once_not_every_minute(
        watch, monkeypatch, caplog):
    async def no_ping(*a, **kw):
        raise FileNotFoundError("ping")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_ping)
    monkeypatch.setattr(network, "ping", real_ping)
    with caplog.at_level("WARNING"):
        for _ in range(10):
            await watch.check_once()
    said = [r for r in caplog.records if "no 'ping' command" in r.getMessage()]
    assert len(said) == 1, "one line, not one per check — this is the SD card"


async def test_no_default_route_is_unmeasurable_and_never_repaired(watch, monkeypatch):
    watch.target = ""
    monkeypatch.setattr(network, "default_route", lambda: None)
    for _ in range(6):
        await watch.check_once()
    assert watch.commands == []
    assert watch.status == network.UNMEASURABLE


# -- what the radio already knows ------------------------------------------

PROC_WIRELESS = (
    "Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n"
    " face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n"
    " wlan0: 0000   58.  -52.  -256        0      0      0      0      0        0\n"
)

IW_LINK = """Connected to 3c:a6:2f:11:22:33 (on wlan0)
\tSSID: Fnaten
\tfreq: 5180
\tRX: 812345 bytes (4321 packets)
\tTX: 91234 bytes (876 packets)
\tsignal: -52 dBm
\trx bitrate: 234.0 MBit/s VHT-MCS 9 80MHz short GI VHT-NSS 2
\ttx bitrate: 195.0 MBit/s VHT-MCS 8 80MHz short GI VHT-NSS 2
"""


def _fake_proc(tmp_path, monkeypatch, text=PROC_WIRELESS):
    stats = tmp_path / "wireless"
    stats.write_text(text)
    monkeypatch.setattr("builtins.open", lambda path, **kw: stats.open(**kw)
                        if path == "/proc/net/wireless" else open(path, **kw))


def test_the_signal_is_read_from_proc(tmp_path, monkeypatch):
    _fake_proc(tmp_path, monkeypatch)
    assert network.signal_level("wlan0") == (-52.0, 58.0)


def test_another_interface_is_not_mistaken_for_this_one(tmp_path, monkeypatch):
    _fake_proc(tmp_path, monkeypatch)
    assert network.signal_level("wlan1") is None


def test_a_wired_frame_has_no_signal_to_read(tmp_path, monkeypatch):
    """Not an error and not a zero: a cable has no dBm."""
    _fake_proc(tmp_path, monkeypatch, "Inter-| sta-|   Quality\n face | tus |\n")
    assert network.signal_level("eth0") is None


def test_a_garbled_line_reports_nothing_rather_than_inventing_a_reading(
        tmp_path, monkeypatch):
    _fake_proc(tmp_path, monkeypatch, PROC_WIRELESS.replace("-52.", "n/a"))
    assert network.signal_level("wlan0") is None


@pytest.mark.parametrize("dbm,percent", [(-40, 100), (-50, 100), (-75, 50),
                                         (-100, 0), (-120, 0)])
def test_dbm_becomes_the_percentage_every_router_page_shows(dbm, percent):
    assert network.signal_percent(dbm) == percent


def test_the_bitrates_come_out_of_iw(tmp_path, monkeypatch):
    found = network._parse_iw_link(IW_LINK)
    assert found["tx_mbit"] == 195.0
    assert found["rx_mbit"] == 234.0
    assert found["frequency_mhz"] == 5180.0
    assert found["ssid"] == "Fnaten"


def test_iw_saying_nothing_useful_is_not_an_error():
    assert network._parse_iw_link("Not connected.") == {}


@pytest.mark.asyncio
async def test_a_frame_without_iw_still_reports_its_signal(tmp_path, monkeypatch):
    """The missing binary is the ``ping`` case again: what can be measured is
    reported, what cannot is absent rather than nought."""
    _fake_proc(tmp_path, monkeypatch)
    monkeypatch.setattr(network, "is_wireless", lambda iface: True)
    monkeypatch.setattr(network, "_output", _never_installed)
    details = await network.link_details("wlan0")
    assert details["signal_dbm"] == -52
    assert details["link_kind"] == "wifi"
    assert "link_mbit" not in details


@pytest.mark.asyncio
async def test_speed_signal_and_band_reach_the_snapshot(tmp_path, monkeypatch):
    _fake_proc(tmp_path, monkeypatch)
    monkeypatch.setattr(network, "is_wireless", lambda iface: True)

    async def iw(*argv, **kw):
        return IW_LINK

    monkeypatch.setattr(network, "_output", iw)
    details = await network.link_details("wlan0")
    assert details["link_mbit"] == 195.0
    assert details["band"] == "5 GHz"
    assert details["signal_percent"] == 96


@pytest.mark.asyncio
async def test_no_interface_means_no_reading_and_no_subprocess(monkeypatch):
    monkeypatch.setattr(network, "_output", _never_called)
    assert await network.link_details("") == {}


async def _never_installed(*argv, **kw):
    return None


async def _never_called(*argv, **kw):
    raise AssertionError("nothing should have been run")
