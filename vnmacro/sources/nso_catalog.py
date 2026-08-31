"""NSO (nso.gov.vn) release catalog via its WordPress REST API.

The site is WordPress with ``/wp-json`` open, so there is no need to scrape
listing pages. Releases are tagged:

    727  báo cáo tình hình kinh tế - xã hội (monthly/quarterly report)
    883  chỉ số giá tiêu dùng (CPI)
    876  xuất nhập khẩu
    872  chỉ số sản xuất công nghiệp

Attachments (.docx narrative + .xlsx tables) are children of the post and come
from ``/wp/v2/media?parent=<id>``. When a post has several revisions of the
same document, the rendered page's "Tệp đính kèm" widget is authoritative, so
that is preferred and the media endpoint is the fallback.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, asdict

from .. import http
from ..config import PATHS
from ..util import parse_period, slugify

log = logging.getLogger(__name__)

API = "https://www.nso.gov.vn/wp-json/wp/v2"

TAGS = {
    "monthly_report": 727,
    "cpi": 883,
    "trade": 876,
    "iip": 872,
}

DOC_EXT = re.compile(r"\.(docx?|xlsx?|pdf|zip)$", re.I)


@dataclass
class Release:
    post_id: int
    tag: str
    slug: str
    link: str
    title: str
    wp_date: str                     # site publish date — NOT the reference period
    freq: str | None = None
    ref_period: str | None = None
    period_date: str | None = None
    # The monthly observation the tables describe — for a quarterly issue this
    # is the last month of the quarter, not the first.
    month_ref: str | None = None
    month_date: str | None = None
    attachments: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _clean_title(html_title: str) -> str:
    t = re.sub(r"<[^>]+>", "", html_title or "")
    return (t.replace("&#8211;", "-").replace("&#8217;", "'")
             .replace("&amp;", "&").replace("&nbsp;", " ").strip())


def list_posts(tag: str, *, limit: int | None = None) -> list[Release]:
    """Enumerate every post carrying ``tag``, newest first."""
    tag_id = TAGS[tag]
    out: list[Release] = []
    page, total_pages = 1, None
    while True:
        # WordPress answers 400 (not an empty list) once `page` runs past the
        # end, so the page count is taken from the first response's headers.
        resp = http.get(
            f"{API}/posts",
            params={
                "tags": tag_id, "per_page": 100, "page": page,
                "_fields": "id,slug,link,title,date,modified",
                "orderby": "date", "order": "desc",
            },
        )
        if total_pages is None:
            total_pages = int(resp.headers.get("X-WP-TotalPages", 1) or 1)
            log.info("tag %s: %s posts across %d pages",
                     tag, resp.headers.get("X-WP-Total", "?"), total_pages)
        rows = resp.json()
        if not rows:
            break
        for r in rows:
            title = _clean_title(r["title"]["rendered"])
            # Titles carry the reference period; slugs are the fallback for
            # older migrated posts whose titles were reformatted.
            per = parse_period(title) or parse_period(r["slug"])
            out.append(Release(
                post_id=r["id"], tag=tag, slug=r["slug"], link=r["link"],
                title=title, wp_date=r["date"],
                freq=per["freq"] if per else None,
                ref_period=per["ref_period"] if per else None,
                period_date=per["date"].isoformat() if per else None,
                month_ref=per["month_ref"] if per else None,
                month_date=per["month_date"].isoformat() if per else None,
            ))
            if limit and len(out) >= limit:
                return out
        page += 1
        if page > total_pages:
            break
    return out


def _attachments_from_page(link: str) -> list[dict]:
    """Read the 'Tệp đính kèm' widget — this is what the site actually offers."""
    try:
        html = http.get(link).text
    except Exception as exc:                      # page gone / transient
        log.warning("could not fetch %s: %s", link, exc)
        return []
    block = re.search(
        r'class="file-attachment".*?</ul>', html, re.S | re.I)
    if not block:
        return []
    items = []
    for m in re.finditer(
        r'<a\s+href="([^"]+)"[^>]*>.*?related-files-title">(.*?)</span>',
        block.group(0), re.S | re.I,
    ):
        url, label = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if DOC_EXT.search(url):
            items.append({"url": url, "label": _clean_title(label),
                          "ext": DOC_EXT.search(url).group(1).lower(),
                          "via": "page"})
    return items


def _attachments_from_media(post_id: int) -> list[dict]:
    try:
        rows = http.get_json(
            f"{API}/media",
            params={"parent": post_id, "per_page": 50,
                    "_fields": "id,source_url,title,mime_type"},
        )
    except Exception as exc:
        log.warning("media lookup failed for post %s: %s", post_id, exc)
        return []
    items = []
    for r in rows:
        url = r.get("source_url", "")
        if not DOC_EXT.search(url):
            continue
        items.append({"url": url, "label": _clean_title(r["title"]["rendered"]),
                      "ext": DOC_EXT.search(url).group(1).lower(),
                      "media_id": r["id"], "via": "media"})
    # Duplicate uploads ("...-final.docx" and "...-final-1.docx"): keep the
    # newest media id per extension.
    best: dict[str, dict] = {}
    for it in sorted(items, key=lambda x: x.get("media_id", 0)):
        best[it["ext"]] = it
    return list(best.values())


def resolve_attachments(rel: Release) -> Release:
    rel.attachments = _attachments_from_page(rel.link) or _attachments_from_media(rel.post_id)
    return rel


def raw_path(rel: Release, att: dict):
    period = rel.ref_period or rel.slug[:24]
    name = f"{period}__{slugify(att['label'], 60)}.{att['ext']}"
    return PATHS.raw / "nso" / rel.tag / name.replace("/", "-")


def fetch(rel: Release, *, offline: bool = False) -> Release:
    """Download every attachment of a release into the raw archive.

    ``offline`` reuses whatever is already archived and issues no requests, so
    a parser change can be replayed over years of reports in seconds.
    """
    if not rel.attachments:
        resolve_attachments(rel)
    for att in rel.attachments:
        dest = raw_path(rel, att)
        try:
            att.update(http.download(att["url"], dest, offline=offline))
        except Exception as exc:
            level = log.debug if offline else log.error
            level("download failed %s: %s", att["url"], exc)
            att["error"] = str(exc)
    return rel
