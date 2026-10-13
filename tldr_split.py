#!/usr/bin/env python3
"""Split TLDR newsletter issues into one-story-per-entry Atom feeds.

Discovers issues from https://tldr.tech/<slug>/archives, parses each issue page,
drops sponsor items, and writes:
  docs/<slug>.xml   one feed per edition
  docs/index.html   list of feed URLs
State (seen issues + recent items) lives in state/items.json so each run only
fetches new issues.

Exit code is non-zero if any edition failed, but feeds for the editions that
succeeded are still written.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from feedgen.feed import FeedGenerator

BASE = "https://tldr.tech"
EDITIONS: dict[str, str] = {
    "tech": "TLDR",
    "dev": "TLDR Dev",
    "ai": "TLDR AI",
    "hardware": "TLDR Hardware",
}
SITE_URL = os.environ.get("SITE_URL", "https://example.github.io/tldr-feeds").rstrip("/")
OUT_DIR = Path(os.environ.get("OUT_DIR", "docs"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "state/items.json"))
RETAIN_DAYS = 14            # items older than this drop out of the feeds
MAX_NEW_ISSUES = 5          # per edition per run; caps first-run backfill
REQUEST_DELAY_S = 1.0       # be polite to tldr.tech
USER_AGENT = "tldr-feed-splitter/1.0 (personal RSS use)"

ISSUE_PATH_RE = re.compile(r"^/(?P<slug>[a-z]+)/(?P<date>\d{4}-\d{2}-\d{2})/?$")
SPONSOR_RE = re.compile(r"\(\s*sponsor\s*\)", re.IGNORECASE)

log = logging.getLogger("tldr")


@dataclass
class Item:
    id: str                 # normalized article URL, used as Atom id / dedupe key
    url: str
    title: str
    summary_html: str
    edition: str
    section: str | None
    issue_date: str         # YYYY-MM-DD
    position: int           # order within the issue


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def clean_url(url: str) -> str:
    """Drop utm_* params and fragments; keep everything else (e.g. paywall tokens)."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def normalize_id(url: str) -> str:
    """Dedupe key: cleaned URL, lowercase host, no trailing slash."""
    parts = urlsplit(clean_url(url))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_archive(page_html: str, slug: str) -> list[str]:
    """Return issue dates (YYYY-MM-DD) linked from an archives page, newest first."""
    soup = BeautifulSoup(page_html, "html.parser")
    dates: set[str] = set()
    for a in soup.find_all("a", href=True):
        parts = urlsplit(urljoin(BASE, a["href"]))
        if not parts.netloc.endswith("tldr.tech"):
            continue
        m = ISSUE_PATH_RE.match(parts.path)
        if m and m["slug"] == slug:
            dates.add(m["date"])
    return sorted(dates, reverse=True)


def _contains(node: Tag, target: Tag) -> bool:
    return any(d is target for d in node.descendants)


def _html_excluding(node: Tag, heading: Tag) -> list[str]:
    """Inner HTML of node with the heading subtree removed."""
    out: list[str] = []
    for child in node.children:
        if child is heading:
            continue
        if isinstance(child, Tag) and _contains(child, heading):
            out.extend(_html_excluding(child, heading))
            continue
        if isinstance(child, NavigableString) and not child.strip():
            continue
        out.append(str(child))
    return out


def _is_boundary(node) -> bool:
    return isinstance(node, Tag) and (node.name in ("h2", "h3", "article")
                                      or node.find(["h2", "h3"]) is not None)


def _extract_summary(h3: Tag, anchor: Tag) -> str:
    # Heading unit is the <a> if it wraps the <h3>, otherwise the <h3> itself.
    heading = anchor if _contains(anchor, h3) else h3
    article = heading.find_parent("article")
    if article is not None:
        return "".join(_html_excluding(article, heading)).strip()
    parts: list[str] = []
    for sib in heading.next_siblings:
        if _is_boundary(sib):
            break
        if isinstance(sib, NavigableString) and not sib.strip():
            continue
        parts.append(str(sib))
    return "".join(parts).strip()


def _absolutize(fragment: str) -> str:
    soup = BeautifulSoup(fragment, "html.parser")
    for a in soup.find_all("a", href=True):
        a["href"] = clean_url(urljoin(BASE, a["href"]))
    return str(soup)


def parse_issue(page_html: str, slug: str, issue_date: str) -> list[Item]:
    soup = BeautifulSoup(page_html, "html.parser")
    root = soup.find("main") or soup.body or soup
    items: list[Item] = []
    section: str | None = None
    seen: set[str] = set()

    for h3 in root.find_all("h3"):
        title = " ".join(h3.get_text(" ", strip=True).split())
        anchor = h3.find("a", href=True) or h3.find_parent("a", href=True)
        if anchor is None:
            if title:
                section = title          # unlinked h3 = section header
            continue
        url = urljoin(BASE, anchor["href"])
        if urlsplit(url).netloc.endswith("tldr.tech"):
            continue                     # site nav / internal links
        if SPONSOR_RE.search(title):
            continue
        item_id = normalize_id(url)
        if item_id in seen:
            continue
        seen.add(item_id)
        items.append(Item(
            id=item_id,
            url=clean_url(url),
            title=title,
            summary_html=_absolutize(_extract_summary(h3, anchor)),
            edition=slug,
            section=section,
            issue_date=issue_date,
            position=len(items),
        ))
    return items


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
class Fetcher:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._last = 0.0

    def get(self, url: str) -> str:
        wait = REQUEST_DELAY_S - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                resp = self.session.get(url, timeout=30)
                self._last = time.monotonic()
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code} for {url}")
                return resp.text
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt == 0:
                    time.sleep(5)
        raise RuntimeError(str(last_exc))


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"issues": {}, "items": []}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


def prune(state: dict, today: date) -> None:
    cutoff = (today - timedelta(days=RETAIN_DAYS)).isoformat()
    state["items"] = [i for i in state["items"] if i["issue_date"] >= cutoff]
    # Keep issue keys a bit longer than items so a stale archive page
    # can't make us re-ingest something we already dropped.
    issue_cutoff = (today - timedelta(days=RETAIN_DAYS * 2)).isoformat()
    state["issues"] = {k: v for k, v in state["issues"].items()
                       if k.split("/", 1)[1] >= issue_cutoff}


# --------------------------------------------------------------------------- #
# Feed output
# --------------------------------------------------------------------------- #
def published_at(item: Item) -> datetime:
    # Issues have no send time; use 11:00 UTC (~6-7am ET) and subtract the
    # position so newest-first readers show the issue in its original order.
    d = date.fromisoformat(item.issue_date)
    return datetime(d.year, d.month, d.day, 11, 0, tzinfo=timezone.utc) - timedelta(seconds=item.position)


def entry_content(item: Item) -> str:
    src = f"{EDITIONS.get(item.edition, item.edition)} · {item.issue_date}"
    if item.section:
        src += f" · {html.escape(item.section)}"
    issue_url = f"{BASE}/{item.edition}/{item.issue_date}"
    return (f"{item.summary_html}"
            f'<p><small>From <a href="{issue_url}">{src}</a></small></p>')


def write_feed(path: Path, feed_id: str, title: str, items: list[Item]) -> None:
    fg = FeedGenerator()
    fg.id(feed_id)
    fg.title(title)
    fg.author({"name": "TLDR"})
    fg.link(href=feed_id, rel="self")
    fg.link(href=BASE, rel="alternate")
    fg.subtitle("TLDR newsletter, one entry per story")
    newest = max((published_at(i) for i in items), default=datetime.now(timezone.utc))
    fg.updated(newest)
    for item in sorted(items, key=published_at, reverse=True):
        fe = fg.add_entry(order="append")
        fe.id(item.id)
        fe.title(item.title)
        fe.link(href=item.url)
        fe.content(entry_content(item), type="html")
        ts = published_at(item)
        fe.published(ts)
        fe.updated(ts)
        fe.category(term=EDITIONS.get(item.edition, item.edition))
        if item.section:
            fe.category(term=item.section)
    path.parent.mkdir(parents=True, exist_ok=True)
    fg.atom_file(str(path), pretty=True)


def write_index(path: Path) -> None:
    rows = [f'<li><a href="{s}.xml">{html.escape(n)}</a></li>' for s, n in EDITIONS.items()]
    path.write_text(
        "<!doctype html><meta charset=utf-8><title>TLDR feeds</title>"
        "<h1>TLDR per-story feeds</h1><ul>" + "".join(rows) + "</ul>\n")


def build_outputs(state: dict) -> None:
    items = [Item(**i) for i in state["items"]]
    for slug, name in EDITIONS.items():
        write_feed(OUT_DIR / f"{slug}.xml", f"{SITE_URL}/{slug}.xml", name,
                   [i for i in items if i.edition == slug])
    write_index(OUT_DIR / "index.html")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run(fetcher: Fetcher, today: date) -> list[str]:
    state = load_state()
    errors: list[str] = []
    cutoff = (today - timedelta(days=RETAIN_DAYS)).isoformat()

    for slug in EDITIONS:
        try:
            dates = parse_archive(fetcher.get(f"{BASE}/{slug}/archives"), slug)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{slug}: archive fetch failed: {exc}")
            continue
        if not dates:
            errors.append(f"{slug}: no issue links found on archive page (layout change?)")
            continue
        new = [d for d in dates if d >= cutoff and f"{slug}/{d}" not in state["issues"]]
        new = sorted(new[:MAX_NEW_ISSUES])
        log.info("%s: %d issues listed, %d new", slug, len(dates), len(new))
        for d in new:
            url = f"{BASE}/{slug}/{d}"
            try:
                items = parse_issue(fetcher.get(url), slug, d)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{slug}/{d}: fetch failed: {exc}")
                continue
            if not items:
                # Not marked seen, so it's retried next run.
                errors.append(f"{slug}/{d}: parsed 0 stories (layout change or page not ready)")
                continue
            log.info("%s/%s: %d stories", slug, d, len(items))
            state["items"] = [i for i in state["items"]
                              if not (i["edition"] == slug and i["issue_date"] == d)]
            state["items"].extend(asdict(i) for i in items)
            state["issues"][f"{slug}/{d}"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    prune(state, today)
    save_state(state)
    build_outputs(state)
    return errors


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    errors = run(Fetcher(), datetime.now(timezone.utc).date())
    for e in errors:
        log.error(e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
