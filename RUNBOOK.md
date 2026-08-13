# RUNBOOK — running this on your machine (macOS / Linux / Windows)

> Want it running continuously with a dashboard, real emails and a live Google Sheet
> instead of by hand? That's one command — see **DEPLOY.md**. This runbook is the
> manual path, and is still the fastest way to see the whole pipeline narrate itself.

## 0. Prerequisites

Python **3.11 or newer**. Check:

```bash
python3 --version
```

If it prints 3.9 or 3.10 (common on stock macOS), install a newer one:

```bash
brew install python@3.12
```

Nothing else is required. No Docker, no Postgres, no cloud account, no paid key.

## 1. Unpack and set up (one time, ~1 minute)

```bash
cd ~/Downloads
unzip deal-engine.zip
cd deal-engine

python3 -m venv .venv          # use python3.12 here if your default python3 is older
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

You should see httpx, feedparser, beautifulsoup4, openpyxl, PyYAML, APScheduler, openai,
pydantic, python-dateutil, numpy install. `(.venv)` now appears in your prompt — keep that
shell open for everything below.

## 2. The demo (this is what you show)

```bash
python demo.py
```

Runs the full narrated pipeline in roughly 30–90 seconds (slower on first run because it is
fetching live), then drops you into the partner chat REPL. Ctrl-D exits.

What it prints, in order: which licensed adapters were skipped and why → live ingest counts
per source → four name variants collapsing into one record → the deterministic filter's
before/after → enrichment → the scoring funnel with token spend per tier → briefs published
→ commentary harvested → sector clusters with signal-to-consensus ratios → peer/co-investor
tracking → the workbook → the digest → instant alerts → source health → the fund's three
example questions answered with citations.

**Outputs land in `output/`:**

- `output/deal_pipeline.xlsx` — the nine required tabs plus Provenance and Demo Cases
- `output/digests/digest_<date>.html` — open in a browser
- `output/briefs/*.md` — one per auto-triggered company
- `output/alerts/*.html` — any instant alert that fired

Open the workbook: `open output/deal_pipeline.xlsx` (macOS).

## 3. Prove it to yourself

```bash
python tests/acceptance.py
python tests/gatekeeper_test.py
```

19 checks mapping 1:1 onto the assignment's acceptance criteria, including the accuracy gate
(five random figures must each trace to a real fetchable URL). Expect `19/19 passed`. Run it
**after** `demo.py`, since it inspects the generated workbook. It uses a throwaway copy of
the database for the mutating tests, so it never disturbs your demo state.

## 4. The other entry points

```bash
python chat.py                                   # interactive partner REPL
python chat.py "who's quietly investing in robotics?"   # one-shot answer
python run.py                                    # supervisor: every scheduled job
```

`run.py` prints each job with its next run time and stays running (Ctrl-C to stop). Job ids
map 1:1 onto the n8n workflow names in the production architecture.

## 5. Turning on model judgment (optional)

Without a key the pipeline runs completely, and every judgment field reads
`[STUB: no API key — judgment unavailable]` — deliberately, so stub output can never be
mistaken for analysis. With a key, judged scoring, brief narratives, TAM estimates,
commentary sentiment and digest rationales become real:

```bash
export NVIDIA_API_KEY=your_key_here
python demo.py
```

Model names live in `config/models.yaml` — never inline in code. The configured provider
is NVIDIA NIM's OpenAI-compatible endpoint serving Thinking Machines' Inkling on every
tier; any OpenAI-compatible provider is a base_url + key + tier-name change.

## 6. Live network vs snapshot cache

Adapters are live-first. On your machine they hit SEC EDGAR, Hacker News, the RSS feeds,
arXiv and GitHub directly, so you will see **more** data than the shipped run: Form D
offering amounts fill in, RSS funding announcements appear, and the sector clusters get
richer. If a host is unreachable, the adapter falls back to a snapshot of a real earlier
fetch in `data/cache/` and records `fetch_mode='cached_snapshot'` on every signal, so
provenance stays unambiguous. Nothing in that cache is hand-written.

To start completely fresh (re-ingest everything from live sources):

```bash
rm -f data/engine.db && python demo.py
```

## 7. Demonstrating the two-way sync live

1. Open `output/deal_pipeline.xlsx`, go to the **Pipeline** tab.
2. Change a **Recommendation** cell (column O) to `Pass` or `Deep Dive`. Save. Close.
3. Run `python -c "from outputs.excel import write_workbook; write_workbook()"`

It prints `two-way sync: 1 human edit(s) written back to DB (human value wins)`, your value
survives the regeneration, and a `partner_actions` row is logged with the feature vector
that produced the original recommendation. That is the feedback loop.

## 8. If something goes wrong

| Symptom | Fix |
|---|---|
| `SyntaxError` on startup | You are on Python < 3.11. Recreate the venv with 3.11+. |
| `pip install` permission errors | You skipped the venv. Do step 1 again. |
| `ModuleNotFoundError` | Run commands from the `deal-engine/` directory with the venv active. |
| Ingest counts are 0 for a source | Expected on a blocked network; check the printed health lines — the source is reported, never silently empty. |
| A source health alert appears | That is the feature working (component 14): a source quiet beyond 2× its interval is surfaced, not hidden. |
