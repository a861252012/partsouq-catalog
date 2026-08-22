from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.etree import ElementTree


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail when a pytest JUnit report contains an undocumented skip.",
    )
    parser.add_argument("report", type=Path)
    parser.add_argument("--allow-message", action="append", default=[])
    return parser.parse_args()


def skipped_messages(report: Path) -> list[str]:
    root = ElementTree.parse(report).getroot()
    messages: list[str] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "skipped":
            continue
        message = element.attrib.get("message", "").strip()
        if message.startswith("Skipped: "):
            message = message.removeprefix("Skipped: ").strip()
        messages.append(message)
    return messages


def main() -> int:
    args = parse_args()
    messages = skipped_messages(args.report)
    unexpected = [
        message for message in messages if not message or message not in args.allow_message
    ]
    if unexpected:
        print("Unexpected pytest skips:", file=sys.stderr)
        for message in unexpected:
            print(f"- {message or '<missing reason>'}", file=sys.stderr)
        return 1
    print(f"Verified {len(messages)} documented pytest skip(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
