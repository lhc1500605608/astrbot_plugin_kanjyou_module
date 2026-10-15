"""Generate AstrBot's self-contained page entries from the shared UI source.

Run after editing pages/common/panel.html or pages/common/app.js.
Use --check in CI to detect stale embedded copies without modifying files.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def render(source: str, helper: str, page: str) -> str:
    # AstrBot loads entries in isolated frames. Inline the helper so no asset
    # path or bundler runtime is required, keeping subscriptions identical.
    source = re.sub(
        r"<html\b([^>]*)>",
        lambda m: (
            "<html"
            + re.sub(r"\sdata-page=[\"\'][^\"\']*[\"\']", "", m[1])
            + f' data-page="{page}">'
        ),
        source,
        count=1,
    )
    script = "<script>\n" + helper.rstrip() + "\n</script>\n"
    position = source.lower().find("<script")
    if position < 0:
        raise ValueError("panel.html must contain its application script")
    return source[:position] + script + source[position:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source = (ROOT / "pages/common/panel.html").read_text(encoding="utf-8")
    helper = (ROOT / "pages/common/app.js").read_text(encoding="utf-8")
    stale = []
    for page in ("status", "control", "logs"):
        target = ROOT / "pages" / page / "index.html"
        expected = render(source, helper, page)
        if not target.exists() or target.read_text(encoding="utf-8") != expected:
            stale.append(page)
            if not args.check:
                target.write_text(expected, encoding="utf-8")
    if args.check and stale:
        print("Out-of-date WebUI entries: " + ", ".join(stale))
        return 1
    print("WebUI entries " + ("match shared sources" if args.check else "generated"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
