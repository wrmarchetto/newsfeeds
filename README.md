# TLDR per-story feeds

Splits TLDR, TLDR Dev, TLDR AI and TLDR Hardware issues into Atom feeds with
one entry per story (sponsors removed), published via GitHub Pages.

Feeds (after setup): `https://<user>.github.io/<repo>/{tech,dev,ai,hardware}.xml`

## Setup
1. Create a **public** repo and push this directory to `main`
   (Pages on private repos needs a paid plan).
2. Settings → Pages → Source: *Deploy from a branch*, branch `main`, folder `/docs`.
3. Actions → *Build TLDR feeds* → *Run workflow* to do the first build.
   (Settings → Actions → General → Workflow permissions must allow read/write;
   the workflow also requests `contents: write`.)
4. Check the run log: each edition should report "N stories" per issue.
5. Subscribe NewsBlur to each of the four feeds.

## How it works
- Discovers issues from `https://tldr.tech/<slug>/archives`.
- Parses each issue: unlinked `<h3>` = section header, linked `<h3>` = story,
  following content = summary. Titles containing "(Sponsor)" are dropped.
- Entry id = article URL with `utm_*` stripped, so NewsBlur dedupes cleanly.
- Keeps 14 days of items in `state/items.json`; only new issues are fetched.
- If an edition fails (HTTP error, 0 stories parsed), the other feeds still
  publish, the issue is retried next run, and the workflow is marked failed so
  GitHub emails you.

## Local run
    pip install -r requirements.txt
    python -m unittest discover -s tests
    SITE_URL=http://localhost:8000 python tldr_split.py && python -m http.server -d docs

Edit `EDITIONS` in `tldr_split.py` to add or remove newsletters.
