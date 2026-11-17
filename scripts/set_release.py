"""Record a release in values-aws.yaml: the image tag and, if given, the public URL.

Usage: set_release.py <values-aws.yaml> <tag> [url]. Called by scripts/release.sh.
"""

from __future__ import annotations

import re
import sys


def set_release(text: str, tag: str, url: str = "") -> str:
    # Quoted: a short SHA can be all digits, which YAML would read as a number.
    text, n = re.subn(r"(?m)^(    tag: ).*$", lambda m: f'{m.group(1)}"{tag}"', text)
    if n != 1:
        raise ValueError(f"expected one 'tag:' under global.image, found {n}")
    if url:
        text, n = re.subn(r"(?m)^(  publicUrl: ).*$", lambda m: m.group(1) + url, text)
        if n != 1:
            raise ValueError(f"expected one 'publicUrl:' under global, found {n}")
    return text


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(__doc__, file=sys.stderr)
        return 2
    path, tag = argv[1], argv[2]
    url = argv[3] if len(argv) == 4 else ""
    with open(path, encoding="utf-8", newline="") as handle:
        text = handle.read()
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(set_release(text, tag, url))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
