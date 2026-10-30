"""ADB timing evidence is retained without recording or relaxing validation."""
from types import SimpleNamespace

import pytest

from sawyer_teleop.quest import QuestInput, QuestValidator
from test_quest import packet


def test_failed_sync_retains_five_rtts_and_does_not_replace_accepted_clock(monkeypatch):
    monkeypatch.setattr('sawyer_teleop.quest.shutil.which', lambda _: 'test-adb')
    clock = [1_000_000_000]
    monkeypatch.setattr('sawyer_teleop.quest.ns', lambda: clock[0])
    quest = QuestInput()
    quest.validator = QuestValidator(123, 20_000_000)
    quest.sync_at = clock[0]
    quest.clock_revision = 1
    delay = [190_000_000]  # RTT/2+10 = 105 ms > 100 ms (half the 200 ms input limit).

    def command(*args):
        clock[0] += delay[0]
        return '1.00 0.00'

    quest.command = command
    with pytest.raises(RuntimeError, match='ADB latency too high'):
        quest.sync()
    evidence = quest.diagnostics()
    assert evidence['last_sync']['rtt_ms'] == [190]*5
    assert evidence['last_sync']['candidate_uncertainty_ms'] == 105
    assert not evidence['last_sync']['accepted']
    assert evidence['clock']['offset_ns'] == 123
    assert evidence['clock']['revision'] == 1
    assert evidence['sync_error']
    delay[0] = 20_000_000
    quest.sync()
    assert quest.diagnostics()['sync_error'] is None
    assert quest.diagnostics()['last_sync']['accepted']
    assert quest.validator.uncertainty == 20_000_000


def test_rejected_old_packet_keeps_original_age_for_backlog_diagnosis(monkeypatch):
    monkeypatch.setattr('sawyer_teleop.quest.shutil.which', lambda _: 'test-adb')
    now = 2_000_000_000
    monkeypatch.setattr('sawyer_teleop.quest.ns', lambda: now)
    quest = QuestInput()
    quest.validator = QuestValidator(0, 2_000_000)
    value = quest.validator.parse(packet(), now-5_000_000)
    value['read_completed_ns'] = now-10_000_000
    quest.latest = value
    quest.process = SimpleNamespace(poll=lambda: None)
    quest.sync_at = now
    assert not quest.poll()['valid']
    evidence = quest.diagnostics()
    assert evidence['source_to_read_ms'] == pytest.approx(989.999999)
    assert evidence['read_to_poll_ms'] == 10
    assert value['mapped_ns'] == packet()['sample_ns']


FAKE_SHELL = r'''
import sys
for line in sys.stdin:
    if line.startswith("exit"):
        break
    tag = line.split()[1]
    sys.stdout.write("noise\n%s 123.45 6.70\n" % tag)
    sys.stdout.flush()
'''


def test_persistent_uptime_shell_pairs_replies_and_falls_back(monkeypatch, tmp_path):
    # 2026-10-29: spawning adb.exe per sample measured 84-102 ms on Windows
    # (uncertainty ~60 ms); an open shell measured ~8 ms.
    import sys
    from sawyer_teleop.quest import UptimeShell
    script = tmp_path/'fake_adb.py'
    script.write_text(FAKE_SHELL)
    shell = UptimeShell([sys.executable, '-I', str(script)])
    try:
        assert shell.read() == '123.45 6.70'
        first = shell.process
        shell.lines.put(b'sawyer-sync-1 999.00 0.00\n')  # stale reply from an earlier request
        assert shell.read() == '123.45 6.70'
        assert shell.process is first
    finally:
        shell.close()
    monkeypatch.setattr('sawyer_teleop.quest.shutil.which', lambda _: 'test-adb')
    quest = QuestInput()
    quest.uptime_shell = UptimeShell([sys.executable, '-I', '-c', 'import sys; sys.exit(0)'], timeout=.5)
    quest.command = lambda *args: '5.00 1.00'
    assert quest.uptime() == '5.00 1.00'
    assert quest.uptime_shell.process is None
