"""Per-destination tailoring: one content item -> one fitting rendition per destination.

Styles by kind of place:
  forum      (Discourse, Lemmy, assisted forums) discussion-first: the post opens
             with the point, ends with a question to the community, no hashtags,
             the link goes last as "more detail"
  article    (Dev.to, Hashnode, Ghost, partner sites) full article, canonical URL
             set to the original, tags in the platform's format
  chat       (Slack) short: bold title, summary cut at a word boundary, link
  feed       (MRSS) title + description, video media first
Then every rendition is cut to the destination's limits at a word boundary,
gets the AI disclosure line where the destination requires one, and carries
notes listing every change made.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from app.connectors import registry
from app.connectors.base import Destination, Media, Rendition

STYLE_BY_CONNECTOR = {
    "discourse": "forum", "lemmy": "forum", "devto": "article", "hashnode": "article", "ghost": "article",
    "webhook": "article", "slack": "chat", "mrss": "feed", "assisted": "forum",
}
# assisted destinations behave like the platform they stand for
ASSISTED_STYLES = {"hackernews": "link_only", "producthunt": "launch", "reddit": "forum", "linkedin_group": "social",
                   "facebook_group": "social", "indiehackers": "forum", "discord": "chat", "quora": "forum"}

DISCLOSURES = {
    "en": "Disclosure: this post was created with the help of AI (AI-generated content).",
    "de": "Hinweis: Dieser Beitrag wurde mit Hilfe von KI erstellt (AI-generated).",
    "fr": "Avertissement : ce message a été créé avec l'aide de l'IA (AI-generated).",
    "es": "Aviso: esta publicación se creó con ayuda de IA (AI-generated).",
    "pt": "Aviso: esta publicação foi criada com a ajuda de IA (AI-generated).",
    "hi": "सूचना: यह पोस्ट AI की सहायता से बनाई गई है (AI-generated).",
    "ta": "அறிவிப்பு: இந்த பதிவு AI உதவியுடன் உருவாக்கப்பட்டது (AI-generated).",
    "ja": "注記：この投稿はAIの支援を受けて作成されました（AI-generated）。",
}
DISCUSSION_PROMPTS = {
    "en": "What has worked for you here? Curious how others in this community approach it.",
    "de": "Welche Erfahrungen habt ihr damit gemacht?",
    "fr": "Quelle est votre expérience à ce sujet ?",
    "es": "¿Qué os ha funcionado a vosotros?",
}


class ContentItem(BaseModel):
    """What a creator wants to distribute, before tailoring."""

    title: str
    body: str = ""
    summary: str | None = None
    link: str | None = Field(None, description="Canonical URL of the original")
    media: list[Media] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)
    promotional: bool | None = None
    ai_generated: bool = False
    language: str | None = "en"
    content_type: str = "article"
    content_id: str | None = None
    audience_tags: list[str] = Field(default_factory=list)


def truncate_words(text: str, limit: int, ellipsis: str = "…") -> str:
    """Cut at the last word boundary that fits, never mid-word."""
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[: max(0, limit - len(ellipsis))]
    space = max(cut.rfind(" "), cut.rfind("\n"))
    if space > limit * 0.5:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-–—\n") + ellipsis


def strip_hashtags(text: str) -> str:
    return re.sub(r"(?<![\w/#])#([A-Za-z][\w-]*)", r"\1", text)


def tag_slug(tag: str) -> str:
    return re.sub(r"[^a-z0-9]", "", tag.lower())


def style_for(destination: Destination) -> str:
    if destination.connector_key == "assisted":
        return ASSISTED_STYLES.get(destination.platform_key, "forum")
    return STYLE_BY_CONNECTOR.get(destination.connector_key, "article")


def tailor(content: ContentItem, destination: Destination) -> tuple[Rendition, list[str]]:
    connector = registry.get(destination.connector_key, check_kill_switch=False)
    spec = connector.surface_spec(destination)
    style = style_for(destination)
    lang = (destination.language or content.language or "en")[:2]
    notes: list[str] = [f"style: {style}"]
    title = content.title.strip()
    body = content.body.strip()
    summary = (content.summary or "").strip() or _first_paragraph(body)
    link = content.link
    tags = list(content.tags)
    canonical = None

    if style == "forum":
        body = strip_hashtags(body)
        if body != content.body.strip():
            notes.append("removed hashtags (forums do not use them)")
        prompt = destination.config.get("discussion_prompt") or DISCUSSION_PROMPTS.get(lang, DISCUSSION_PROMPTS["en"])
        body = f"{body}\n\n{prompt}"
        notes.append("discussion-first framing: added a question for the community")
        tags = []
        if link:
            body += f"\n\nMore detail: {link}"
            notes.append("link moved to the end as 'more detail'")
            link = None if destination.connector_key != "lemmy" else link
    elif style == "article":
        canonical = content.link
        if canonical:
            notes.append(f"canonical URL set to the original ({canonical})")
        if destination.connector_key == "devto":
            tags = [t for t in dict.fromkeys(tag_slug(t) for t in tags) if t][:4]
            notes.append("tags converted to Dev.to format (lowercase, alphanumeric, max 4)")
        elif destination.connector_key == "hashnode":
            tags = [t for t in dict.fromkeys(tag_slug(t) for t in tags) if t][:5]
        body = strip_hashtags(body)
    elif style == "chat":
        body = truncate_words(strip_hashtags(summary), int(destination.config.get("summary_chars", 600)))
        tags = []
        notes.append("chat style: summary only, cut at a word boundary")
    elif style == "feed":
        body = truncate_words(summary, int(spec.get("body_max_chars") or 5000))
        videos = [m for m in content.media if m.type == "video"]
        if not videos:
            notes.append("warning: no video in this item; feed readers expect one")
    elif style == "link_only":
        body = ""
        tags = []
        notes.append("link-only site: title and link, no body")
    elif style == "launch":
        body = truncate_words(summary, 260)
        notes.append("launch style: one-line tagline")
    elif style == "social":
        hashtags = " ".join("#" + re.sub(r"[^A-Za-z0-9]", "", t.title()) for t in tags[:3])
        body = truncate_words(summary, 1200) + (f"\n\n{hashtags}" if hashtags else "")
        notes.append("social style: summary plus CamelCase hashtags")

    mentions = [m for m in content.mentions if destination.connector_key not in ("discourse", "lemmy", "slack")]
    if len(mentions) < len(content.mentions):
        notes.append("dropped @mentions that would not resolve on this platform")

    # AI disclosure
    if content.ai_generated and destination.disclosure_required:
        line = DISCLOSURES.get(lang, DISCLOSURES["en"])
        body = f"{body}\n\n_{line}_" if body else line
        notes.append(f"added AI disclosure line ({lang})")

    # limits, at word boundaries; the disclosure must survive truncation
    title_max = spec.get("title_max_chars")
    if title_max and len(title) > title_max:
        notes.append(f"title cut from {len(title)} to at most {title_max} characters at a word boundary")
        title = truncate_words(title, int(title_max))
    body_max = spec.get("body_max_chars")
    if body_max and len(body) > body_max:
        tail = ""
        if content.ai_generated and destination.disclosure_required:
            tail = "\n\n_" + DISCLOSURES.get(lang, DISCLOSURES["en"]) + "_"
            body = body[: body.rfind(tail)] if tail in body else body
        more = f"\n\nRead the full post: {content.link}" if content.link and style != "link_only" else ""
        room = int(body_max) - len(tail) - len(more)
        body = truncate_words(body, room) + more + tail
        notes.append(f"body cut to {body_max} characters at a word boundary" + (" with a link to the full post" if more else ""))

    # media the destination does not accept is dropped, with a note
    accepted = set(spec.get("content_types") or [])
    media = []
    for m in content.media:
        if accepted and m.type not in accepted and not (m.type == "video" and "video_link" in accepted):
            notes.append(f"dropped {m.type} media not accepted here ({m.url})")
            continue
        media.append(m)
    max_media = spec.get("max_media")
    if max_media is not None and len(media) > max_media:
        notes.append(f"kept the first {max_media} media item(s) of {len(media)}")
        media = media[: int(max_media)]

    lang_note = None
    if destination.language and content.language and destination.language[:2] != content.language[:2]:
        lang_note = f"community language is '{destination.language}', content is '{content.language}' (not translated)"
        notes.append(lang_note)

    rendition = Rendition(
        title=title, body=body, body_format="markdown", link=link, canonical_url=canonical, media=media, tags=tags,
        mentions=mentions, promotional=content.promotional, ai_generated=content.ai_generated,
        language=content.language, content_id=content.content_id, surface=spec.get("surface"),
    )
    return rendition, notes


def _first_paragraph(body: str) -> str:
    for para in re.split(r"\n\s*\n", body):
        p = para.strip()
        if p and not p.startswith("#"):
            return p
    return body[:500]


def tailor_many(content: ContentItem, destinations: list[Destination]) -> list[dict[str, Any]]:
    out = []
    for d in destinations:
        r, notes = tailor(content, d)
        out.append({"destination_id": d.id, "destination": d.name, "rendition": r.model_dump(exclude_none=True),
                    "notes": notes})
    return out
