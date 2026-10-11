"""Refresh artifact hashes for reviewed version pins using official PyPI metadata.

This does not resolve dependencies or select versions. Never run it automatically
at deployment: review pin changes and audit them before publishing a new lock.
"""

import concurrent.futures
import json
import re
import urllib.request
from pathlib import Path

LOCK = Path(__file__).resolve().parents[1] / "requirements.lock"
PIN = re.compile(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)")


def hashes(pin):
    name, version = pin.groups()
    with urllib.request.urlopen(
        f"https://pypi.org/pypi/{name}/{version}/json", timeout=30
    ) as response:
        metadata = json.load(response)
    values = sorted(
        {
            file["digests"]["sha256"]
            for file in metadata["urls"]
            if not file.get("yanked")
        }
    )
    if not values or any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in values):
        raise ValueError(f"No valid release hashes for {name}=={version}")
    return (
        f"{name}=={version} \\\n"
        + " \\\n".join(f"    --hash=sha256:{value}" for value in values)
        + "\n"
    )


def main():
    pins = [
        match
        for line in LOCK.read_text().splitlines()
        if (match := PIN.fullmatch(line.strip().removesuffix(" \\")))
    ]
    if not pins:
        raise ValueError("No reviewed version pins found")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        blocks = list(executor.map(hashes, pins))
    LOCK.write_text(
        "# Reviewed runtime pins. Every allowed PyPI artifact is hash-verified.\n--require-hashes\n--only-binary=:all:\n"
        + "".join(blocks)
    )
    print(f"Updated artifact hashes for {len(pins)} pinned packages.")


if __name__ == "__main__":
    main()
