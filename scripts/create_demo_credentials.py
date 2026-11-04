"""Create local protocol-role credentials without printing or overwriting them."""
import argparse
import os
from pathlib import Path
import secrets


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    args = p.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths = [args.directory / "requester.token", args.directory / "enrollment.token"]
    if any(path.exists() for path in paths):
        p.error("Credential file already exists; use a new directory")
    for path in paths:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(32) + "\n")
    print("Created requester.token and enrollment.token with owner-only permissions.")


if __name__ == "__main__":
    main()
