"""Pre-flight: ensure Deskmind's host ports are free (or already held by Deskmind's containers).

Exits non-zero if a port is held by something else, so `make up` stops before starting anything.
Only probes the configured Deskmind ports; never touches 5432.
"""

from __future__ import annotations

import socket
import subprocess
import sys

sys.path.insert(0, "src")

from deskmind.config import get_settings


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def owned_by_deskmind(service: str, container_port: int, host_port: int) -> bool:
    out = subprocess.run(
        ["docker", "compose", "port", service, str(container_port)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return out.endswith(f":{host_port}")


def main() -> int:
    s = get_settings()
    checks = [("postgres", 5432, s.postgres_host_port), ("redis", 6379, s.redis_host_port)]
    failed = False
    for service, container_port, host_port in checks:
        if not port_in_use(host_port):
            print(f"port {host_port} ({service}): free")
        elif owned_by_deskmind(service, container_port, host_port):
            print(f"port {host_port} ({service}): already held by deskmind container")
        else:
            print(f"port {host_port} ({service}): IN USE by another process - aborting")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
