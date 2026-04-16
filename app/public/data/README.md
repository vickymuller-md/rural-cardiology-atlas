# `public/data/`

This directory is populated by the Python pipeline at `../scripts/`. Run:

```bash
cd ../scripts && uv sync && uv run python run_all.py
cp data/processed/counties.json ../app/public/data/counties.json
```

The final `counties.json` is ~2 MB (~3,144 county records) and is committed
to the repo so Vercel can serve it as a static asset without re-running the
pipeline on every deploy.
