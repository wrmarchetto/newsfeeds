import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tldr_split as ts  # noqa: E402

# Variant A: <article><a><h3>title</h3></a><div>summary</div></article>
ISSUE_ARTICLE = """
<html><body><main>
<h1>TLDR AI 2026-10-12</h1>
<article><a href="https://cloud.example.com/agent?utm_source=tldr&e=1"><h3>Meet the agent (Sponsor)</h3></a>
  <div class="newsletter-html">Buy things.</div></article>
<section><header><h3>Headlines &amp; Launches</h3></header>
<article><a href="https://www.bloomberg.com/news/x?accessToken=abc&utm_source=tldrai"><h3>Nvidia Discusses Reflection (3 minute read)</h3></a>
  <div class="newsletter-html">Nvidia is in <a href="/ai">early</a> talks.</div></article>
<article><a href="https://www.anthropic.com/research/x/?utm_source=tldrai"><h3>Investigating unintended actions (16 minute read)</h3></a>
  <div class="newsletter-html">Report details.</div></article>
</section>
<section><header><h3>Quick Links</h3></header>
<article><a href="https://github.com/foo/bar?utm_source=tldrai"><h3>foo/bar (GitHub Repo)</h3></a>
  <div class="newsletter-html">A repo.</div></article>
</section>
</main></body></html>
"""

# Variant B: flat <h3><a>title</a></h3><p>summary</p> siblings
ISSUE_FLAT = """
<html><body><main>
<h3>Headlines</h3>
<h3><a href="https://a.example.com/one?utm_source=tldr">Story one (2 minute read)</a></h3>
<p>First summary.</p><ul><li>bullet</li></ul>
<h3><a href="https://b.example.com/two">Story two (Sponsor)</a></h3>
<p>Ad copy.</p>
<h3>Deep Dives</h3>
<h3><a href="https://c.example.com/three">Story three (5 minute read)</a></h3>
<p>Third summary.</p>
<h2>Get the most interesting stories</h2>
<p>Subscribe</p>
</main></body></html>
"""

ARCHIVE = """
<html><body>
<a href="https://tldr.tech/">TLDR</a>
<a href="https://tldr.tech/ai/2026-10-12">Gemini 4</a>
<a href="/ai/2026-10-09">GPT</a>
<a href="https://tldr.tech/tech/2026-10-12">other edition</a>
<a href="https://tldr.tech/ai/archives">archives</a>
</body></html>
"""


class ParseTests(unittest.TestCase):
    def test_article_layout(self):
        items = ts.parse_issue(ISSUE_ARTICLE, "ai", "2026-10-12")
        self.assertEqual([i.title for i in items], [
            "Nvidia Discusses Reflection (3 minute read)",
            "Investigating unintended actions (16 minute read)",
            "foo/bar (GitHub Repo)",
        ])
        self.assertEqual(items[0].section, "Headlines & Launches")
        self.assertEqual(items[2].section, "Quick Links")
        self.assertEqual(items[0].url, "https://www.bloomberg.com/news/x?accessToken=abc")
        self.assertIn("Nvidia is in", items[0].summary_html)
        self.assertNotIn("<h3", items[0].summary_html)
        self.assertIn('href="https://tldr.tech/ai"', items[0].summary_html)
        self.assertEqual(items[1].id, "https://www.anthropic.com/research/x")
        self.assertEqual([i.position for i in items], [0, 1, 2])

    def test_flat_layout(self):
        items = ts.parse_issue(ISSUE_FLAT, "dev", "2026-10-13")
        self.assertEqual([i.title for i in items],
                         ["Story one (2 minute read)", "Story three (5 minute read)"])
        self.assertEqual(items[0].section, "Headlines")
        self.assertEqual(items[1].section, "Deep Dives")
        self.assertIn("First summary.", items[0].summary_html)
        self.assertIn("bullet", items[0].summary_html)
        self.assertNotIn("Ad copy", items[0].summary_html)
        self.assertNotIn("Subscribe", items[1].summary_html)

    def test_archive(self):
        self.assertEqual(ts.parse_archive(ARCHIVE, "ai"), ["2026-10-12", "2026-10-09"])


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if url not in self.pages:
            raise RuntimeError(f"HTTP 404 for {url}")
        return self.pages[url]


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ts.OUT_DIR = Path(self.tmp.name) / "docs"
        ts.STATE_FILE = Path(self.tmp.name) / "state" / "items.json"
        ts.EDITIONS = {"ai": "TLDR AI", "tech": "TLDR"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_per_edition_and_incremental(self):
        tech_issue = ISSUE_ARTICLE.replace("tldrai", "tldrtech")  # same stories, other edition
        pages = {
            "https://tldr.tech/ai/archives": ARCHIVE,
            "https://tldr.tech/ai/2026-10-12": ISSUE_ARTICLE,
            "https://tldr.tech/ai/2026-10-09": ISSUE_FLAT,
            "https://tldr.tech/tech/archives": '<a href="/tech/2026-10-12">x</a>',
            "https://tldr.tech/tech/2026-10-12": tech_issue,
        }
        f = FakeFetcher(pages)
        errors = ts.run(f, date(2026, 10, 13))
        self.assertEqual(errors, [])

        ns = {"a": "http://www.w3.org/2005/Atom"}
        ai = ET.parse(ts.OUT_DIR / "ai.xml").getroot().findall("a:entry", ns)
        tech = ET.parse(ts.OUT_DIR / "tech.xml").getroot().findall("a:entry", ns)
        self.assertEqual(len(ai), 5)
        self.assertEqual(len(tech), 3)  # each edition keeps its own copy
        self.assertFalse((ts.OUT_DIR / "all.xml").exists())
        self.assertTrue((ts.OUT_DIR / "index.html").exists())

        # Second run fetches only archive pages.
        f2 = FakeFetcher(pages)
        self.assertEqual(ts.run(f2, date(2026, 10, 13)), [])
        self.assertTrue(all(u.endswith("/archives") for u in f2.calls))

    def test_empty_issue_is_error_and_retried(self):
        pages = {
            "https://tldr.tech/ai/archives": '<a href="/ai/2026-10-12">x</a>',
            "https://tldr.tech/ai/2026-10-12": "<html><body><main><p>soon</p></main></body></html>",
            "https://tldr.tech/tech/archives": "<html></html>",
        }
        errors = ts.run(FakeFetcher(pages), date(2026, 10, 13))
        self.assertEqual(len(errors), 2)  # empty ai issue + no tech links
        state = ts.load_state()
        self.assertNotIn("ai/2026-10-12", state["issues"])


if __name__ == "__main__":
    unittest.main()
