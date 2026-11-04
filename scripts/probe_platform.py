#!/usr/bin/env python3
"""Read-only observations for planning; this is never an attestation verifier.

No device identifiers, hostname, credentials, kernel logs, installations, VM
creation, or privilege changes are collected. Android checks are opt-in and
require an already-authorized adb connection.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import shutil
import subprocess


def run_readonly(args: list[str]) -> str | None:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=4, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or len(result.stdout) > 1024:
        return None
    return result.stdout.strip()


def observation(value, meaning: str) -> dict:
    return {"value": value, "status": "unknown" if value is None else "observed", "meaning": meaning}


def probe_platform(*, android=False, runner=run_readonly, system=None, machine=None,
                   path_exists=None, which=None) -> dict:
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    path_exists = Path.exists if path_exists is None else path_exists
    which = shutil.which if which is None else which
    result = {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "system": system,
        "architecture": machine,
        "observations": {},
        "trust": {
            "attestation": "not_performed",
            "host_confidentiality": "not_established",
            "confidential_eligibility": False,
        },
        "limitations": [
            "Local OS observations can be forged by that OS and do not establish a trusted worker.",
            "Device-node presence or a virtualization feature does not prove protected execution.",
            "No protected guest, model, toolchain, secure boot chain, or endpoint key was verified.",
        ],
    }
    observations = result["observations"]
    if system == "Darwin":
        value = runner(["/usr/sbin/sysctl", "-n", "kern.hv_support"])
        observations["apple_hypervisor_api_available"] = observation(
            True if value == "1" else False if value == "0" else None,
            "Apple's documented ordinary Hypervisor API availability query; not host-confidential execution.",
        )
    elif system == "Linux":
        for name, node in [("kvm_device", "/dev/kvm"), ("sev_guest_device", "/dev/sev-guest"),
                           ("tdx_guest_device", "/dev/tdx_guest")]:
            observations[name] = observation(bool(path_exists(Path(node))),
                "Device node exists only; access, hardware evidence, guest identity, and policy remain unverified.")
    else:
        observations["platform_backend"] = observation(None, "No platform-specific probe is implemented.")

    if android:
        adb = which("adb")
        result["android"] = {"requested": True, "available": bool(adb), "observations": {}}
        if adb:
            # Do not invoke `adb devices`: it prints private device identifiers.
            queries = {
                "android_release": ["getprop", "ro.build.version.release"],
                "android_sdk": ["getprop", "ro.build.version.sdk"],
                "avf_system_feature": ["pm", "has-feature", "android.software.virtualization_framework"],
                "verified_boot_state": ["getprop", "ro.boot.verifiedbootstate"],
                "bootloader_device_state": ["getprop", "ro.boot.vbmeta.device_state"],
            }
            for name, command in queries.items():
                value = runner([adb, "shell", *command])
                # Empty or unexpected output is unknown, not a capability verdict.
                value = value if value and len(value) < 100 and "\n" not in value else None
                result["android"]["observations"][name] = observation(value,
                    "Untrusted preliminary OS report; protected-VM capability and real attestation require separate checks.")
        result["limitations"].append(
            "Android CAPABILITY_PROTECTED_VM and RKP-backed VM attestation were not requested or verified.")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--android", action="store_true",
                        help="Also query an already-authorized adb device; no identifiers or mutations")
    args = parser.parse_args()
    print(json.dumps(probe_platform(android=args.android), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
