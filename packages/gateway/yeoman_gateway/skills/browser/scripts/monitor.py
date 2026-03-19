#!/usr/bin/env python3
"""Webpage change detector for yeoman's browser skill."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def pinchtab(method: str, path: str, body: dict | None = None) -> dict | str:
    base = os.getenv("PINCHTAB_URL", "http://localhost:9867")
    token = os.getenv("BRIDGE_TOKEN", "")

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw.decode()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Pinchtab error {e.code}: {e.read().decode()}") from e
    except (ConnectionRefusedError, OSError) as e:
        raise SystemExit(
            "Cannot connect to pinchtab. Start it with the browser skill's scripts/start.sh."
        ) from e


def extract_text(result: dict | str) -> str:
    if isinstance(result, str):
        return result.strip()
    for key in ("content", "text", "snapshot", "result"):
        val = result.get(key, "")
        if val:
            return str(val).strip()
    return json.dumps(result, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor a webpage for changes via pinchtab")
    parser.add_argument("--url", "-u", required=True, help="URL to monitor")
    parser.add_argument("--label", "-l", default="", help="Human-readable label for notifications")
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="No output when nothing changed",
    )
    parser.add_argument(
        "--filter",
        choices=["interactive", "text"],
        default=None,
        help="Snapshot filter",
    )
    parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    parser.add_argument("--screenshot", "-s", action="store_true", help="Include screenshot hint")
    args = parser.parse_args()

    label = args.label or args.url

    pinchtab("POST", "/navigate", {"url": args.url})

    qs = "diff=true&format=text"
    if args.filter:
        qs += f"&filter={args.filter}"
    result = pinchtab("GET", f"/snapshot?{qs}")
    content = extract_text(result)
    changed = bool(content)

    if not changed:
        if not args.quiet:
            print(f"[no change] {label}")
        sys.exit(0)

    if args.json:
        out: dict = {"label": label, "url": args.url, "changed": True, "diff": content}
        if args.screenshot:
            out["screenshot_endpoint"] = "GET http://localhost:9867/screenshot"
        print(json.dumps(out, indent=2))
    else:
        print(f"[CHANGED] {label}")
        print(f"URL: {args.url}")
        print()
        if len(content) > 3000:
            print(content[:3000])
            print(f"\n... ({len(content) - 3000} more chars)")
        else:
            print(content)


if __name__ == "__main__":
    main()
