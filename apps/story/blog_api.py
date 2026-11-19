"""Blog list support: 30-day readers per post and the list page's overview.

"Readers" are unique audience people (staff and suspected bots excluded, a
person being the signed-in user else the browser) who opened a post — a visit
event, or a reading session, carrying the post. AnalyticsEvent is used rather
than BlogReadingProgress: progress rows exist only for signed-in readers, and
most blog traffic is anonymous search landings.
"""

from collections import defaultdict
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Count
from django.utils import timezone

from apps.stats.models import AnalyticsEvent

from .models import Blog, BlogTopic, Story, published_blog_q

READS_WINDOW_DAYS = 30
READS_CACHE_KEY = "blog:readers-30d:v1"
READS_CACHE_SECONDS = 60 * 15
MOST_READ_LIMIT = 4
LINKED_BOOKS_LIMIT = 6
READ_EVENT_TYPES = (AnalyticsEvent.EVENT_VISIT, AnalyticsEvent.EVENT_READING_SESSION)


def compute_blog_readers():
    from .analytics_api import _content_identity, audience_events

    since = timezone.now() - timedelta(days=READS_WINDOW_DAYS)
    rows = audience_events(
        AnalyticsEvent.objects.filter(event_type__in=READ_EVENT_TYPES, blog__isnull=False, created_at__gte=since)
    ).values_list("blog_id", "user_id", "visitor_id")
    people = defaultdict(set)
    for blog_id, user_id, visitor_id in rows:
        people[blog_id].add(_content_identity(user_id, visitor_id))
    return {blog_id: len(ids) for blog_id, ids in people.items()}


def blog_readers_30d():
    """{blog_id: unique readers in the last 30 days}, cached for 15 minutes."""
    counts = cache.get(READS_CACHE_KEY)
    if counts is None:
        counts = compute_blog_readers()
        cache.set(READS_CACHE_KEY, counts, READS_CACHE_SECONDS)
    return counts


def popularity_order(blog_ids_with_dates, counts):
    """Ids most-read first, ties newest first. `blog_ids_with_dates` is (id, created_at)."""
    return [pk for pk, _created in sorted(blog_ids_with_dates, key=lambda row: (-counts.get(row[0], 0), -row[1].timestamp()))]


def overview_parts():
    """The raw material for GET /blog/overview/: model objects and counts; the view serializes them."""
    published = Blog.objects.published()
    counts = blog_readers_30d()

    featured = published.filter(is_featured=True).order_by("-created_at").first() or published.order_by("-created_at").first()

    ranked = popularity_order(list(published.values_list("pk", "created_at")), counts)
    most_read_ids = [pk for pk in ranked if counts.get(pk, 0) > 0][:MOST_READ_LIMIT]

    topics = list(
        BlogTopic.objects.annotate(post_count=Count("blogs", filter=published_blog_q("blogs"), distinct=True)).order_by("order", "name")
    )
    books = list(
        Story.objects.published()
        .annotate(post_count=Count("linked_blog_posts", filter=published_blog_q("linked_blog_posts"), distinct=True))
        .filter(post_count__gt=0)
        .select_related("author")
        .order_by("-post_count", "title")[:LINKED_BOOKS_LIMIT]
    )
    return {
        "featured": featured,
        "most_read_ids": most_read_ids,
        "topics": topics,
        "linked_books": books,
        "total_posts": published.count(),
        "counts": counts,
    }
