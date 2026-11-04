import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("probe_platform", Path(__file__).parents[1] / "scripts" / "probe_platform.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class ProbeTests(unittest.TestCase):
    def test_mac_hypervisor_availability_never_grants_trust(self):
        calls = []
        def runner(command):
            calls.append(command)
            return "1"
        result = probe.probe_platform(system="Darwin", machine="arm64", runner=runner)
        self.assertTrue(result["observations"]["apple_hypervisor_api_available"]["value"])
        self.assertFalse(result["trust"]["confidential_eligibility"])
        self.assertEqual(result["trust"]["attestation"], "not_performed")
        self.assertEqual(calls, [["/usr/sbin/sysctl", "-n", "kern.hv_support"]])

    def test_failed_query_is_unknown_not_unsupported(self):
        result = probe.probe_platform(system="Darwin", machine="arm64", runner=lambda _: None)
        self.assertEqual(result["observations"]["apple_hypervisor_api_available"]["status"], "unknown")

    def test_linux_guest_nodes_are_only_observations(self):
        result = probe.probe_platform(system="Linux", machine="x86_64", path_exists=lambda _: True)
        self.assertTrue(result["observations"]["sev_guest_device"]["value"])
        self.assertFalse(result["trust"]["confidential_eligibility"])
        self.assertNotIn("android", result)

    def test_android_feature_does_not_prove_protected_vm(self):
        calls = []
        def runner(command):
            calls.append(command)
            return "true" if "has-feature" in command else "15"
        result = probe.probe_platform(system="Linux", machine="aarch64", android=True,
            runner=runner, path_exists=lambda _: False, which=lambda _: "/example/adb")
        self.assertEqual(result["android"]["observations"]["avf_system_feature"]["value"], "true")
        self.assertFalse(result["trust"]["confidential_eligibility"])
        forbidden = ["devices", "root", "flash", "install", "ro.serialno", "ro.boot.serialno"]
        self.assertFalse(any(word in command for command in calls for word in forbidden))

    def test_android_not_installed_does_not_invoke_runner(self):
        def unexpected(_):
            raise AssertionError("No adb queries expected")
        result = probe.probe_platform(system="Other", machine="unknown", android=True,
            runner=unexpected, which=lambda _: None)
        self.assertFalse(result["android"]["available"])


if __name__ == "__main__":
    unittest.main()
