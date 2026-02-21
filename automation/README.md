# Auto X Publisher (MVP)

This is a minimal app that automates:

1. RSS collection
2. Article draft generation
3. Human approval
4. Posting to X

## Why this flow

The default flow is intentionally safe:

- `draft`: gather and generate
- `approve`: human confirms
- `publish`: post only approved content

This avoids accidental auto-posting of low-quality or unsafe text.

## Quick start

1. Update config:

```bash
cp automation/example_config.json automation/config.json
```

2. (Optional but recommended) Set OpenAI key for better drafts:

```bash
export OPENAI_API_KEY="..."
```

3. Generate draft:

```bash
python -m automation.x_publisher draft --config automation/config.json
```

4. Inspect output in `automation/runs/<run_id>/`:

- `sources.json`
- `article.md`
- `x_post.txt`

5. Approve the run:

```bash
python -m automation.x_publisher approve --config automation/config.json --run-id <run_id>
```

6. Set X credentials:

```bash
export X_API_KEY="..."
export X_API_SECRET="..."
export X_ACCESS_TOKEN="..."
export X_ACCESS_TOKEN_SECRET="..."
```

7. Publish:

```bash
python -m automation.x_publisher publish --config automation/config.json --run-id <run_id>
```

## Dry run

Use `--dry-run` to verify the full flow without calling X API.

```bash
python -m automation.x_publisher publish --config automation/config.json --run-id <run_id> --dry-run
```

## One-command mode

Create draft, and optionally auto approve / auto publish:

```bash
python -m automation.x_publisher run --config automation/config.json
python -m automation.x_publisher run --config automation/config.json --auto-approve
python -m automation.x_publisher run --config automation/config.json --auto-publish --dry-run
```

