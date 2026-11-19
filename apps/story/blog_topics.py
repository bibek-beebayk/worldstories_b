"""Choosing blog topics for a post.

`suggest_topics_ai` goes through the same generation pipeline as the excerpt
(ai_generation.generate), asking for slugs from the closed topic list only.
`suggest_topics_heuristic` is a keyword fallback for when no API key is
configured — a rough first pass for an editor to review, labelled as such.

Nothing here writes topics by itself: callers decide (the management command is
a dry run unless --apply; the admin action applies for the one post it is run on).
"""

import re
from dataclasses import dataclass, field

from django.utils.html import strip_tags

from .ai_generation import MAX_CONTENT_CHARS, GenerationError, generate
from .models import Blog, BlogTopic, PromptSettings

MAX_TOPICS_PER_POST = 2


@dataclass
class TopicSuggestion:
    slugs: list = field(default_factory=list)
    source: str = ""  # "content" (AI, read the post) or "heuristic"
    confident: bool = True
    note: str = ""


def topic_instructions(topics):
    listing = "\n".join(f"- {topic.slug}: {topic.name}" + (f" — {topic.description}" if topic.description else "") for topic in topics)
    return (
        "You label blog posts for a story-reading website.\n"
        f"Choose the {MAX_TOPICS_PER_POST} topics (at most) that best describe the post, using ONLY these topic slugs:\n"
        f"{listing}\n\n"
        "Put the chosen slugs, comma-separated and nothing else, in the `text` field "
        "(for example: reading-guides, folklore-myth). Choose one topic if only one clearly fits. "
        "Set confident=false if none of the topics fits well, and say why in confidence_note."
    )


def parse_topic_slugs(text, valid_slugs):
    """The slugs in `text` that are real topics, in order, at most two."""
    chosen = []
    for token in re.split(r"[,\n;]+", (text or "").lower()):
        slug = token.strip().strip("-•*").strip()
        if slug in valid_slugs and slug not in chosen:
            chosen.append(slug)
    return chosen[:MAX_TOPICS_PER_POST]


def _plain(blog):
    text = strip_tags(blog.content or "").strip()
    return f"Excerpt: {blog.excerpt or '(none)'}\n\n{text}"[:MAX_CONTENT_CHARS]


def suggest_topics_ai(blog, topics=None):
    """Ask the model for 1–2 topic slugs. Raises GenerationError on API failure."""
    topics = list(topics if topics is not None else BlogTopic.objects.all())
    valid = {topic.slug for topic in topics}
    result = generate(
        action="topics",
        instructions=topic_instructions(topics),
        title=blog.title,
        author_name=blog.author_name,
        input_fields=["title", "author", "content"],
        model=PromptSettings.get_solo().excerpt_model,
        content_text=_plain(blog),
        render_as="text",
    )
    slugs = parse_topic_slugs(result.content, valid)
    if not slugs:
        raise GenerationError(f"The model returned no valid topic: {result.content!r}")
    return TopicSuggestion(slugs=slugs, source=result.source, confident=result.confident, note=result.confidence_note)


# Keyword fallback: a topic's score is how many of its cues the post's title,
# excerpt and body mention (title counted three times). Deliberately simple.
HEURISTIC_CUES = {
    "reading-guides": ["read along", "reading list", "where to start", "how to read", "to read this", "guide", "recommend", "monsoon", "five ", "ten ", "best "],
    "genre-deep-dives": ["genre", "gothic", "science fiction", "fantasy", "mystery", "romance", "horror", "poetry", "short story", "short fiction"],
    "book-explainers": ["explainer", "what happens", "meaning of", "themes of", "analysis", "why the", "how we choose", "ending of"],
    "author-spotlights": ["author", "writer", "spotlight", "interview", "notes from", "biography", "her work", "his work"],
    "folklore-myth": ["folktale", "folk tale", "folklore", "myth", "legend", "fable", "fairy tale", "oral tradition"],
    "women-writers": ["women writers", "women authors", "her novel", "female author", "woman writer", "women's"],
}


def suggest_topics_heuristic(blog, topics=None):
    valid = {topic.slug for topic in (topics if topics is not None else BlogTopic.objects.all())}
    title = (blog.title or "").lower()
    body = f"{blog.excerpt or ''} {strip_tags(blog.content or '')}".lower()
    scored = []
    for slug, cues in HEURISTIC_CUES.items():
        if slug not in valid:
            continue
        score = sum(3 * title.count(cue) + body.count(cue) for cue in cues)
        if score:
            scored.append((score, slug))
    scored.sort(key=lambda row: (-row[0], row[1]))
    slugs = [slug for _score, slug in scored[:MAX_TOPICS_PER_POST]]
    return TopicSuggestion(
        slugs=slugs,
        source="heuristic",
        confident=bool(scored) and scored[0][0] >= 3,
        note="Keyword match, not read by a model." if slugs else "No keyword matched any topic.",
    )


def apply_suggestion(blog, suggestion):
    """Writes the suggested topics and the status trail onto the post."""
    topics = list(BlogTopic.objects.filter(slug__in=suggestion.slugs))
    blog.topics.set(topics)
    Blog.objects.filter(pk=blog.pk).update(
        topics_status=Blog.GEN_STATUS_COMPLETED,
        topics_source=Blog.GEN_SOURCE_CONTENT if suggestion.source == "content" else Blog.GEN_SOURCE_METADATA,
        topics_confident=suggestion.confident,
        topics_confidence_note=suggestion.note,
        topics_error=None,
    )
