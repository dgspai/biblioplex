#!/usr/bin/env python3
"""Fail if any relative `](target)` link in a tracked *.md file points at a missing path.

Skips http(s)/mailto/#anchor links and fenced code blocks. Anchors (`file.md#x`) are
stripped; only file existence is checked (ponytail: no heading-anchor validation).
"""
import re
import subprocess
import sys
from pathlib import Path

LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^\s*(```|~~~)")


def broken(md: Path) -> list[tuple[int, str]]:
    bad, in_fence = [], False
    for n, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
        if FENCE.match(line):
            in_fence = not in_fence
        if in_fence:
            continue
        for target in LINK.findall(line):
            if re.match(r"^([a-z][a-z0-9+.-]*:|#)", target, re.I):
                continue
            path = target.split("#", 1)[0].split("?", 1)[0]
            if path and not (md.parent / path).exists():
                bad.append((n, target))
    return bad


def main() -> int:
    files = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "*.md"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    failures = 0
    for f in files:
        p = Path(f)
        if not p.exists():
            continue  # deleted in working tree
        for n, t in broken(p):
            print(f"{f}:{n}: broken link -> {t}")
            failures += 1
    print(f"check-links: {failures} broken" if failures else "check-links OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
