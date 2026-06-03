"""Fetch selfh.st weekly issues and render structured Markdown."""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import escape as html_escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen

DEFAULT_FEED_URL = "https://selfh.st/weekly/rss/"
DEFAULT_OUTPUT_DIR = Path("newsletters/selfh-st/weekly")
DEFAULT_USER_AGENT = "selfhst-weekly-md/0.1 (+https://selfh.st/weekly/rss/)"
VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
ACTIVITY_GROUPS = {
    1: "Software Updates",
    2: "New Software",
    3: "Directory Additions",
    4: "Project Updates",
}


@dataclass(frozen=True)
class IssueSource:
    title: str
    url: str
    description: str = ""
    author: str = ""
    published_at: datetime | None = None
    image_url: str | None = None

    @property
    def slug_date(self) -> str:
        match = re.search(r"/weekly/(\d{4}-\d{2}-\d{2})/", self.url)
        if match:
            return match.group(1)
        if self.published_at:
            return self.published_at.date().isoformat()
        raise ValueError(f"Cannot derive issue date from URL: {self.url}")


@dataclass
class Bookmark:
    title: str
    url: str
    description: str = ""
    authors: list[str] = field(default_factory=list)
    thumbnail_url: str = ""


@dataclass
class ContentItem:
    kind: str
    text: str = ""
    url: str = ""
    bookmark: Bookmark | None = None
    language: str = ""
    width: int = 0


@dataclass
class Section:
    title: str
    items: list[ContentItem] = field(default_factory=list)
    level: int = 2
    excluded: bool = False


@dataclass
class ParsedArticle:
    title: str = ""
    description: str = ""
    author: str = ""
    published_at: datetime | None = None
    image_url: str | None = None
    activity_url: str = ""
    sections: list[Section] = field(default_factory=list)


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self._title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = _attrs_dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            key = attrs_dict.get("property") or attrs_dict.get("name")
            content = attrs_dict.get("content")
            if key and content:
                self.meta[key] = content

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)

    @property
    def title(self) -> str:
        return _normalize_inline("".join(self._title_parts))


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[Section] = [Section("Overview")]
        self._current_section = self.sections[0]
        self._skip_depth = 0
        self._blackout_depth = 0

        self._capture_kind = ""
        self._capture_tag = ""
        self._capture_parts: list[str] = []
        self._capture_language = ""
        self._capture_heading_level = 2
        self._inline_link_stack: list[str] = []
        self._list_stack: list[str] = []

        self._bookmark_depth = 0
        self._bookmark: Bookmark | None = None
        self._bookmark_capture = ""
        self._bookmark_capture_depth = 0
        self._bookmark_parts: list[str] = []
        self._bookmark_thumbnail_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = _attrs_dict(attrs)
        classes = set(attrs_dict.get("class", "").split())

        if self._blackout_depth:
            self._blackout_depth += 1
            return
        if tag in {"script", "style"}:
            self._blackout_depth = 1
            return

        if self._skip_depth:
            if tag not in VOID_TAGS:
                self._skip_depth += 1
            return
        if _is_skip_container(attrs_dict, classes):
            self._skip_depth = 1
            return

        if tag == "div" and attrs_dict.get("id") == "activity-container":
            self._append_activity_placeholder()
            return

        if self._bookmark_depth:
            self._handle_bookmark_start(tag, attrs_dict, classes)
            return
        if tag in {"ul", "ol"}:
            self._list_stack.append(tag)
            return
        if tag == "figure" and "kg-bookmark-card" in classes:
            self._bookmark_depth = 1
            self._bookmark = Bookmark(title="", url="")
            return

        if tag in {"h2", "h3", "h4", "h5", "h6"}:
            self._start_capture("heading", tag, heading_level=int(tag[1]))
        elif tag == "div" and attrs_dict.get("id") == "nts-body":
            self._start_capture("paragraph", tag)
        elif tag == "p":
            self._start_capture("paragraph", tag)
        elif tag == "li":
            kind = "ordered" if self._list_stack and self._list_stack[-1] == "ol" else "bullet"
            self._start_capture(kind, tag)
        elif tag == "blockquote":
            self._start_capture("quote", tag)
        elif tag == "pre":
            self._start_capture("code", tag)
        elif tag == "img":
            self._append_image(attrs_dict)
        elif self._capture_kind:
            self._handle_inline_start(tag, attrs_dict, classes)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._blackout_depth:
            self._blackout_depth -= 1
            return
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if self._bookmark_depth:
            self._handle_bookmark_end(tag)
            return

        if self._capture_kind and self._capture_kind != "code":
            self._handle_inline_end(tag)

        if self._capture_kind and tag == self._capture_tag:
            self._finish_capture()
        elif tag in {"ul", "ol"} and self._list_stack:
            self._list_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._blackout_depth or self._skip_depth:
            return
        if self._bookmark_depth:
            if self._bookmark_capture:
                self._bookmark_parts.append(data)
            return
        if self._capture_kind:
            self._capture_parts.append(data)

    def _start_capture(self, kind: str, tag: str, heading_level: int = 2) -> None:
        if self._capture_kind:
            return
        self._capture_kind = kind
        self._capture_tag = tag
        self._capture_parts = []
        self._capture_language = ""
        self._capture_heading_level = heading_level
        self._inline_link_stack = []

    def _finish_capture(self) -> None:
        kind = self._capture_kind
        text = "".join(self._capture_parts)
        language = self._capture_language

        self._capture_kind = ""
        self._capture_tag = ""
        self._capture_parts = []
        self._capture_language = ""
        heading_level = self._capture_heading_level
        self._capture_heading_level = 2
        self._inline_link_stack = []

        if kind == "code":
            normalized = text.strip("\n")
        else:
            normalized = _normalize_inline(text)
        if not normalized:
            return

        if kind == "heading":
            section = Section(normalized, level=heading_level)
            self.sections.append(section)
            self._current_section = section
            return

        if self._current_section.excluded:
            return
        self._current_section.items.append(
            ContentItem(kind=kind, text=normalized, language=language)
        )

    def _append_image(self, attrs_dict: dict[str, str]) -> None:
        src = attrs_dict.get("src", "")
        if not src or self._current_section.excluded:
            return
        width = 64 if attrs_dict.get("id") == "nts-logo" else 0
        self._current_section.items.append(
            ContentItem(
                kind="image",
                text=attrs_dict.get("alt", ""),
                url=src,
                width=width,
            )
        )

    def _append_activity_placeholder(self) -> None:
        if self._current_section.title == "Development Activity":
            return
        section = Section("Development Activity")
        self.sections.append(section)
        self._current_section = section

    def _handle_inline_start(
        self, tag: str, attrs_dict: dict[str, str], classes: set[str]
    ) -> None:
        if self._capture_kind == "code":
            if tag == "code":
                language = _language_from_classes(classes)
                if language:
                    self._capture_language = language
            return
        if tag == "a":
            self._capture_parts.append("[")
            self._inline_link_stack.append(attrs_dict.get("href", ""))
        elif tag in {"strong", "b"}:
            self._capture_parts.append("**")
        elif tag in {"em", "i"}:
            self._capture_parts.append("*")
        elif tag == "code":
            self._capture_parts.append("`")
            language = _language_from_classes(classes)
            if language:
                self._capture_language = language

    def _handle_inline_end(self, tag: str) -> None:
        if tag == "a" and self._inline_link_stack:
            href = self._inline_link_stack.pop()
            self._capture_parts.append(f"]({href})" if href else "]")
        elif tag in {"strong", "b"}:
            self._capture_parts.append("**")
        elif tag in {"em", "i"}:
            self._capture_parts.append("*")
        elif tag == "code":
            self._capture_parts.append("`")

    def _handle_bookmark_start(
        self, tag: str, attrs_dict: dict[str, str], classes: set[str]
    ) -> None:
        if self._bookmark is None:
            return
        if tag == "a" and not self._bookmark.url:
            self._bookmark.url = attrs_dict.get("href", "")
        if (
            tag == "img"
            and self._bookmark_thumbnail_depth
            and not self._bookmark.thumbnail_url
        ):
            self._bookmark.thumbnail_url = attrs_dict.get("src", "")

        field_name = ""
        if "kg-bookmark-title" in classes:
            field_name = "title"
        elif "kg-bookmark-description" in classes:
            field_name = "description"
        elif "kg-bookmark-author" in classes:
            field_name = "author"
        elif "kg-bookmark-publisher" in classes:
            field_name = "publisher"

        if "kg-bookmark-thumbnail" in classes:
            self._bookmark_thumbnail_depth = 1
        elif self._bookmark_thumbnail_depth and tag not in VOID_TAGS:
            self._bookmark_thumbnail_depth += 1

        if tag not in VOID_TAGS:
            self._bookmark_depth += 1

        if field_name and not self._bookmark_capture:
            self._bookmark_capture = field_name
            self._bookmark_capture_depth = 1
            self._bookmark_parts = []
        elif self._bookmark_capture and tag not in VOID_TAGS:
            self._bookmark_capture_depth += 1

    def _handle_bookmark_end(self, tag: str) -> None:
        if self._bookmark_capture:
            self._bookmark_capture_depth -= 1
            if self._bookmark_capture_depth == 0 and self._bookmark:
                value = _normalize_inline("".join(self._bookmark_parts))
                if value:
                    if self._bookmark_capture == "title":
                        self._bookmark.title = value
                    elif self._bookmark_capture == "description":
                        self._bookmark.description = value
                    else:
                        self._bookmark.authors.append(value)
                self._bookmark_capture = ""
                self._bookmark_parts = []

        if self._bookmark_thumbnail_depth:
            self._bookmark_thumbnail_depth -= 1

        self._bookmark_depth -= 1
        if self._bookmark_depth == 0 and self._bookmark:
            if (
                self._bookmark.title
                and self._bookmark.url
                and not self._current_section.excluded
            ):
                self._current_section.items.append(
                    ContentItem(kind="bookmark", bookmark=self._bookmark)
                )
            self._bookmark = None
            self._bookmark_thumbnail_depth = 0


def parse_feed(feed_xml: str) -> IssueSource:
    """Return the first Self-Host Weekly item from a selfh.st RSS feed."""
    issues = parse_feed_items(feed_xml)
    if not issues:
        raise ValueError("No Self-Host Weekly item found in feed")
    return issues[0]


def parse_feed_items(feed_xml: str, year: int | None = None) -> list[IssueSource]:
    """Return every Self-Host Weekly item visible in a selfh.st RSS feed."""
    root = ET.fromstring(feed_xml)
    issues: list[IssueSource] = []
    items = root.findall("./channel/item")
    for item in items:
        title = _xml_text(item, "title")
        link = _xml_text(item, "link")
        categories = [_clean_text(child.text or "") for child in item.findall("category")]
        if not _is_weekly_item(title, link, categories):
            continue

        published = _parse_rfc2822_datetime(_xml_text(item, "pubDate"))
        image_url = None
        for child in item:
            if child.tag.endswith("content") and child.attrib.get("medium") == "image":
                image_url = child.attrib.get("url")
                break

        issue = IssueSource(
            title=title,
            url=link,
            description=_xml_text(item, "description"),
            author=_xml_namespaced_text(item, "creator"),
            published_at=published,
            image_url=image_url,
        )
        if year is None or _issue_year(issue) == year:
            issues.append(issue)
    return issues


def parse_article(html: str) -> ParsedArticle:
    meta_parser = _MetaParser()
    meta_parser.feed(html)
    meta = meta_parser.meta

    article_parser = _ArticleParser()
    article_parser.feed(_extract_article_html(html))

    sections = [
        section
        for section in article_parser.sections
        if section.items or section.title != "Overview"
    ]
    return ParsedArticle(
        title=meta.get("og:title") or meta.get("twitter:title") or meta_parser.title,
        description=meta.get("og:description") or meta.get("twitter:description") or "",
        author=meta.get("author") or meta.get("twitter:data1") or "",
        published_at=_parse_iso_datetime(meta.get("article:published_time", "")),
        image_url=meta.get("og:image") or meta.get("twitter:image"),
        activity_url=_activity_url(meta, sections),
        sections=sections,
    )


def issue_from_article(url: str, article: ParsedArticle) -> IssueSource:
    return IssueSource(
        title=article.title or f"Self-Host Weekly ({_date_from_url(url)})",
        url=url,
        description=article.description,
        author=article.author,
        published_at=article.published_at,
        image_url=article.image_url,
    )


def build_markdown(
    issue: IssueSource,
    article: ParsedArticle,
    fetched_at: datetime | None = None,
) -> str:
    fetched = fetched_at or datetime.now(timezone.utc)
    title = issue.title or article.title
    description = issue.description or article.description
    published = issue.published_at or article.published_at
    author = issue.author or article.author
    image_url = issue.image_url or article.image_url

    lines: list[str] = [
        "---",
        f"title: {_yaml_scalar(title)}",
        f"url: {_yaml_scalar(issue.url)}",
        f"published_at: {_yaml_scalar(_isoformat(published))}",
        f"fetched_at: {_yaml_scalar(_isoformat(fetched))}",
        f"description: {_yaml_scalar(description)}",
    ]
    if author:
        lines.append(f"author: {_yaml_scalar(author)}")
    if image_url:
        lines.append(f"image: {_yaml_scalar(image_url)}")
    lines.extend(["---", "", f"# {title}", ""])

    if description:
        lines.extend([f"> {description}", ""])

    if image_url:
        lines.extend([f"![{title}]({image_url})", ""])

    for section in article.sections:
        lines.extend(_render_section(section))

    return "\n".join(lines).rstrip() + "\n"


def write_issue(
    output_dir: Path,
    issue: IssueSource,
    article: ParsedArticle,
    fetched_at: datetime | None = None,
    overwrite: bool = False,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{issue.slug_date}.md"
    if output_path.exists() and not overwrite:
        update_index(output_dir)
        return output_path
    output_path.write_text(build_markdown(issue, article, fetched_at), encoding="utf-8")
    update_index(output_dir)
    return output_path


def update_index(output_dir: Path, index_name: str = "README.md") -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for markdown_path in output_dir.glob("*.md"):
        if markdown_path.name == index_name:
            continue
        frontmatter = _read_frontmatter(markdown_path)
        if not frontmatter:
            continue
        entries.append(
            {
                "path": markdown_path,
                "date": markdown_path.stem,
                "title": frontmatter.get("title", markdown_path.stem),
                "url": frontmatter.get("url", ""),
                "published_at": frontmatter.get("published_at", ""),
                "description": frontmatter.get("description", ""),
            }
        )

    entries.sort(key=lambda item: (item["date"], item["published_at"]), reverse=True)
    lines = [
        "# Self-Host Weekly Archive",
        "",
        "Generated Markdown archive from the public selfh.st weekly RSS feed.",
        "",
    ]
    if entries:
        for entry in entries:
            description = f" - {entry['description']}" if entry["description"] else ""
            lines.append(
                f"- [{entry['date']} - {entry['title']}]({entry['path'].name}){description}"
            )
    else:
        lines.append("_No issues have been generated yet._")
    lines.append("")

    index_path = output_dir / index_name
    index_path.write_text("\n".join(lines), encoding="utf-8")
    return index_path


def fetch_text(url: str, user_agent: str = DEFAULT_USER_AGENT, timeout: int = 30) -> str:
    try:
        return _fetch_text_once(url, user_agent=user_agent, timeout=timeout)
    except URLError as error:
        if not _is_proxy_tunnel_forbidden(error):
            raise
        print(
            f"Default proxy rejected {url}; retrying without proxy.",
            file=sys.stderr,
        )
        return _fetch_text_once(
            url,
            user_agent=user_agent,
            timeout=timeout,
            ignore_proxy=True,
        )


def _fetch_text_once(
    url: str,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: int = 30,
    ignore_proxy: bool = False,
) -> str:
    request = Request(url, headers={"User-Agent": user_agent})
    opener = build_opener(ProxyHandler({})) if ignore_proxy else None
    response_context = opener.open(request, timeout=timeout) if opener else urlopen(request, timeout=timeout)
    with response_context as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, "replace")


def collect_issue(
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    url: str = "",
    feed_url: str = DEFAULT_FEED_URL,
    fetched_at: datetime | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
    overwrite: bool = False,
) -> Path:
    if url:
        existing_path = output_path_for_url(output_dir, url)
        if existing_path.exists() and not overwrite:
            update_index(output_dir)
            return existing_path
        article_html = fetch_text(url, user_agent=user_agent)
        article = parse_article(article_html)
        hydrate_activity(article, user_agent=user_agent)
        issue = issue_from_article(url, article)
    else:
        issue = parse_feed(fetch_text(feed_url, user_agent=user_agent))
        article = parse_article(fetch_text(issue.url, user_agent=user_agent))
        hydrate_activity(article, user_agent=user_agent)
    return write_issue(output_dir, issue, article, fetched_at=fetched_at, overwrite=overwrite)


def collect_feed_issues(
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    feed_url: str = DEFAULT_FEED_URL,
    year: int | None = None,
    fetched_at: datetime | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
    overwrite: bool = False,
) -> list[Path]:
    issues = parse_feed_items(fetch_text(feed_url, user_agent=user_agent), year=year)

    output_dir.mkdir(parents=True, exist_ok=True)
    if not issues:
        update_index(output_dir)
        return []

    output_paths: list[Path] = []
    for issue in issues:
        output_path = output_dir / f"{issue.slug_date}.md"
        if output_path.exists() and not overwrite:
            output_paths.append(output_path)
            continue
        article = parse_article(fetch_text(issue.url, user_agent=user_agent))
        hydrate_activity(article, user_agent=user_agent)
        output_paths.append(
            write_issue(
                output_dir,
                issue,
                article,
                fetched_at=fetched_at,
                overwrite=overwrite,
            )
        )
    update_index(output_dir)
    return output_paths


def output_path_for_url(output_dir: Path, url: str) -> Path:
    return output_dir / f"{IssueSource(title='', url=url).slug_date}.md"


def hydrate_activity(
    article: ParsedArticle,
    user_agent: str = DEFAULT_USER_AGENT,
) -> None:
    if not article.activity_url:
        return
    section = _find_section(article.sections, "Development Activity")
    if section is None:
        return

    payload = json.loads(fetch_text(article.activity_url, user_agent=user_agent))
    if not isinstance(payload, list):
        raise ValueError(f"Unexpected activity payload from {article.activity_url}")
    section.items = _activity_section_items(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Self-Host Weekly and save the article as structured Markdown."
    )
    parser.add_argument(
        "--url",
        default="",
        help="Specific newsletter URL. If omitted, the latest weekly RSS item is used.",
    )
    parser.add_argument("--feed-url", default=DEFAULT_FEED_URL)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Archive every Self-Host Weekly item currently visible in the RSS feed.",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="When used with --all, archive only RSS-visible issues from this year.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Markdown output directory. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--fetched-at",
        default="",
        help="ISO timestamp override for reproducible output.",
    )
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing issue file instead of leaving it unchanged.",
    )
    args = parser.parse_args(argv)

    if args.url and args.all:
        parser.error("--url and --all cannot be used together")

    fetched_at = _parse_iso_datetime(args.fetched_at) if args.fetched_at else None
    if args.all:
        output_paths = collect_feed_issues(
            output_dir=args.output_dir,
            feed_url=args.feed_url,
            year=args.year,
            fetched_at=fetched_at,
            user_agent=args.user_agent,
            overwrite=args.force,
        )
        for output_path in output_paths:
            print(output_path)
    else:
        output_path = collect_issue(
            output_dir=args.output_dir,
            url=args.url,
            feed_url=args.feed_url,
            fetched_at=fetched_at,
            user_agent=args.user_agent,
            overwrite=args.force,
        )
        print(output_path)
    return 0


def _render_section(section: Section) -> list[str]:
    if section.excluded:
        return []
    level = max(2, min(section.level, 6))
    lines = [f"{'#' * level} {section.title}", ""]
    if _is_newswire_bookmark_table(section):
        lines.extend(_render_bookmark_table(section.items))
        return lines

    previous_kind = ""
    ordered_index = 1
    for item in section.items:
        if previous_kind in {"bullet", "ordered"} and item.kind != previous_kind and lines[-1] != "":
            lines.append("")
        if item.kind != "ordered":
            ordered_index = 1
        if item.kind == "paragraph":
            lines.extend([item.text, ""])
        elif item.kind == "bullet":
            lines.append(f"- {item.text}")
        elif item.kind == "ordered":
            lines.append(f"{ordered_index}. {item.text}")
            ordered_index += 1
        elif item.kind == "quote":
            lines.extend([_render_quote(item.text), ""])
        elif item.kind == "code":
            lines.extend([_render_code(item.text, item.language), ""])
        elif item.kind == "image":
            lines.extend([_render_image(item), ""])
        elif item.kind == "bookmark" and item.bookmark:
            lines.append(_render_bookmark(item.bookmark))
        elif item.kind == "subheading":
            if lines[-1] != "":
                lines.append("")
            subheading_level = min(level + 1, 6)
            lines.extend([f"{'#' * subheading_level} {item.text}", ""])
        previous_kind = item.kind
    if lines[-1] != "":
        lines.append("")
    return lines


def _render_bookmark(bookmark: Bookmark) -> str:
    description = bookmark.description
    metadata = f" {_bookmark_metadata(bookmark)}" if bookmark.authors else ""
    suffix = f" - {description}" if description else ""
    return f"- [{bookmark.title}]({bookmark.url}){suffix}{metadata}"


def _is_newswire_bookmark_table(section: Section) -> bool:
    return (
        section.title == "Newswire"
        and bool(section.items)
        and all(item.kind == "bookmark" and item.bookmark for item in section.items)
        and any(item.bookmark and item.bookmark.thumbnail_url for item in section.items)
    )


def _render_bookmark_table(items: list[ContentItem]) -> list[str]:
    lines = ["| Thumbnail | Story |", "| --- | --- |"]
    for item in items:
        if not item.bookmark:
            continue
        bookmark = item.bookmark
        thumbnail = ""
        if bookmark.thumbnail_url:
            thumbnail = _render_html_image(
                bookmark.thumbnail_url,
                bookmark.title,
                width=120,
            )
        lines.append(
            f"| {_table_cell(thumbnail)} | {_table_cell(_render_bookmark_story(bookmark))} |"
        )
    lines.append("")
    return lines


def _render_bookmark_story(bookmark: Bookmark) -> str:
    parts = [f"[{bookmark.title}]({bookmark.url})"]
    if bookmark.description:
        parts.append(bookmark.description)
    metadata = _bookmark_metadata(bookmark)
    if metadata:
        parts.append(metadata)
    return "<br>".join(parts)


def _bookmark_metadata(bookmark: Bookmark) -> str:
    if not bookmark.authors:
        return ""
    return "_(" + " / ".join(dict.fromkeys(bookmark.authors)) + ")_"


def _table_cell(value: str) -> str:
    return value.replace("\n", "<br>").replace("|", "\\|")


def _render_image(item: ContentItem) -> str:
    if item.width:
        return _render_html_image(item.url, item.text, item.width)
    return f"![{item.text}]({item.url})"


def _render_html_image(url: str, alt: str, width: int) -> str:
    src = html_escape(url, quote=True)
    escaped_alt = html_escape(alt, quote=True)
    return f'<img src="{src}" alt="{escaped_alt}" width="{width}">'


def _render_quote(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines())


def _render_code(text: str, language: str) -> str:
    return f"```{language}\n{text}\n```"


def _activity_url(meta: dict[str, str], sections: list[Section]) -> str:
    if _find_section(sections, "Development Activity") is None:
        return ""
    year = meta.get("year", "")
    uuid = meta.get("uuid", "")
    if not year or not uuid:
        return ""
    return f"https://selfh.st/static/weekly/activity/{year}/{uuid}.json"


def _find_section(sections: list[Section], title: str) -> Section | None:
    for section in sections:
        if section.title == title:
            return section
    return None


def _activity_section_items(rows: list[object]) -> list[ContentItem]:
    grouped: dict[int, list[str]] = {key: [] for key in ACTIVITY_GROUPS}
    for row in rows:
        if not isinstance(row, list) or not row:
            continue
        activity_type = _activity_int(row, 0)
        if activity_type not in grouped:
            continue
        rendered = _render_activity_row(activity_type, row)
        if rendered:
            grouped[activity_type].append(rendered)

    items: list[ContentItem] = []
    for activity_type, title in ACTIVITY_GROUPS.items():
        bullets = grouped[activity_type]
        if not bullets:
            continue
        items.append(ContentItem(kind="subheading", text=title))
        items.extend(ContentItem(kind="bullet", text=bullet) for bullet in bullets)
    return items


def _render_activity_row(activity_type: int, row: list[object]) -> str:
    project = _activity_value(row, 1)
    project_url = _activity_value(row, 4)
    title = _markdown_link(project, project_url)
    metadata = _activity_metadata(row, activity_type)

    if activity_type == 1:
        version = _activity_value(row, 8)
        version_url = _activity_value(row, 9)
        description = _activity_value(row, 11)
        if version:
            title = f"{title} {_markdown_link(version, version_url)}"
        if description:
            title = f"{title} - {description}"
        return f"{title}{metadata}"

    if activity_type in {2, 3}:
        description = _activity_value(row, 11)
        if description:
            title = f"{title} - {description}"
        return f"{title}{metadata}"

    if activity_type == 4:
        change = _activity_value(row, 12)
        old_value = _activity_value(row, 13)
        new_value = _activity_value(row, 14)
        if change and (old_value or new_value):
            title = f"{title} - {change}: {old_value} -> {new_value}"
        elif change:
            title = f"{title} - {change}"
        return f"{title}{metadata}"

    return ""


def _activity_metadata(row: list[object], activity_type: int) -> str:
    parts = [
        _activity_value(row, 6),
        _activity_value(row, 5),
    ]
    if _activity_bool(row, 2):
        parts.append("Editor's Pick")
    if _activity_bool(row, 3):
        parts.append("AI-Assisted")
    if activity_type == 1 and _activity_bool(row, 10):
        parts.append("Breaking Change")

    values = list(dict.fromkeys(part for part in parts if part))
    return f" _({' / '.join(values)})_" if values else ""


def _activity_value(row: list[object], index: int) -> str:
    if index >= len(row) or row[index] is None:
        return ""
    return _normalize_inline(str(row[index]))


def _activity_int(row: list[object], index: int) -> int:
    try:
        return int(_activity_value(row, index))
    except ValueError:
        return 0


def _activity_bool(row: list[object], index: int) -> bool:
    value = _activity_value(row, index).lower()
    return value in {"1", "true", "yes"}


def _markdown_link(text: str, url: str) -> str:
    if text and url:
        return f"[{text}]({url})"
    return text or url


def _extract_article_html(html: str) -> str:
    match = re.search(r"<article\b", html, flags=re.IGNORECASE)
    if not match:
        return html
    end = html.lower().find("</article>", match.start())
    if end == -1:
        return html[match.start() :]
    return html[match.start() : end + len("</article>")]


def _is_skip_container(attrs_dict: dict[str, str], classes: set[str]) -> bool:
    return False


def _is_weekly_item(title: str, link: str, categories: Iterable[str]) -> bool:
    return (
        "/weekly/" in link
        or title.startswith("Self-Host Weekly")
        or "Self-Host Weekly" in set(categories)
    )


def _issue_year(issue: IssueSource) -> int | None:
    if issue.published_at:
        return issue.published_at.year
    match = re.search(r"/weekly/(\d{4})-\d{2}-\d{2}/", issue.url)
    return int(match.group(1)) if match else None


def _xml_text(parent: ET.Element, tag_name: str) -> str:
    child = parent.find(tag_name)
    return _clean_text(child.text if child is not None and child.text else "")


def _xml_namespaced_text(parent: ET.Element, local_name: str) -> str:
    for child in parent:
        if child.tag.endswith(f"}}{local_name}") or child.tag == local_name:
            return _clean_text(child.text or "")
    return ""


def _attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {key: value or "" for key, value in attrs}


def _language_from_classes(classes: set[str]) -> str:
    for class_name in classes:
        if class_name.startswith("language-"):
            return class_name.removeprefix("language-")
    return ""


def _normalize_inline(text: str) -> str:
    normalized = " ".join(text.split())
    normalized = normalized.replace("[ ", "[").replace(" ](", "](")
    normalized = re.sub(r"\[\*\*([^\]]+)\]\(([^)]+)\)\*\*", r"[**\1**](\2)", normalized)
    normalized = re.sub(r"\[\*([^\]]+)\]\(([^)]+)\)\*", r"[*\1*](\2)", normalized)
    return normalized.strip()


def _clean_text(text: str) -> str:
    return _normalize_inline(text)


def _parse_rfc2822_datetime(value: str) -> datetime | None:
    if not value:
        return None
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _isoformat(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _yaml_scalar(value: str) -> str:
    return json.dumps(value or "", ensure_ascii=False)


def _read_frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        return {}
    values: dict[str, str] = {}
    for line in lines[1:]:
        if line == "---":
            break
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        value = raw_value.strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value.strip('"')
        values[key.strip()] = value
    return values


def _date_from_url(url: str) -> str:
    match = re.search(r"/weekly/(\d{4}-\d{2}-\d{2})/", url)
    return match.group(1) if match else "unknown date"


if __name__ == "__main__":
    raise SystemExit(main())
