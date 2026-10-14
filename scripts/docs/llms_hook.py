"""Publish machine-readable copies of the guide alongside the rendered site.

An agent reading documentation wants the prose, not the navigation shell, the
search worker and the theme's CSS. Three artifacts come out of every build:

``/llms.txt``
    An index in the `llms.txt <https://llmstxt.org/>`_ shape: what this site is,
    then every page as a link with a one-line summary, in navigation order. Small
    enough to read first and decide what else to fetch.

``/llms-full.txt``
    Every page concatenated, each under its own heading and canonical URL. One
    fetch for the whole manual when the reader has the context budget for it.

``<any page>.md``
    The Markdown source of that page, at the page's own URL plus ``.md``. Lets a
    reader that already found a page take the clean copy instead of parsing HTML.

All three are generated, never hand-maintained, so they cannot drift from the
pages. Nothing here changes the rendered site.
"""
from __future__ import annotations

import re
from pathlib import Path

# Pages are written for people, so they open with an H1 and a sentence or two
# saying who does this and when. That sentence is the summary; nothing separate
# has to be maintained for the index.
_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_FIGURE = re.compile(r"<figure markdown>.*?</figure>", re.DOTALL)
_ADMONITION_MARKER = re.compile(r"^!!!\s+\w+(?:\s+\"[^\"]*\")?\s*$", re.MULTILINE)

_nav_pages: list = []


def on_nav(nav, config, files):
    """Remember reading order. The nav is the author's ordering; a directory
    walk is not, and an index that lists Troubleshooting before Getting Started
    is worse than no index."""
    global _nav_pages
    _nav_pages = [page for page in nav.pages if page.file.src_path.endswith(".md")]
    return nav


def _title(markdown: str, fallback: str) -> str:
    found = _H1.search(markdown)
    return found.group(1) if found else fallback


def _summary(markdown: str) -> str:
    """The page's opening sentence, flattened to one line.

    Skips the H1 and anything that is not prose: a figure, an admonition
    marker, a table row, a list bullet. Stops at the first blank line after
    prose starts, so this is the lede paragraph rather than the whole page.
    """
    body = _FIGURE.sub("", markdown)
    body = _ADMONITION_MARKER.sub("", body)
    collected: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            if collected:
                break
            continue
        # A bullet needs the space: `**Pending Order** is the one screen…` is
        # prose that happens to open in bold, and dropping it took the summary
        # from the middle of the paragraph.
        is_structure = (
            line.startswith(("#", "|", ">", "<", "```", "==="))
            or re.match(r"^[-*+]\s", line)
            or re.match(r"^\d+\.\s", line)
        )
        if is_structure:
            if collected:
                break
            continue
        collected.append(line)
    text = " ".join(collected)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)   # links -> their text
    text = re.sub(r"[*`]", "", text)                        # bold, code ticks
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 300:
        cut = text[:300].rsplit(". ", 1)[0]
        text = (cut + ".") if cut else text[:300].rstrip() + "…"
    return text


def _page_url(config, page) -> str:
    return config["site_url"].rstrip("/") + "/" + page.url


def on_post_build(config):
    docs_dir = Path(config["docs_dir"])
    site_dir = Path(config["site_dir"])
    site_url = config["site_url"].rstrip("/")

    entries = []
    for page in _nav_pages:
        source = docs_dir / page.file.src_path
        if not source.is_file():
            continue
        markdown = source.read_text(encoding="utf-8")
        entries.append({
            "page": page,
            "src": page.file.src_path,
            "url": _page_url(config, page),
            "title": _title(markdown, page.title or page.file.src_path),
            "summary": _summary(markdown),
            "markdown": markdown,
        })

        # The page's own Markdown, one URL along from the page itself.
        target = site_dir / (page.file.src_path if page.file.src_path != "index.md"
                             else "index.md")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")

    _write_index(site_dir, site_url, config, entries)
    _write_full(site_dir, site_url, config, entries)


def _section_of(src_path: str) -> str:
    head = src_path.split("/")[0]
    if head.endswith(".md"):
        return "Start here"
    return {
        "getting-started": "Getting started",
        "selling": "Selling",
        "inventory": "Inventory",
        "compliance": "Compliance",
        "channels": "Online channels",
        "consignment": "Consignment",
        "distributors": "Distributors",
        "reports": "Reports",
        "settings": "Settings",
        "reference": "Reference",
        "ai": "For AI agents",
    }.get(head, head.replace("-", " ").capitalize())


def _write_index(site_dir: Path, site_url: str, config, entries) -> None:
    lines = [
        f"# {config['site_name']}",
        "",
        f"> {config['site_description']}",
        "",
        "This is the staff manual for a single-store federal firearms licensee "
        "running a POS built on ERPNext. It covers selling at the counter, "
        "receiving inventory, the ATF acquisition-and-disposition record, Form "
        "4473 and NICS, the WooCommerce and GunBroker sales channels, outbound "
        "consignment to partner dealers, distributor drop-ship, and the reports "
        "a manager reads.",
        "",
        "Every page is also available as Markdown at its own URL plus `.md`. "
        f"The whole manual as one file is at {site_url}/llms-full.txt.",
        "",
        "Anything describing a firearm transfer, the bound book, Form 4473 or a "
        "background check states a legal duty as well as a workflow. Read those "
        "pages as constraints, not as options.",
        "",
    ]
    current = None
    for entry in entries:
        section = _section_of(entry["src"])
        if section != current:
            if current is not None:
                lines.append("")
            lines += [f"## {section}", ""]
            current = section
        summary = f": {entry['summary']}" if entry["summary"] else ""
        lines.append(f"- [{entry['title']}]({entry['url']}){summary}")
    lines.append("")
    (site_dir / "llms.txt").write_text("\n".join(lines), encoding="utf-8")


def _write_full(site_dir: Path, site_url: str, config, entries) -> None:
    lines = [
        f"# {config['site_name']}",
        "",
        f"> {config['site_description']}",
        "",
        f"The complete manual, {len(entries)} pages in reading order. Generated "
        f"from {site_url}/ on every build; the index is at {site_url}/llms.txt.",
        "",
        "---",
        "",
    ]
    for entry in entries:
        lines += [
            f"<!-- source: {entry['src']} -->",
            f"# {entry['title']}",
            "",
            f"Canonical URL: {entry['url']}",
            "",
        ]
        # Demote the page's own H1: it is already the heading above, and two of
        # them in one file makes the outline lie about the structure.
        body = _H1.sub(lambda m: f"## {m.group(1)}", entry["markdown"], count=1)
        body = re.sub(r"^#(?=\s)", "##", body, flags=re.MULTILINE)
        lines += [body.strip(), "", "---", ""]
    (site_dir / "llms-full.txt").write_text("\n".join(lines), encoding="utf-8")
