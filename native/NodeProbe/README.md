# Native network observation client

`NodeProbe` is a dependency-free Swift command-line application using Foundation URLSession. It demonstrates a compiled native application communicating with the marketplace and observing ordinary Hypervisor API availability. It does not create a VM, enroll a worker, load a model, verify attestation, or authorize private data.

From this directory, with a Swift 6 toolchain:

```sh
swift test
swift run node-probe
swift run node-probe --market http://127.0.0.1:8787
```

Use the actual marketplace origin/port printed by the Python demo or standalone market. The CLI reads `/health` and `/v1/snapshot`; its JSON output contains only metadata counts and demo credit totals. It does not accept or print bearer tokens. HTTP is restricted to literal loopback addresses or `localhost`; remote origins require HTTPS with the platform's normal certificate validation. Redirects are refused. Responses have a 1 MiB limit and bounded network timeouts.

On macOS, the host observation calls the read-only `sysctlbyname("kern.hv_support")` query documented by [Apple Hypervisor](https://developer.apple.com/documentation/hypervisor). A successful query concerns ordinary VM support; failure means unknown, not unsupported. The response always includes `confidentialEligibility: false`, `attestation: not_performed`, and `hostConfidentiality: not_established`.

The package uses temporary caches in the restricted Codex executor. These commands worked around read-only user caches and SwiftPM's unavailable nested process sandbox while compiling only this project's trusted source:

```sh
env CLANG_MODULE_CACHE_PATH=/tmp/meta-nodeprobe-module-cache SWIFTPM_MODULECACHE_OVERRIDE=/tmp/meta-nodeprobe-module-cache swift test --disable-sandbox --scratch-path /tmp/meta-nodeprobe-build --cache-path /tmp/meta-nodeprobe-cache --config-path /tmp/meta-nodeprobe-config --security-path /tmp/meta-nodeprobe-security -Xswiftc -module-cache-path -Xswiftc /tmp/meta-nodeprobe-module-cache
/tmp/meta-nodeprobe-build/debug/node-probe
```

`--disable-sandbox` above is a SwiftPM build setting, not a worker security setting or permission to execute arbitrary job code. Most ordinary developer environments can use the shorter standard commands.

The native executable is a starting point for a later launcher/control application. A protected node and its attested endpoint key must live in the chosen protected environment; this host CLI must not become the custodian of private job keys merely because it is compiled or signed.

## Verification on October 29

- `python3 -m unittest discover -s tests -p test_probe_platform.py -v` from the workspace root: 5 tests passed.
- The temporary-cache `swift test` command above: native executable compiled; 3 Swift tests passed.
- From the workspace root, `python3 native/NodeProbe/smoke_market.py --binary /tmp/meta-nodeprobe-build/debug/node-probe`: passed against a real temporary `MarketStore` HTTP server, reading both endpoints. It generated disposable role tokens, printed none, and removed the temporary database/server afterward. The restricted executor required local-network approval because its sandbox blocks loopback binding; approval was granted.
- Both Python and native host observations preserve the unestablished-confidentiality result. The sandbox denied the ordinary Hypervisor sysctl query, so availability was reported as unknown rather than unsupported.

## Verification on November 1

Apple Swift 6.4 on arm64 macOS 27, standard commands with no temporary-cache workaround.

- `swift test`: `Executed 3 tests, with 0 failures (0 unexpected)`. Swift Testing also reported `Test run with 0 tests in 0 suites passed`, because the package's tests are XCTest.
- `smoke_market.py --binary native/NodeProbe/.build/debug/node-probe` under the project `.venv`: passed, exit 0.
- Against a standalone loopback market with one `--provider fixture` worker: exit 0, `workerCount` 1 and all other counters 0. Before the worker joined, all counters were 0. A `localhost` origin also exited 0, and a remote plain HTTP origin was refused with exit 1.
- Hypervisor query, outside the Codex sandbox: `hypervisorAPIAvailable` true, with `confidentialEligibility` false, `attestation` `not_performed` and `hostConfidentiality` `not_established`. This is ordinary VM availability, not protection.

The client sends no `Authorization` header, so it depends on `/health` and `/v1/snapshot` staying readable without a token. It does not call `/v1/status`.

The smoke helper requires the Python foundation in the same workspace. It performs no external requests or paid inference. A normal build can use `--binary native/NodeProbe/.build/debug/node-probe` instead.
