"""Create a self-signed development TLS certificate and key with openssl.

For local development only. Anyone holding key.pem can impersonate these services.
The directory default is gitignored, and *.pem and *.key are ignored everywhere.
"""
import argparse
import ipaddress
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def subject_alt_names(hosts):
    names = []
    for host in hosts:
        try:
            names.append("IP:" + str(ipaddress.ip_address(host)))
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
                raise ValueError(f"Invalid certificate host: {host!r}") from None
            names.append("DNS:" + host)
    return ",".join(names)


def create_certificate(directory, hosts=(), days=30, openssl="openssl"):
    directory = Path(directory)
    cert, key = directory / "cert.pem", directory / "key.pem"
    if cert.exists() or key.exists():
        raise ValueError("Certificate or key already exists; use a new directory")
    if not shutil.which(openssl):
        raise ValueError("openssl was not found")
    all_hosts = list(dict.fromkeys(["localhost", "127.0.0.1", *hosts]))
    config = (
        "[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=agent-marketplace-development\n"
        "[v3]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        f"extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nsubjectAltName={subject_alt_names(all_hosts)}\n"
    )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    previous = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(dir=directory) as scratch:
            config_path, new_key, new_cert = (Path(scratch) / name for name in ("openssl.cnf", "key.pem", "cert.pem"))
            config_path.write_text(config)
            subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", str(days),
                            "-config", str(config_path), "-extensions", "v3", "-keyout", str(new_key), "-out", str(new_cert)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            new_key.chmod(0o600)
            new_cert.chmod(0o644)
            new_key.replace(key)
            new_cert.replace(cert)
    finally:
        os.umask(previous)
    return cert, key


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path, nargs="?", default=Path(".demo/tls"))
    p.add_argument("--host", action="append", default=[], help="Address or name clients connect to; repeatable. localhost and 127.0.0.1 are always included")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--openssl", default="openssl")
    args = p.parse_args()
    try:
        cert, key = create_certificate(args.directory, args.host, args.days, args.openssl)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from None
    print(f"Certificate (copy to clients, use with --ca-file): {cert}")
    print(f"Private key (owner-only, never copy or commit): {key}")


if __name__ == "__main__":
    main()
