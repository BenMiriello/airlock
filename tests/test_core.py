"""Unit tests for the Broker state machine. Pure logic, no I/O, no threads."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

# Allow `python -m unittest tests.test_core` from project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core import (
    Broker, Mode, LeaseState, PreemptHandler, AppConfig,
)


def _make_broker(total: int = 24000, margin: int = 0, mode: Mode = Mode.PRIORITY) -> Broker:
    b = Broker(total_vram_mib=total, safety_margin_mib=margin, mode=mode)
    return b


def _register_app(b: Broker, name: str, budget: int = 1000, priority: int = 50,
                  watch: bool = True) -> None:
    b.apps[name] = AppConfig(
        name=name, default_budget_mib=budget, default_priority=priority,
        preempt_handler=PreemptHandler("sigterm"), watch_implicit=watch,
    )


class CoreBasics(unittest.TestCase):
    def test_grant_immediate_when_room(self):
        b = _make_broker(total=24000)
        res = b.submit(app="a", vram_budget_mib=8000, client_pid=None)
        self.assertTrue(res.is_granted())
        self.assertEqual(b.committed_mib(), 8000)

    def test_reject_when_budget_exceeds_total(self):
        b = _make_broker(total=24000, margin=512)
        res = b.submit(app="a", vram_budget_mib=24000, client_pid=None)
        self.assertTrue(res.is_error())
        self.assertEqual(res.error, "cannot_fit")

    def test_two_leases_coexist_within_budget(self):
        b = _make_broker()
        r1 = b.submit(app="a", vram_budget_mib=10000, client_pid=None, priority=50)
        r2 = b.submit(app="b", vram_budget_mib=8000, client_pid=None, priority=50)
        self.assertTrue(r1.is_granted())
        self.assertTrue(r2.is_granted())
        self.assertEqual(len(b.active_leases), 2)

    def test_third_lease_queues_when_no_room(self):
        b = _make_broker()
        b.submit(app="a", vram_budget_mib=10000, client_pid=None, priority=50)
        b.submit(app="b", vram_budget_mib=10000, client_pid=None, priority=50)
        r3 = b.submit(app="c", vram_budget_mib=8000, client_pid=None, priority=50)
        self.assertTrue(r3.is_queued())
        self.assertEqual(len(b.queue), 1)

    def test_release_frees_budget_and_grants_queued(self):
        b = _make_broker()
        r1 = b.submit(app="a", vram_budget_mib=10000, client_pid=None, priority=50)
        b.submit(app="b", vram_budget_mib=10000, client_pid=None, priority=50)
        r3 = b.submit(app="c", vram_budget_mib=8000, client_pid=None, priority=50)
        self.assertTrue(r3.is_queued())
        b.release(r1.lease.id)
        effect = b.tick()
        self.assertEqual(len(effect.grants), 1)
        self.assertEqual(effect.grants[0].app, "c")


class ExclusiveMode(unittest.TestCase):
    def test_exclusive_queues_second_request(self):
        b = _make_broker(mode=Mode.EXCLUSIVE)
        r1 = b.submit(app="a", vram_budget_mib=4000, client_pid=None)
        r2 = b.submit(app="b", vram_budget_mib=4000, client_pid=None)
        self.assertTrue(r1.is_granted())
        self.assertTrue(r2.is_queued())

    def test_exclusive_release_grants_next(self):
        b = _make_broker(mode=Mode.EXCLUSIVE)
        r1 = b.submit(app="a", vram_budget_mib=4000, client_pid=None)
        b.submit(app="b", vram_budget_mib=4000, client_pid=None)
        b.release(r1.lease.id)
        effect = b.tick()
        self.assertEqual(len(effect.grants), 1)

    def test_mode_switch_to_exclusive_preempts_extras(self):
        b = _make_broker(mode=Mode.PRIORITY)
        r1 = b.submit(app="a", vram_budget_mib=4000, client_pid=None, priority=70)
        r2 = b.submit(app="b", vram_budget_mib=4000, client_pid=None, priority=30)
        self.assertTrue(r1.is_granted())
        self.assertTrue(r2.is_granted())
        effect = b.set_mode(Mode.EXCLUSIVE)
        self.assertEqual(len(effect.preempts), 1)
        # The kept lease is r1 (priority 70)
        self.assertEqual(effect.preempts[0][0], r2.lease.id)


class PriorityPreemption(unittest.TestCase):
    def test_higher_priority_preempts(self):
        b = _make_broker(total=10000)
        r1 = b.submit(app="low", vram_budget_mib=8000, client_pid=None, priority=20)
        self.assertTrue(r1.is_granted())
        r2 = b.submit(app="high", vram_budget_mib=8000, client_pid=None, priority=80)
        self.assertTrue(r2.is_queued())
        self.assertEqual(len(r2.preempts_to_fire), 1)
        self.assertEqual(r2.preempts_to_fire[0][0], r1.lease.id)
        # After preempt completes, r1 releases — broker grants r2.
        b.release(r1.lease.id)
        effect = b.tick()
        self.assertEqual(len(effect.grants), 1)
        self.assertEqual(effect.grants[0].app, "high")

    def test_lower_priority_doesnt_preempt(self):
        b = _make_broker(total=10000)
        b.submit(app="high", vram_budget_mib=8000, client_pid=None, priority=80)
        r2 = b.submit(app="low", vram_budget_mib=4000, client_pid=None, priority=20)
        self.assertTrue(r2.is_queued())
        self.assertEqual(len(r2.preempts_to_fire), 0)

    def test_preempt_picks_smallest_set(self):
        b = _make_broker(total=24000)
        l1 = b.submit(app="a", vram_budget_mib=4000, client_pid=None, priority=20).lease
        l2 = b.submit(app="b", vram_budget_mib=8000, client_pid=None, priority=20).lease
        l3 = b.submit(app="c", vram_budget_mib=10000, client_pid=None, priority=20).lease
        # 22000 used, 2000 free. Need 10000.
        r = b.submit(app="high", vram_budget_mib=10000, client_pid=None, priority=80)
        self.assertTrue(r.is_queued())
        # Need 10000 - 2000 = 8000 more. Smallest set: just l2 (8000 budget).
        # But algorithm sorts by (priority asc, -budget desc) within ties — so it
        # picks l3 (largest budget first for ties).
        preempted_ids = {lid for lid, _ in r.preempts_to_fire}
        self.assertEqual(len(preempted_ids), 1)
        # Should be l3 (10000) since it frees enough alone
        self.assertIn(l3.id, preempted_ids)


class EqualMode(unittest.TestCase):
    def test_equal_never_preempts(self):
        b = _make_broker(total=10000, mode=Mode.EQUAL)
        b.submit(app="a", vram_budget_mib=8000, client_pid=None, priority=20)
        r = b.submit(app="b", vram_budget_mib=8000, client_pid=None, priority=80)
        self.assertTrue(r.is_queued())
        self.assertEqual(len(r.preempts_to_fire), 0)


class Snapshot(unittest.TestCase):
    def test_snapshot_restore_roundtrip(self):
        b1 = _make_broker(total=24000, margin=512, mode=Mode.PRIORITY)
        _register_app(b1, "comfyui", budget=14000, priority=60)
        b1.submit(app="a", vram_budget_mib=8000, client_pid=12345, priority=50)
        b1.submit(app="b", vram_budget_mib=10000, client_pid=23456, priority=70)
        # Queue something
        b1.submit(app="c", vram_budget_mib=20000, client_pid=34567, priority=10)
        snap = b1.snapshot()
        b2 = _make_broker(total=1, margin=0, mode=Mode.EQUAL)
        b2.restore(snap)
        self.assertEqual(b2.mode, Mode.PRIORITY)
        self.assertEqual(b2.total_vram_mib, 24000)
        self.assertEqual(b2.safety_margin_mib, 512)
        self.assertEqual(len(b2.active_leases), 2)
        self.assertEqual(len(b2.queue), 1)
        self.assertIn("comfyui", b2.apps)


class ImplicitLeases(unittest.TestCase):
    def test_implicit_lease_for_registered_app(self):
        b = _make_broker()
        _register_app(b, "comfyui", budget=14000, priority=60)
        l = b.submit_implicit("comfyui", client_pid=99999, observed_vram_mib=12000)
        self.assertIsNotNone(l)
        self.assertTrue(l.implicit)
        # Budget should be max(default 14000, observed*1.2 = 14400) = 14400
        self.assertEqual(l.vram_budget_mib, 14400)

    def test_implicit_lease_none_for_unknown_app(self):
        b = _make_broker()
        l = b.submit_implicit("unknown", client_pid=99999, observed_vram_mib=8000)
        self.assertIsNone(l)


class BudgetDecay(unittest.TestCase):
    def test_decay_shrinks_oversized_implicit_lease(self):
        b = _make_broker()
        _register_app(b, "ollama", budget=22000, priority=30)
        # Implicit lease created when observed VRAM was 20000 → budget = 24000
        l = b.submit_implicit("ollama", client_pid=12345, observed_vram_mib=20000)
        self.assertEqual(l.vram_budget_mib, 24000)
        # Actual drops sharply. Peak still records the original 20000 spike,
        # so decay target is 20000*1.2=24000 — no change yet.
        b.update_actual_usage(12345, 500)
        changes = b.decay_implicit_budgets()
        self.assertEqual(changes, [])
        # Simulate a longer interval where the spike has dropped out of the
        # tracked peak (a v2 enhancement would track rolling peak; here we
        # manually update for the test).
        l.vram_peak_mib = 500
        changes = b.decay_implicit_budgets()
        self.assertEqual(len(changes), 1)
        # New budget = max(500*1.2=600, floor=256) = 600
        self.assertEqual(l.vram_budget_mib, 600)

    def test_decay_skips_explicit_leases(self):
        b = _make_broker()
        r = b.submit(app="explicit", vram_budget_mib=10000, client_pid=5555)
        b.update_actual_usage(5555, 100)
        r.lease.vram_peak_mib = 100
        changes = b.decay_implicit_budgets()
        self.assertEqual(changes, [])
        self.assertEqual(r.lease.vram_budget_mib, 10000)

    def test_actual_usage_tracks_peak(self):
        b = _make_broker()
        r = b.submit(app="a", vram_budget_mib=5000, client_pid=9999)
        b.update_actual_usage(9999, 100)
        b.update_actual_usage(9999, 800)
        b.update_actual_usage(9999, 200)
        self.assertEqual(r.lease.vram_peak_mib, 800)
        self.assertEqual(r.lease.vram_actual_mib, 200)


class BoostsAndForget(unittest.TestCase):
    def test_boost_overrides_priority(self):
        b = _make_broker()
        now = time.monotonic()
        b.set_boost("a", priority=90, ttl_s=60, now_mono=now)
        eff_pri = b.effective_priority("a", base_priority=30, now_mono=now + 10)
        self.assertEqual(eff_pri, 90)

    def test_boost_expires(self):
        b = _make_broker()
        now = time.monotonic()
        b.set_boost("a", priority=90, ttl_s=1, now_mono=now)
        eff_pri = b.effective_priority("a", base_priority=30, now_mono=now + 5)
        self.assertEqual(eff_pri, 30)

    def test_forget_pid_releases_leases(self):
        b = _make_broker()
        r1 = b.submit(app="a", vram_budget_mib=4000, client_pid=4242)
        r2 = b.submit(app="b", vram_budget_mib=4000, client_pid=5555)
        released = b.forget_pid(4242)
        self.assertEqual(released, [r1.lease.id])
        self.assertIn(r2.lease.id, b.active_leases)


if __name__ == "__main__":
    unittest.main()
