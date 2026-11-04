"""Build owner-only ARM parameters from credential JSON on stdin, never secret argv."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys


def main():
    try:
        target, registry = Path(sys.argv[1]), sys.argv[2]
        if not registry or target.is_symlink():
            raise ValueError()
        info = target.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError()
        credential = json.load(sys.stdin)
        password = credential["passwords"][0]["value"]
        username = credential.get("username") or registry
        if not isinstance(password, str) or not password or not isinstance(username, str):
            raise ValueError()
        params = {"registryUser": {"value": username}, "registryPassword": {"value": password},
                  "registryServer": {"value": registry + ".azurecr.io"}}
        for key, env_name in (("image", "WORKER_IMAGE"), ("modelImage", "MODEL_IMAGE"),
                              ("containerGroupName", "AZ_CONTAINER_GROUP")):
            value = os.environ[env_name]
            if not value:
                raise ValueError()
            params[key] = {"value": value}
        policy = Path(os.environ["CCE_POLICY_FILE"]).read_text().strip()
        if not policy or len(policy) > 2_000_000:
            raise ValueError()
        params["ccePolicy"] = {"value": policy}
        if os.environ.get("AZ_LOCATION"):
            params["location"] = {"value": os.environ["AZ_LOCATION"]}
        with target.open("w") as stream:
            json.dump(params, stream)
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        print("Unable to prepare deployment parameters; check configuration and credential input.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
