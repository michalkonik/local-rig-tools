#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Helper for /find-in-dark-web: query AHMIA (a Tor-hidden-service-only index)
through the local Tor SOCKS5 proxy and print ranked .onion results.

Ahmia serves a JS shell unless you send the CSRF-ish form token from its
front page, so this script: (1) fetches the front page, (2) scrapes the hidden
<input name=... value=...> token, (3) re-requests /search/ with q + token,
(4) parses the ranked result list.

Usage:
    ahmia_query.py "<query>" [--count N]
"""

import html as htmllib
import re
import subprocess
import sys
import urllib.parse

SOCKS = "127.0.0.1:9050"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
# Real Ahmia onion service (starts juhanurmi..., ends 4csyd.onion).
AHMIA = "juhanurmihxlp77nkq76byazcldy2hlmovfu2epvl5ankdibsot4csyd.onion"


def fetch(url: str) -> str:
    r = subprocess.run(
        ["curl", "-sSL", "--socks5-hostname", SOCKS, "--max-time", "90",
         "-A", UA, url],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return f"__CURL_ERROR__ {r.stderr.strip()} (exit {r.returncode})"
    return r.stdout


def get_token(page: str) -> str:
    m = re.search(r'name="([0-9a-f]{6})" value="([0-9a-f]{6})"', page)
    return m.group(1) + "=" + m.group(2) if m else ""


def parse(page: str, count: int) -> list[dict]:
    """Ahmia result markup: <li class="result"><h4><a href="/search/redirect?...
    redirect_url=http://<onion>/">TITLE</a></h4><p>SNIPPET</p><cite>ONION</cite>
    <span class="lastSeen" data-timestamp="...">AGE</span>"""
    out = []
    total = re.search(r'id="totalResults">\s*([\d,]+)', page)
    for blk in re.finditer(r'<li class="result">(.*?)</li>', page, re.S):
        b = blk.group(1)
        mu = re.search(r'redirect_url=([^"&]+)', b)
        if not mu:
            continue
        url = htmllib.unescape(mu.group(1))
        mt = re.search(r'<a[^>]*>(.*?)</a>', b, re.S)
        title = htmllib.unescape(re.sub(r"<[^>]+>", "", mt.group(1))).strip() if mt else ""
        # collapse keyword-stuffed titles ("lsd lsd lsd lsd")
        title = re.sub(r'\b(\w+)\s+(?=\1\b)', '', title)
        title = re.sub(r'\s{2,}', ' ', title).strip()
        ms = re.search(r'</h4>\s*<p>(.*?)</p>', b, re.S)
        snip = htmllib.unescape(re.sub(r"<[^>]+>", "", ms.group(1))).strip() if ms else ""
        snip = re.sub(r'\s{2,}', ' ', snip)
        ml = re.search(r'data-timestamp="([^"]+)"[^>]*>\s*([^<]+)', b)
        seen = (ml.group(1).strip() + " (" + ml.group(2).strip() + ")") if ml else "?"
        out.append({"title": title, "url": url, "snippet": snip, "last_seen": seen})
        if len(out) >= count:
            break
    return out, (total.group(1) if total else "?")


def main(argv):
    args = list(argv)
    count = 10
    if "--count" in args:
        i = args.index("--count")
        count = int(args[i + 1])
        del args[i:i + 2]
    query = " ".join(args).strip()
    if not query:
        print('usage: ahmia_query.py "<query>" [--count N]')
        return 2

    print(f"[tor] ahmia (onion-only index) for: {query}")
    front = fetch(f"http://{AHMIA}/")
    if front.startswith("__CURL_ERROR__"):
        print(front)
        return 1
    token = get_token(front)
    url = f"http://{AHMIA}/search/?q={urllib.parse.quote(query)}"
    if token:
        url += "&" + token
    page = fetch(url)
    if page.startswith("__CURL_ERROR__"):
        print(page)
        return 1

    results, total = parse(page, count)
    if not results:
        print(f"no onion results for: {query} (page {len(page)} bytes, "
              f"token={'yes' if token else 'no'})")
        return 1

    print(f"\nTOP {len(results)} of {total} ONION-ONLY RESULTS for: {query}\n")
    for i, r in enumerate(results, 1):
        print(f"{i}. {r['title'][:90]}")
        print(f"   {r['url']}")
        if r["snippet"]:
            print(f"   {r['snippet'][:220]}")
        print(f"   last seen: {r['last_seen']}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
