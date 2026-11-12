"""Memory accounting contracts using mocked processes, not large allocations."""

import ctypes
import errno
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from folder_memory import memory_budget as memory


def sample(pid, rss, footprint, status="ok"):
    return {"pid": pid, "rss_bytes": rss, "physical_footprint_bytes": footprint, "source": "fixture", "status": status, "known": status != "unavailable", "exists": status == "ok", "approximate": False}


class MemoryTests(unittest.TestCase):
    def test_public_v0_abi_and_actual_pointer_shape(self):
        self.assertEqual(ctypes.sizeof(memory._RUsageInfoV0), 96)
        self.assertEqual(memory._RUsageInfoV0.ri_resident_size.offset, 64)
        self.assertEqual(memory._RUsageInfoV0.ri_phys_footprint.offset, 72)

        def call(pid, flavor, pointer):
            self.assertEqual((pid, flavor), (123, 0))
            struct = ctypes.cast(pointer, ctypes.POINTER(memory._RUsageInfoV0)).contents
            struct.ri_resident_size = 100
            struct.ri_phys_footprint = 900
            struct.ri_proc_start_abstime = 12
            return 0

        with patch.object(memory, "_libproc", return_value=SimpleNamespace(proc_pid_rusage=call)):
            self.assertEqual(memory._darwin_rusage(123), (100, 900, 12))

    def test_native_measurement_uses_footprint_even_when_rss_differs(self):
        with patch.object(memory.platform, "system", return_value="Darwin"), patch.object(memory, "_darwin_rusage", return_value=(100, 900, 5)):
            result = memory.process_memory(123)
        self.assertEqual(result["rss_bytes"], 100)
        self.assertEqual(result["physical_footprint_bytes"], 900)
        self.assertTrue(result["known"])
        self.assertFalse(result["approximate"])

    def test_native_zero_is_a_known_measurement(self):
        with patch.object(memory.platform, "system", return_value="Darwin"), patch.object(memory, "_darwin_rusage", return_value=(0, 0, 5)):
            result = memory.process_memory(123)
        self.assertTrue(result["known"])
        self.assertEqual(result["physical_footprint_bytes"], 0)
        self.assertEqual(result["status"], "ok")

    def test_native_exited_pid_is_known_zero_not_unavailable(self):
        with patch.object(memory.platform, "system", return_value="Darwin"), patch.object(memory, "_darwin_rusage", side_effect=OSError(errno.ESRCH, "gone")):
            result = memory.process_memory(123)
        self.assertEqual(result["status"], "exited")
        self.assertEqual(result["rss_bytes"], 0)
        self.assertTrue(result["known"])

    def test_fallback_distinguishes_inaccessible_from_zero(self):
        with patch.object(memory.platform, "system", return_value="Darwin"), patch.object(memory, "_darwin_rusage", side_effect=OSError(errno.EPERM, "denied")), patch.object(memory, "_ps_rss", return_value=None), patch.object(memory, "_vmmap_footprint", return_value=None), patch.object(memory, "_exists", return_value=True):
            result = memory.process_memory(123)
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["known"])
        self.assertIsNone(result["rss_bytes"])

    def test_fallback_can_use_current_vmmap_footprint(self):
        with patch.object(memory.platform, "system", return_value="Darwin"), patch.object(memory, "_darwin_rusage", side_effect=OSError(errno.EPERM, "denied")), patch.object(memory, "_ps_rss", return_value=100), patch.object(memory, "_vmmap_footprint", return_value=800):
            result = memory.process_memory(123)
        self.assertEqual(result["physical_footprint_bytes"], 800)
        self.assertTrue(result["approximate"])

    def test_size_units_and_current_not_peak(self):
        cases = {"1024": 1024, "1,024 K": 1024 * 1024, "1.5 MiB": 1572864, "2G": 2 * 1024 ** 3, "2 bytes": 2, "0B": 0, "garbage": None, "1,5G": None, "-4M": None}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(memory._parse_size(text), expected)
        text = "Physical footprint: 1.5G\nPhysical footprint (peak): 8.8G\n"
        self.assertEqual(memory._parse_vmmap(text), int(DecimalString("1.5") * 1024 ** 3))
        self.assertIsNone(memory._parse_vmmap("Physical footprint (peak): 8G"))

    def test_subprocess_fallback_only_uses_explicit_pid_and_timeout(self):
        response = SimpleNamespace(returncode=0, stdout="123\n")
        with patch.object(memory.subprocess, "run", return_value=response) as run:
            self.assertEqual(memory._ps_rss(123), 123 * 1024)
        self.assertEqual(run.call_args.args[0], ["/bin/ps", "-p", "123", "-o", "rss="])
        self.assertEqual(run.call_args.kwargs["timeout"], 2)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        with patch.object(memory, "_run", side_effect=subprocess.TimeoutExpired("vmmap", 2)):
            self.assertIsNone(memory._vmmap_footprint(123))

    def test_unique_processes_use_footprint_charges(self):
        items = {1: sample(1, 200, 1000), 2: sample(2, 500, 2000)}
        with patch.object(memory, "process_memory", side_effect=lambda pid: items[pid]) as observe:
            result = memory.tree_memory([1, 2, 1], budget=4000)
        self.assertEqual(observe.call_count, 2)
        self.assertEqual(result["rss_bytes"], 700)
        self.assertEqual(result["charged_bytes"], 3000)
        self.assertTrue(result["within_budget"])
        self.assertTrue(result["footprint_complete"])

    def test_rss_only_does_not_claim_a_complete_budget_pass(self):
        with patch.object(memory, "process_memory", return_value=sample(1, 100, None)):
            result = memory.tree_memory([1], budget=1000)
        self.assertEqual(result["charged_bytes"], 100)
        self.assertEqual(result["rss_fallback_pids"], [1])
        self.assertIsNone(result["within_budget"])
        self.assertEqual(result["budget_status"], "unknown")

    def test_unknown_process_is_not_a_zero_sized_budget_pass(self):
        with patch.object(memory, "process_memory", return_value=sample(1, None, None, "unavailable")):
            result = memory.tree_memory([1], budget=1000)
        self.assertEqual(result["unknown_pids"], [1])
        self.assertFalse(result["measurement_complete"])
        self.assertIsNone(result["within_budget"])

    def test_known_exited_process_does_not_make_the_measurement_incomplete(self):
        with patch.object(memory, "process_memory", return_value=sample(1, 0, None, "exited")):
            result = memory.tree_memory([1])
        self.assertEqual(result["exited_pids"], [1])
        self.assertTrue(result["measurement_complete"])
        self.assertIsNone(result["within_budget"])
        self.assertEqual(result["budget_status"], "not_configured")
        self.assertEqual(result["charged_bytes"], 0)

    def test_an_observed_overage_fails_even_with_another_unknown_pid(self):
        items = {1: sample(1, 1, 1001), 2: sample(2, None, None, "unavailable")}
        with patch.object(memory, "process_memory", side_effect=lambda pid: items[pid]):
            result = memory.tree_memory([1, 2], budget=1000)
        self.assertFalse(result["within_budget"])
        self.assertEqual(result["budget_status"], "exceeded")

    def test_invalid_or_unbounded_pid_requests_are_rejected(self):
        for pid in [0, -1, True, "123", 2 ** 40]:
            with self.subTest(pid=pid), self.assertRaises(ValueError):
                memory.process_memory(pid)
        with self.assertRaises(ValueError):
            memory.tree_memory(range(1, 66))
        with self.assertRaises(ValueError):
            memory.tree_memory([1], budget=0)


def DecimalString(value):
    from decimal import Decimal
    return Decimal(value)


if __name__ == "__main__":
    unittest.main()
