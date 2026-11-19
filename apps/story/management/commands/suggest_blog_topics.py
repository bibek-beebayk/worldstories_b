from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.story.ai_generation import GenerationError
from apps.story.blog_topics import apply_suggestion, suggest_topics_ai, suggest_topics_heuristic
from apps.story.models import Blog, BlogTopic


class Command(BaseCommand):
    help = (
        "Suggest 1-2 topics per blog post, from the seeded topic list. A dry run unless "
        "--apply: it prints the suggestions and writes nothing."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the suggested topics (default: dry run).")
        parser.add_argument("--all", action="store_true", help="Include posts that already have topics (default: only those without).")
        parser.add_argument("--slug", help="Only this post.")
        parser.add_argument(
            "--heuristic",
            action="store_true",
            help="Keyword matching instead of the AI pipeline — a rough first pass, for when no API key is set.",
        )

    def handle(self, *args, **options):
        topics = list(BlogTopic.objects.all())
        if not topics:
            raise CommandError("No blog topics exist yet. Run migrations (they seed the list) or add some in the admin.")
        use_ai = not options["heuristic"]
        if use_ai and not settings.ANTHROPIC_API_KEY:
            raise CommandError("ANTHROPIC_API_KEY is not set. Set it, or pass --heuristic for a keyword-based first pass.")

        blogs = Blog.objects.published().order_by("created_at")
        if options["slug"]:
            blogs = blogs.filter(slug=options["slug"])
        if not options["all"]:
            blogs = blogs.filter(topics__isnull=True)

        names = {topic.slug: topic.name for topic in topics}
        mode = "AI" if use_ai else "heuristic"
        self.stdout.write(f"{'APPLYING' if options['apply'] else 'DRY RUN'} ({mode}) - {blogs.count()} post(s)\n")
        done = failed = 0
        for blog in blogs:
            try:
                suggestion = suggest_topics_ai(blog, topics) if use_ai else suggest_topics_heuristic(blog, topics)
            except GenerationError as exc:
                failed += 1
                self.stdout.write(f"  [{blog.pk}] {blog.title}\n      FAILED: {exc}")
                continue
            label = ", ".join(names[slug] for slug in suggestion.slugs) or "(none)"
            flag = "" if suggestion.confident else "  [low confidence]"
            self.stdout.write(f"  [{blog.pk}] {blog.title}\n      -> {label}{flag}  ({suggestion.note})" if suggestion.note else f"  [{blog.pk}] {blog.title}\n      -> {label}{flag}")
            if options["apply"] and suggestion.slugs:
                apply_suggestion(blog, suggestion)
                done += 1
        if options["apply"]:
            self.stdout.write(self.style.SUCCESS(f"\nApplied topics to {done} post(s); {failed} failed."))
        else:
            self.stdout.write("\nNothing was written. Re-run with --apply to save these.")
