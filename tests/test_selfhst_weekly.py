import json
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from selfhst_weekly_md.archive import (
    ContentItem,
    IssueSource,
    ParsedArticle,
    Section,
    build_markdown,
    collect_feed_issues,
    collect_issue,
    parse_article,
    parse_feed,
    parse_feed_items,
    update_index,
    write_issue,
)


class SelfHostWeeklyTests(unittest.TestCase):
    def test_parse_feed_selects_latest_weekly_item(self):
        feed_xml = textwrap.dedent(
            """\
            <?xml version="1.0" encoding="UTF-8"?>
            <rss xmlns:dc="http://purl.org/dc/elements/1.1/"
                 xmlns:media="http://search.yahoo.com/mrss/">
              <channel>
                <item>
                  <title><![CDATA[Other Post]]></title>
                  <link>https://selfh.st/post/other/</link>
                  <pubDate>Mon, 01 Jun 2026 08:00:00 -0400</pubDate>
                </item>
                <item>
                  <title><![CDATA[Self-Host Weekly (29 May 2026)]]></title>
                  <description><![CDATA[We've officially reached the papal stage of the AI debate]]></description>
                  <link>https://selfh.st/weekly/2026-05-29/</link>
                  <category><![CDATA[Self-Host Weekly]]></category>
                  <dc:creator><![CDATA[Ethan Sholly]]></dc:creator>
                  <pubDate>Fri, 29 May 2026 07:54:36 -0400</pubDate>
                  <media:content url="https://selfh.st/content/images/2026/05/2026-05-29-featured-image.png" medium="image"/>
                </item>
              </channel>
            </rss>
            """
        )

        issue = parse_feed(feed_xml)

        self.assertEqual(issue.title, "Self-Host Weekly (29 May 2026)")
        self.assertEqual(issue.url, "https://selfh.st/weekly/2026-05-29/")
        self.assertEqual(issue.author, "Ethan Sholly")
        self.assertEqual(issue.slug_date, "2026-05-29")
        self.assertEqual(
            issue.image_url,
            "https://selfh.st/content/images/2026/05/2026-05-29-featured-image.png",
        )

    def test_parse_feed_items_returns_all_visible_weekly_items_for_year(self):
        feed_xml = textwrap.dedent(
            """\
            <rss xmlns:dc="http://purl.org/dc/elements/1.1/">
              <channel>
                <item>
                  <title>Self-Host Weekly (29 May 2026)</title>
                  <link>https://selfh.st/weekly/2026-05-29/</link>
                  <category>Self-Host Weekly</category>
                  <pubDate>Fri, 29 May 2026 07:54:36 -0400</pubDate>
                </item>
                <item>
                  <title>Self-Host Weekly (22 May 2026)</title>
                  <link>https://selfh.st/weekly/2026-05-22/</link>
                  <category>Self-Host Weekly</category>
                  <pubDate>Fri, 22 May 2026 07:54:36 -0400</pubDate>
                </item>
                <item>
                  <title>Self-Host Weekly (26 December 2025)</title>
                  <link>https://selfh.st/weekly/2025-12-26/</link>
                  <category>Self-Host Weekly</category>
                  <pubDate>Fri, 26 Dec 2025 07:54:36 -0500</pubDate>
                </item>
                <item>
                  <title>Other Post</title>
                  <link>https://selfh.st/post/other/</link>
                </item>
              </channel>
            </rss>
            """
        )

        issues = parse_feed_items(feed_xml, year=2026)

        self.assertEqual([issue.slug_date for issue in issues], ["2026-05-29", "2026-05-22"])

    def test_collect_feed_issues_writes_all_visible_rss_items_for_year(self):
        feed_xml = textwrap.dedent(
            """\
            <rss>
              <channel>
                <item>
                  <title>Self-Host Weekly (29 May 2026)</title>
                  <link>https://selfh.st/weekly/2026-05-29/</link>
                  <category>Self-Host Weekly</category>
                  <pubDate>Fri, 29 May 2026 07:54:36 -0400</pubDate>
                </item>
                <item>
                  <title>Self-Host Weekly (22 May 2026)</title>
                  <link>https://selfh.st/weekly/2026-05-22/</link>
                  <category>Self-Host Weekly</category>
                  <pubDate>Fri, 22 May 2026 07:54:36 -0400</pubDate>
                </item>
              </channel>
            </rss>
            """
        )
        article_html = {
            "https://selfh.st/weekly/2026-05-29/": "<article><h2>Weekly Highlights</h2><p>May 29 body.</p></article>",
            "https://selfh.st/weekly/2026-05-22/": "<article><h2>Weekly Highlights</h2><p>May 22 body.</p></article>",
        }

        def fake_fetch(url: str, **kwargs):
            if url == "https://selfh.st/weekly/rss/":
                return feed_xml
            return article_html[url]

        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            with patch("selfhst_weekly_md.archive.fetch_text", side_effect=fake_fetch):
                paths = collect_feed_issues(
                    output_dir=output_dir,
                    year=2026,
                    fetched_at=datetime(2026, 6, 2, 1, 0, 0, tzinfo=timezone.utc),
                )

            self.assertEqual(
                [path.name for path in paths],
                ["2026-05-29.md", "2026-05-22.md"],
            )
            self.assertIn("May 29 body.", (output_dir / "2026-05-29.md").read_text(encoding="utf-8"))
            self.assertIn("May 22 body.", (output_dir / "2026-05-22.md").read_text(encoding="utf-8"))
            self.assertTrue((output_dir / "README.md").exists())

    def test_collect_feed_issues_returns_empty_when_year_has_no_visible_items(self):
        feed_xml = textwrap.dedent(
            """\
            <rss>
              <channel>
                <item>
                  <title>Self-Host Weekly (29 May 2026)</title>
                  <link>https://selfh.st/weekly/2026-05-29/</link>
                  <category>Self-Host Weekly</category>
                  <pubDate>Fri, 29 May 2026 07:54:36 -0400</pubDate>
                </item>
              </channel>
            </rss>
            """
        )

        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            with patch("selfhst_weekly_md.archive.fetch_text", return_value=feed_xml):
                paths = collect_feed_issues(output_dir=output_dir, year=2025)

            self.assertEqual(paths, [])
            self.assertIn(
                "_No issues have been generated yet._",
                (output_dir / "README.md").read_text(encoding="utf-8"),
            )

    def test_collect_issue_includes_dynamic_development_activity(self):
        article_html = textwrap.dedent(
            """\
            <html>
              <head>
                <meta property="og:title" content="Self-Host Weekly (6 March 2026)">
                <meta name="year" content="2026">
                <meta name="uuid" content="activity-uuid">
              </head>
              <body>
                <article>
                  <h2>Newswire</h2>
                  <p>Before activity.</p>
                  <div id="activity-container"></div>
                  <h2>Feedback</h2>
                </article>
              </body>
            </html>
            """
        )
        activity_rows = [
            [
                1,
                "BunkerM",
                0,
                0,
                "https://github.com/bunkeriot/BunkerM",
                "GitHub",
                "MQTT",
                "Internet of Things",
                "v2.0.0",
                "https://github.com/bunkeriot/BunkerM/releases/tag/v2.0.0",
                "",
                "Redesigned interface and hardened authentication",
                "",
                "",
                "",
            ],
            [
                4,
                "Ackify",
                0,
                0,
                "https://github.com/kOlapsis/ackify",
                "GitHub",
                "Security",
                "Security",
                "",
                "",
                "",
                "",
                "Repository",
                "btouchard/ackify-ce",
                "kOlapsis/ackify",
            ],
        ]

        def fake_fetch(url: str, **kwargs):
            if url == "https://selfh.st/weekly/2026-03-06/":
                return article_html
            if url == "https://selfh.st/static/weekly/activity/2026/activity-uuid.json":
                return json.dumps(activity_rows)
            raise AssertionError(f"Unexpected URL: {url}")

        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            with patch("selfhst_weekly_md.archive.fetch_text", side_effect=fake_fetch):
                path = collect_issue(
                    output_dir=output_dir,
                    url="https://selfh.st/weekly/2026-03-06/",
                    fetched_at=datetime(2026, 6, 2, 1, 0, 0, tzinfo=timezone.utc),
                )

            rendered = path.read_text(encoding="utf-8")

        self.assertIn("## Development Activity", rendered)
        self.assertIn("### Software Updates", rendered)
        self.assertIn(
            "- [BunkerM](https://github.com/bunkeriot/BunkerM) [v2.0.0](https://github.com/bunkeriot/BunkerM/releases/tag/v2.0.0) - Redesigned interface and hardened authentication _(MQTT / GitHub)_",
            rendered,
        )
        self.assertIn("### Project Updates", rendered)
        self.assertIn(
            "- [Ackify](https://github.com/kOlapsis/ackify) - Repository: btouchard/ackify-ce -> kOlapsis/ackify _(Security / GitHub)_",
            rendered,
        )

    def test_article_parser_keeps_sections_links_and_bookmarks(self):
        article_html = textwrap.dedent(
            """\
            <html>
              <head>
                <meta property="og:title" content="Self-Host Weekly (29 May 2026)">
                <meta property="og:description" content="Papal stage of the AI debate">
                <meta property="article:published_time" content="2026-05-29T11:54:36.000Z">
              </head>
              <body>
                <article class="ghost-content">
                  <div id="nts-header">SPONSORED BY</div>
                  <h2 id="newsletter-highlights-header">Weekly Highlights</h2>
                  <p>The <a href="https://example.com/gitea?ref=selfh.st">Gitea issue</a> needs updates.</p>
                  <ul>
                    <li><strong>Homarr</strong> reduced memory usage.</li>
                  </ul>
                  <p>After list paragraph.</p>
                  <h2 id="newswire">Newswire</h2>
                  <figure class="kg-card kg-bookmark-card">
                    <a class="kg-bookmark-container" href="https://example.com/story?ref=selfh.st">
                      <div class="kg-bookmark-title">Useful homelab story</div>
                      <div class="kg-bookmark-description">A short description about the story.</div>
                      <div class="kg-bookmark-metadata">
                        <span class="kg-bookmark-author">Example Blog</span>
                        <span class="kg-bookmark-publisher">Ada Lovelace</span>
                      </div>
                      <div class="kg-bookmark-thumbnail">
                        <img src="thumb.png" alt="">
                      </div>
                    </a>
                  </figure>
                  <h2 id="content-spotlight">Content Spotlight</h2>
                  <p>Meet <strong>Tracearr</strong>, a monitoring app.</p>
                  <h2 id="command-line-corner">Command Line Corner</h2>
                  <p>Use <strong>echo "!!"</strong> to save a previous command.</p>
                  <figure class="kg-card kg-code-card">
                    <pre><code class="language-bash">$ echo "!!" &gt; selfhost.sh</code></pre>
                  </figure>
                  <h2 id="executive-sponsors">Executive Sponsors</h2>
                  <ul><li>Sponsor content should be preserved.</li></ul>
                </article>
              </body>
            </html>
            """
        )

        article = parse_article(article_html)

        rendered = build_markdown(
            IssueSource(
                title="Self-Host Weekly (29 May 2026)",
                url="https://selfh.st/weekly/2026-05-29/",
                description="Papal stage of the AI debate",
                author="Ethan Sholly",
                published_at=datetime(2026, 5, 29, 11, 54, 36, tzinfo=timezone.utc),
                image_url="https://selfh.st/content/images/featured.png",
            ),
            article,
            fetched_at=datetime(2026, 6, 2, 1, 0, 0, tzinfo=timezone.utc),
        )

        self.assertIn("## Weekly Highlights", rendered)
        self.assertIn("[Gitea issue](https://example.com/gitea?ref=selfh.st)", rendered)
        self.assertIn("- **Homarr** reduced memory usage.", rendered)
        self.assertIn("- **Homarr** reduced memory usage.\n\nAfter list paragraph.", rendered)
        self.assertIn("| Thumbnail | Story |", rendered)
        self.assertIn(
            '| <img src="thumb.png" alt="Useful homelab story" width="120"> | [Useful homelab story](https://example.com/story?ref=selfh.st)<br>A short description about the story.<br>_(Example Blog / Ada Lovelace)_ |',
            rendered,
        )
        self.assertNotIn("- [Useful homelab story](https://example.com/story?ref=selfh.st)", rendered)
        self.assertIn("## Content Spotlight", rendered)
        self.assertIn("Meet **Tracearr**, a monitoring app.", rendered)
        self.assertIn(
            "![Self-Host Weekly (29 May 2026)](https://selfh.st/content/images/featured.png)",
            rendered,
        )
        self.assertIn("```bash\n$ echo \"!!\" > selfhost.sh\n```", rendered)
        self.assertIn("## Executive Sponsors", rendered)
        self.assertIn("Sponsor content should be preserved", rendered)
        self.assertNotIn("## At a Glance", rendered)
        self.assertNotIn("Sections captured:", rendered)
        self.assertNotIn("Bookmark links captured:", rendered)
        self.assertNotIn("- Source:", rendered)

    def test_article_parser_bounds_sponsor_header_icon(self):
        article_html = textwrap.dedent(
            """\
            <article>
              <div id="nts-header">
                <span>SPONSORED BY</span>
                <img id="nts-logo" src="https://cdn.jsdelivr.net/gh/selfhst/icons@main/svg/1panel.svg">
              </div>
              <div id="nts-body">
                <b>Self-Host Weekly</b> is sponsored by
                <a href="https://github.com/1Panel-dev/1Panel?ref=selfh.st"><strong>1Panel</strong></a>.
              </div>
              <h2>Weekly Highlights</h2>
              <p>Issue body.</p>
            </article>
            """
        )

        rendered = build_markdown(
            IssueSource(
                title="Self-Host Weekly (29 May 2026)",
                url="https://selfh.st/weekly/2026-05-29/",
            ),
            parse_article(article_html),
            fetched_at=datetime(2026, 6, 2, 1, 0, 0, tzinfo=timezone.utc),
        )

        self.assertIn(
            '<img src="https://cdn.jsdelivr.net/gh/selfhst/icons@main/svg/1panel.svg" alt="" width="64">',
            rendered,
        )
        self.assertNotIn(
            "![](https://cdn.jsdelivr.net/gh/selfhst/icons@main/svg/1panel.svg)",
            rendered,
        )

    def test_article_parser_preserves_source_structure_beyond_h2_and_bullets(self):
        article_html = textwrap.dedent(
            """\
            <article>
              <h2>Weekly Highlights</h2>
              <h3>Project details</h3>
              <p>Install steps:</p>
              <ol>
                <li>Pull the image.</li>
                <li>Start the service.</li>
              </ol>
              <figure class="kg-card kg-image-card">
                <img src="https://selfh.st/content/images/example.png" alt="Example dashboard">
              </figure>
            </article>
            """
        )

        rendered = build_markdown(
            IssueSource(
                title="Self-Host Weekly (29 May 2026)",
                url="https://selfh.st/weekly/2026-05-29/",
            ),
            parse_article(article_html),
            fetched_at=datetime(2026, 6, 2, 1, 0, 0, tzinfo=timezone.utc),
        )

        self.assertIn("## Weekly Highlights", rendered)
        self.assertIn("### Project details", rendered)
        self.assertIn("1. Pull the image.", rendered)
        self.assertIn("2. Start the service.", rendered)
        self.assertIn(
            "![Example dashboard](https://selfh.st/content/images/example.png)",
            rendered,
        )

    def test_article_parser_preserves_sponsor_html_card_text(self):
        article_html = textwrap.dedent(
            """\
            <article>
              <div id="nts-body">
                <b>Self-Host Weekly</b> is sponsored by
                <a href="https://github.com/1Panel-dev/1Panel?ref=selfh.st"><strong>1Panel</strong></a>,
                a modern, open source VPS control panel.
                <a href="https://github.com/1Panel-dev/1Panel?ref=selfh.st"><strong>Deploy local LLMs</strong></a>
                from one clean dashboard.
                <a href="https://www.pikapods.com/"><strong>Try it today</a></strong>.
              </div>
              <h2>Weekly Highlights</h2>
              <p>Issue body.</p>
            </article>
            """
        )

        rendered = build_markdown(
            IssueSource(
                title="Self-Host Weekly (29 May 2026)",
                url="https://selfh.st/weekly/2026-05-29/",
            ),
            parse_article(article_html),
            fetched_at=datetime(2026, 6, 2, 1, 0, 0, tzinfo=timezone.utc),
        )

        self.assertIn(
            "**Self-Host Weekly** is sponsored by [**1Panel**](https://github.com/1Panel-dev/1Panel?ref=selfh.st), a modern, open source VPS control panel. [**Deploy local LLMs**](https://github.com/1Panel-dev/1Panel?ref=selfh.st) from one clean dashboard.",
            rendered,
        )
        self.assertIn("[**Try it today**](https://www.pikapods.com/).", rendered)
        self.assertIn("## Weekly Highlights", rendered)

    def test_update_index_sorts_generated_issues_newest_first(self):
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "2026-05-22.md").write_text(
                "---\n"
                "title: Self-Host Weekly (22 May 2026)\n"
                "url: https://selfh.st/weekly/2026-05-22/\n"
                "published_at: 2026-05-22T12:08:32+00:00\n"
                "description: Earlier issue\n"
                "---\n",
                encoding="utf-8",
            )
            (output_dir / "2026-05-29.md").write_text(
                "---\n"
                "title: Self-Host Weekly (29 May 2026)\n"
                "url: https://selfh.st/weekly/2026-05-29/\n"
                "published_at: 2026-05-29T11:54:36+00:00\n"
                "description: Later issue\n"
                "---\n",
                encoding="utf-8",
            )

            update_index(output_dir)

            index = (output_dir / "README.md").read_text(encoding="utf-8")
            first = index.index("2026-05-29")
            second = index.index("2026-05-22")
            self.assertLess(first, second)
            self.assertIn("[2026-05-29 - Self-Host Weekly (29 May 2026)](2026-05-29.md)", index)

    def test_write_issue_does_not_overwrite_existing_file_without_force(self):
        issue = IssueSource(
            title="Self-Host Weekly (29 May 2026)",
            url="https://selfh.st/weekly/2026-05-29/",
            published_at=datetime(2026, 5, 29, 11, 54, 36, tzinfo=timezone.utc),
        )
        article = ParsedArticle(
            sections=[
                Section(
                    "Weekly Highlights",
                    [ContentItem(kind="paragraph", text="Original issue body.")],
                )
            ]
        )

        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            path = write_issue(
                output_dir,
                issue,
                article,
                fetched_at=datetime(2026, 6, 2, 1, 0, 0, tzinfo=timezone.utc),
            )
            first_content = path.read_text(encoding="utf-8")

            article.sections[0].items[0].text = "Changed issue body."
            write_issue(
                output_dir,
                issue,
                article,
                fetched_at=datetime(2026, 6, 3, 1, 0, 0, tzinfo=timezone.utc),
            )

            self.assertEqual(path.read_text(encoding="utf-8"), first_content)


if __name__ == "__main__":
    unittest.main()
