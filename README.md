# selfhst-md

Archive Self-Host Weekly posts from `https://selfh.st/weekly/rss/` as source-oriented Markdown.

## Scope

This project only does the following:

- Reads the public selfh.st weekly RSS feed to find Self-Host Weekly issues.
- Fetches the source article page for each selected issue.
- Extracts as much of the original article body as practical into Markdown.
- Saves issue files under `newsletters/selfh-st/weekly/YYYY-MM-DD.md`.
- Maintains `newsletters/selfh-st/weekly/README.md` as a newest-first archive index.

It does not translate issues, publish downstream posts, or call model services.

The RSS feed is used for discovery. The feed is not full-text, so each source article page is fetched and parsed to produce the Markdown body.

## Markdown Output

Each generated issue keeps article metadata in YAML frontmatter:

- `title`
- `url`
- `published_at`
- `fetched_at`
- `description`
- `author`
- `image`

The Markdown body intentionally avoids generated summaries or synthetic digest sections. It preserves the source title, description, headings, paragraphs, links, lists, blockquotes, code blocks, bookmark cards, images, and source sections that are visible in the article HTML.

The current checked-in archive contains every 2026 Self-Host Weekly issue visible in the RSS feed at generation time.

## Local Usage

Fetch the latest issue from the RSS feed:

```bash
python3 -m selfhst_weekly_md.archive
```

Fetch every 2026 issue currently visible in the RSS feed:

```bash
python3 -m selfhst_weekly_md.archive --all --year 2026
```

Fetch a specific issue:

```bash
python3 -m selfhst_weekly_md.archive \
  --url https://selfh.st/weekly/2026-05-29/
```

Write to a custom output directory:

```bash
python3 -m selfhst_weekly_md.archive \
  --output-dir newsletters/selfh-st/weekly
```

Existing issue files are left unchanged by default. Use `--force` when you intentionally want to regenerate an existing Markdown file.

## Archive

Generated issues are stored in:

```text
newsletters/selfh-st/weekly/
```

The archive index is generated at:

```text
newsletters/selfh-st/weekly/README.md
```

## Tests

```bash
python3 -m unittest discover -s tests
```

## GitHub Actions

`.github/workflows/selfhst-weekly.yml` is written for normal external GitHub Actions, not an internal or self-hosted runner.

- Runner: `ubuntu-latest`
- Network: public outbound internet from the GitHub-hosted runner
- Push trigger: archives every 2026 Self-Host Weekly issue currently visible in RSS when pushed to `main`
- Schedule: every Friday at 13:30 UTC, using the same RSS-visible 2026 archive path
- Manual inputs: `newsletter_url` for one issue, or `archive_year` for RSS-visible issues from a different year
- Secrets: none
- Commit scope: generated issue Markdown files and the archive index

The workflow first verifies that `https://selfh.st/weekly/rss/` is reachable, then runs the test suite, generates Markdown for all selected RSS entries, and commits only when the archive changed. The repository must allow `GITHUB_TOKEN` to create commits.
