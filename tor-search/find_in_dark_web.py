#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Skill: /find-in-dark-web
Search indexed content over the Tor network (anonymous), via the local Tor
SOCKS5 proxy at 127.0.0.1:9050.

Usage:
    find_in_dark_web.py "<query>" [--count N]

Implementation detail: queries DuckDuckGo's official .onion search endpoint
through the Tor SOCKS proxy. This is a LEGITIMATE public index — the same
search engine duckduckgo.com, reachable only via Tor. It indexes public
onion sites; it is NOT a tool for locating illegal content.
All queries exit through the Tor network (anonymous exit IP).
"""

import sys
import re
import html as htmllib
import urllib.parse
import subprocess

# DuckDuckGo public .onion endpoint (official v3 onion address).
# NOTE: the previous value in this file was malformed (60 chars) so Tor
# rejected it with "Invalid hostname"; fixed 2026-10-28.
DDG_ONION = "duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion"
SOCKS = "127.0.0.1:9050"          # Tor SOCKS5 proxy (tor@default.service)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
# /html/ is DDG's JS-free endpoint; the default "/" page is a JS app shell
# that returns no parsable result anchors.
BASE = f"http://{DDG_ONION}/html/?q="


def fetch(query: str) -> str:
    url = BASE + urllib.parse.quote(query)
    r = subprocess.run(
        ["curl", "-sSL",
         "--socks5-hostname", SOCKS,   # resolve + route through Tor
         "--max-time", "60", "-A", UA, url],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        # typical failure: onion service unreachable / SOCKS refused
        return f"__CURL_ERROR__ {r.stderr.strip()} (exit {r.returncode})"
    return r.stdout


def parse(html: str, count: int) -> list[dict]:
    results = []
    for anchor in re.finditer(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=class="result__a"|\Z)',
        html, re.S,
    ):
        url = htmllib.unescape(anchor.group(1))
        title = htmllib.unescape(re.sub(r"<[^>]+>", "", anchor.group(2))).strip()
        snip = ""
        m = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', anchor.group(3), re.S)
        if m:
            snip = htmllib.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        results.append({"title": title, "url": url, "snippet": snip})
        if len(results) >= count:
            break
    return results


def main(argv):
    args = list(argv)
    count = 5
    if "--count" in args:
        i = args.index("--count")
        count = int(args[i + 1]); del args[i:i + 2]
    query = " ".join(args).strip()
    if not query:
        print("usage: find_in_dark_web.py \"<query>\" [--count N]")
        return 2

    print(f"[tor] searching (exit via Tor proxy {SOCKS}) for: {query}")
    html = fetch(query)
    if html.startswith("__CURL_ERROR__"):
        print(html)
        print("\nSuggestion: confirm Tor is up: sudo systemctl restart tor@default")
        return 1

    results = parse(html, count)
    if not results:
        print(f"no indexed results for: {query}")
        return 1

    print(f"\nTOP {len(results)} ONION RESULTS for: {query}\n")
    for i, r in enumerate(results, 1):
        print(f"{i}. {r['title']}")
        print(f"   {r['url']}")
        if r["snippet"]:
            print(f"   {r['snippet'][:200]}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
