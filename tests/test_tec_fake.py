"""Tests for C6 ``transport/fake.py``: emulation, framing, recording, factory (Task 8).

Requirements: FR-13, FR-13.1, FR-13.3, FR-13.4, NFR-4. The scripted actions (FR-13.2) are in
``test_tec_fake_actions.py`` (split for the 500-line limit).

Every command is framed by the real C3 ``encode_command`` and every reply is decoded by the real
``ReplyAssembler``/``parse_reply`` (helpers in ``tec_fake_helpers``). Commands the allowlist
refuses (unknown mnemonic, address 7) are built as raw reports.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from inheco_tec_api.protocol import commands as c
from inheco_tec_api.protocol.commands import ALLOWED_MNEMONICS, Command
from inheco_tec_api.protocol.framing import encode_command
from inheco_tec_api.transport.base import DeviceIOError, DeviceNotFound, HidTransport
from inheco_tec_api.transport.fake import (
    FakeMainboard,
    FakeSlot,
    FakeTecDevice,
    FakeTransportFactory,
    WriteRecord,
    encode_reply,
)
from tec_fake_helpers import (
    TickClock,
    opened,
    raw_command_reports,
    read_texts,
    send,
    send_one,
    send_raw,
    write_reports,
)

# --- FR-13.1: framed STT round trip through the real decoder ----------------------------------


def test_fr13_1_framed_stt_updates_target_and_reply_decodes() -> None:
    dev = opened()
    reply = send_one(dev, c.stt(1, 370))
    assert (reply.address, reply.mnemonic, reply.error_char, reply.data) == ("1", "stt", "0", "")
    assert dev.slots[1].target == 370
    assert dev.slots[2].target == 0  # other slot untouched
    assert send_one(dev, c.rtt(1)).data == "0370"


def test_fr13_1_stt_with_bad_param_is_wrong_parameter() -> None:
    dev = opened()
    # 2000 is above the wire bound; encode_command refuses it, so send it raw.
    reply = send_raw(dev, "1STT2000")[0]
    assert reply.error_char == "5"
    assert dev.slots[1].target == 0


def test_fr13_1_multi_report_command_is_reassembled() -> None:
    dev = opened()
    reports = encode_command(c.stt(2, 1999))  # "2STT1999" + CRC = 9 bytes -> 2 reports
    assert len(reports) == 2
    write_reports(dev, reports[:1])
    assert dev.pending_reports == 0  # nothing until the last report arrives
    assert dev.writes == []
    write_reports(dev, reports[1:])
    assert read_texts(dev) == ["2stt0"]
    assert dev.slots[2].target == 1999
    assert dev.writes[-1].command_str == "2STT1999"
    assert dev.writes[-1].reports == tuple(reports)


def test_fr13_1_multi_report_reply_decodes() -> None:
    dev = opened()
    dev.mainboard.firmware[4] = "(c) A LONG COPYRIGHT STRING FOR THE TEST"
    write_reports(dev, encode_command(c.rfv(4)))
    assert dev.pending_reports > 1  # reply spans several 8-byte payloads
    texts = read_texts(dev)
    assert texts == ["0rfv0(c) A LONG COPYRIGHT STRING FOR THE TEST"]


def test_fr13_1_encode_reply_frames_short_and_long_text() -> None:
    short = encode_reply("1stt0")
    assert len(short) == 1 and len(short[0]) == 8 and short[0][7] != 0x23
    long = encode_reply("0rfv0ABCDEFGHIJ")  # 15 chars + CRC = 16 bytes -> 7 + 7 + 2
    assert len(long) == 3
    assert all(len(p) == 8 for p in long)
    assert long[0][7] == 0x23 and long[1][7] == 0x23 and long[2][7] != 0x23


def test_fr13_1_bad_command_crc_gets_protocol_violation_and_no_state_change() -> None:
    dev = opened()
    good = raw_command_reports("1STT370")[0]
    bad_crc = good[len("1STT370") + 1] ^ 0x01
    replies = send_raw(dev, "1STT370", crc=bad_crc)
    assert [(r.name, r.error_char) for r in replies] == [("1stt", "1")]
    assert dev.slots[1].target == 0


def test_fr13_1_report_with_nonzero_report_id_gets_no_reply() -> None:
    dev = opened()
    report = bytearray(encode_command(c.rtt(1))[0])
    report[0] = 0x01
    dev.write_report(bytes(report))
    assert dev.pending_reports == 0


# --- FR-13.1: every allowlisted mnemonic is handled ---------------------------------------------

ALL_COMMANDS: list[tuple[Command, str, str]] = [
    (c.stt(1, 370), "1stt", ""),
    (c.ate(1, True), "1ate", ""),
    (c.rat(1, 1), "1rat", "0250"),
    (c.rtt(1), "1rtt", "0000"),
    (c.rlt(1), "1rlt", "40"),
    (c.rmt1(1), "1rmt", "700"),
    (c.ran(1), "0ran", "7000179"),
    (c.rhe(1), "1rhe", "2"),
    (c.rtd(0), "0rtd", "1"),
    (c.rfv(1), "0rfv", "APP V2.00"),
    (c.rhv(0), "0rhv", "200"),
    (c.rdc1(), "0rdc", ""),
    (c.aeo(), "0aeo", ""),
]


def test_fr13_1_command_table_covers_the_whole_allowlist() -> None:
    assert {cmd.mnemonic for cmd, _, _ in ALL_COMMANDS} == set(ALLOWED_MNEMONICS)
    assert len(ALLOWED_MNEMONICS) == 13


@pytest.mark.parametrize(("cmd", "echo", "data"), ALL_COMMANDS, ids=lambda v: str(v))
def test_fr13_1_each_allowlisted_mnemonic_succeeds(cmd: Command, echo: str, data: str) -> None:
    dev = opened()
    reply = send_one(dev, cmd)
    assert reply.name == echo
    assert reply.error_char == "0"
    assert reply.data == data
    assert dev.commands == [cmd.text]


def test_fr13_1_ate_switches_control_and_heater_state() -> None:
    dev = opened()
    dev.slots[1].actual = 250
    send_one(dev, c.stt(1, 40))
    send_one(dev, c.ate(1, True))
    assert dev.slots[1].enabled is True
    assert send_one(dev, c.rhe(1)).data == "1"  # actual above target -> cooling
    dev.slots[1].actual = 30
    assert send_one(dev, c.rhe(1)).data == "0"  # at or below target -> heating
    send_one(dev, c.ate(1, False))
    assert dev.slots[1].enabled is False
    assert send_one(dev, c.rhe(1)).data == "2"


def test_fr13_1_rat_ramps_toward_target_without_overshoot() -> None:
    dev = opened(FakeTecDevice(slots={1: FakeSlot(actual=250, ramp_step=100)}))
    send_one(dev, c.stt(1, 40))
    assert send_one(dev, c.rat(1, 3)).data == "0250"  # disabled: no ramp
    send_one(dev, c.ate(1, True))
    values = [send_one(dev, c.rat(1, 1)).data for _ in range(4)]
    assert values == ["0150", "0050", "0040", "0040"]


def test_fr13_1_rat_without_ramp_keeps_actual() -> None:
    dev = opened()
    send_one(dev, c.stt(1, 40))
    send_one(dev, c.ate(1, True))
    assert {send_one(dev, c.rat(1, 2)).data for _ in range(3)} == {"0250"}


def test_fr13_1_slot_model_values_are_reported() -> None:
    slot = FakeSlot(device_type=4, article_number="7000166", rlt=-100, rmt1=1100)
    dev = opened(FakeTecDevice(slots={3: slot}))
    assert send_one(dev, c.rtd(3)).data == "4"
    assert send_one(dev, c.ran(3)).data == "7000166"
    assert send_one(dev, c.rlt(3)).data == "-100"
    assert send_one(dev, c.rmt1(3)).data == "1100"


def test_fr13_1_rtd_of_empty_slot_is_128_and_controller_type_from_mainboard() -> None:
    dev = opened(FakeTecDevice(mainboard=FakeMainboard(controller_type=3)))
    assert send_one(dev, c.rtd(0)).data == "3"
    assert send_one(dev, c.rtd(5)).data == "128"


@pytest.mark.parametrize("sel", range(5))
def test_fr13_1_rfv_selectors(sel: int) -> None:
    dev = opened()
    assert send_one(dev, c.rfv(sel)).data == dev.mainboard.firmware[sel]


@pytest.mark.parametrize("sel", range(9))
def test_fr13_1_rhv_selectors(sel: int) -> None:
    dev = opened()
    dev.mainboard.sensor_values[sel] = f"V{sel}"
    assert send_one(dev, c.rhv(sel)).data == f"V{sel}"


def test_fr13_1_aeo_sets_emergency_off_and_disables_every_slot() -> None:
    dev = opened()
    for slot in (1, 2):
        send_one(dev, c.ate(slot, True))
    send_one(dev, c.aeo())
    assert dev.mainboard.emergency_off is True
    assert not any(slot.enabled for slot in dev.slots.values())


# --- FR-13.1: unknown slot -> 7, unknown mnemonic -> 4 -----------------------------------------


def test_fr13_1_unknown_slot_via_encode_command_is_7() -> None:
    dev = opened()  # slots 1 and 2 only
    reply = send_one(dev, c.stt(3, 370))
    assert (reply.name, reply.error_char, reply.data) == ("3stt", "7", "")


@pytest.mark.parametrize("text", ["7RTT", "9STT370", "0RAT1"])
def test_fr13_1_unknown_slot_raw_is_7(text: str) -> None:
    dev = opened()
    [reply] = send_raw(dev, text)
    assert reply.error_char == "7"
    assert reply.name == text[:4].lower()


@pytest.mark.parametrize("text", ["1XYZ", "0ABC1", "1RHV0"])
def test_fr13_1_unknown_mnemonic_raw_is_4(text: str) -> None:
    dev = opened()
    [reply] = send_raw(dev, text)
    assert reply.error_char == "4"
    assert reply.name == text[:4].lower()
    assert dev.commands == [text]


def test_fr13_1_unknown_mnemonic_on_unknown_slot_is_4() -> None:
    dev = opened()
    [reply] = send_raw(dev, "5XYZ")
    assert reply.error_char == "4"


# --- FR-13.3: recording -------------------------------------------------------------------------


def test_fr13_3_write_log_records_command_strings_with_increasing_timestamps() -> None:
    clock = TickClock()
    dev = opened(FakeTecDevice(clock=clock))
    cmds = [c.stt(1, 370), c.ate(1, True), c.rat(1, 1), c.rdc1(), c.rfv(4)]
    for cmd in cmds:
        send(dev, cmd)
    assert dev.commands == [cmd.text for cmd in cmds]
    assert all(isinstance(rec, WriteRecord) for rec in dev.writes)
    stamps = [rec.monotonic_s for rec in dev.writes]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
    assert [rec.reports for rec in dev.writes] == [tuple(encode_command(cmd)) for cmd in cmds]


def test_fr13_3_default_clock_timestamps_are_monotonic() -> None:
    dev = opened()
    for _ in range(5):
        send(dev, c.rtt(1))
    stamps = [rec.monotonic_s for rec in dev.writes]
    assert stamps == sorted(stamps)


def test_fr13_3_open_count_and_is_open() -> None:
    dev = FakeTecDevice()
    assert (dev.is_open, dev.open_count) == (False, 0)
    dev.open()
    dev.open()  # second open while open is a no-op
    assert (dev.is_open, dev.open_count) == (True, 1)
    dev.close()
    assert dev.is_open is False
    dev.open()
    assert dev.open_count == 2


def test_fr13_3_state_and_log_survive_close_and_reopen() -> None:
    dev = opened()
    send_one(dev, c.stt(1, 370))
    send_one(dev, c.ate(1, True))
    dev.close()
    dev.open()
    assert send_one(dev, c.rtt(1)).data == "0370"
    assert send_one(dev, c.rhe(1)).data == "0"  # enabled, actual 25.0 <= 37.0: heating
    assert dev.commands == ["1STT370", "1ATE1", "1RTT", "1RHE"]


def test_fr13_3_close_drops_queued_input() -> None:
    dev = opened()
    write_reports(dev, encode_command(c.rtt(1)))
    assert dev.pending_reports > 0
    dev.close()
    dev.open()
    assert dev.read_report(10) is None


def test_fr13_3_factory_returns_same_device_and_state_survives_second_create() -> None:
    factory = FakeTransportFactory()
    first = factory.create(0)
    assert isinstance(first, FakeTecDevice)
    first.open()
    write_reports(first, encode_command(c.stt(2, 55)))
    first.close()
    second = factory.create(0)
    assert second is first
    second.open()
    assert send_one(first, c.rtt(2)).data == "0055"
    assert factory.device(0) is first
    assert factory.create(1) is not first


def test_fr13_3_factory_uses_supplied_devices_and_rejects_mismatched_ids() -> None:
    dev = FakeTecDevice(3)
    assert FakeTransportFactory({3: dev}).create(3) is dev
    with pytest.raises(ValueError):
        FakeTransportFactory({2: FakeTecDevice(3)})


def test_fr13_3_factory_default_device_uses_factory_clock() -> None:
    clock = TickClock(start=0.0, step=1.0)
    dev = FakeTransportFactory(clock=clock).device(4)
    dev.open()
    send(dev, c.rtt(1))
    assert dev.controller_id == 4
    assert dev.writes[0].monotonic_s == 1.0


def test_fr13_3_controller_id_out_of_range_rejected() -> None:
    with pytest.raises(ValueError):
        FakeTecDevice(8)


# --- HidTransport contract (FR-13; carry-forward T7-C1) ----------------------------------------


@pytest.mark.parametrize("timeout", [100.0, 0.5, True, False, "100", None])
def test_fr13_read_report_rejects_non_int_timeout(timeout: object) -> None:
    dev = opened()
    write_reports(dev, encode_command(c.rtt(1)))
    queued = dev.pending_reports
    with pytest.raises(TypeError):
        dev.read_report(timeout)  # type: ignore[arg-type]
    assert dev.pending_reports == queued  # nothing consumed


def test_fr13_read_report_returns_none_on_empty_queue() -> None:
    dev = opened()
    assert dev.read_report(0) is None


def test_fr13_io_on_closed_device_raises_device_io_error() -> None:
    dev = FakeTecDevice()
    report = encode_command(c.rtt(1))[0]
    with pytest.raises(DeviceIOError):
        dev.write_report(report)
    with pytest.raises(DeviceIOError):
        dev.read_report(10)
    with pytest.raises(DeviceIOError):
        dev.flush_input()


def test_fr13_write_report_of_wrong_length_raises_value_error() -> None:
    dev = opened()
    with pytest.raises(ValueError):
        dev.write_report(b"\x00" * 8)
    assert dev.writes == []


def test_fr13_flush_input_discards_and_counts() -> None:
    dev = opened()
    dev.mainboard.firmware[4] = "X" * 30
    write_reports(dev, encode_command(c.rfv(4)))
    queued = dev.pending_reports
    assert queued > 1
    assert dev.flush_input() == queued
    assert dev.read_report(10) is None
    assert dev.flush_input() == 0


def test_fr13_absent_device_open_raises_device_not_found() -> None:
    dev = FakeTecDevice()
    dev.present = False
    with pytest.raises(DeviceNotFound):
        dev.open()
    assert dev.open_count == 0


def test_fr13_fake_satisfies_transport_protocol_shape() -> None:
    dev: HidTransport = FakeTecDevice()
    for name in ("open", "close", "write_report", "read_report", "flush_input"):
        assert callable(getattr(dev, name))


# --- FR-13.4: is_fake flag ----------------------------------------------------------------------


def test_fr13_4_is_fake_true_on_device_and_factory() -> None:
    assert FakeTecDevice.is_fake is True
    assert FakeTecDevice().is_fake is True
    assert FakeTransportFactory.is_fake is True
    assert FakeTransportFactory().is_fake is True


# --- NFR-4: the fake never imports hid -----------------------------------------------------------


@pytest.mark.parametrize("module", ["fake.py", "fake_actions.py"])
def test_nfr4_fake_modules_do_not_import_hid(package_dir: Path, module: str) -> None:
    tree = ast.parse((package_dir / "transport" / module).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level > 0:
            assert node.module != "hidapi_transport", "fake must not pull in the hid transport"
    assert "hid" not in imported
    assert "inheco_tec_api" in imported or module == "fake_actions.py"


def test_nfr4_fake_runs_with_hid_import_blocked() -> None:
    script = textwrap.dedent(
        """
        import sys

        class Blocker:
            def find_spec(self, name, path=None, target=None):
                if name == "hid" or name.startswith("hid."):
                    raise ImportError("hid import blocked by test")
                return None

        sys.meta_path.insert(0, Blocker())
        from inheco_tec_api.protocol import commands as c
        from inheco_tec_api.protocol.framing import ReplyAssembler, encode_command, parse_reply
        from inheco_tec_api.transport.fake import FakeTransportFactory

        dev = FakeTransportFactory().create(0)
        dev.open()
        for report in encode_command(c.stt(1, 370)):
            dev.write_report(report)
        asm = ReplyAssembler()
        asm.feed(dev.read_report(100))
        assert parse_reply(asm.finish()).name == "1stt"
        assert "hid" not in sys.modules
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"
