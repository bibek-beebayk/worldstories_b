from django.core.management.base import BaseCommand

from apps.story.ai_generation_jobs import executor, run_generate_blog_excerpt
from apps.story.models import Blog


class Command(BaseCommand):
    help = (
        "Queue excerpt generation for every published blog post whose excerpt is empty. "
        "A dry run unless --apply: it lists the posts and queues nothing."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Queue the generation jobs (default: dry run).")

    def handle(self, *args, **options):
        from django.db.models import Q

        missing = Blog.objects.published().filter(Q(excerpt__isnull=True) | Q(excerpt__exact="")).order_by("created_at")
        posts = list(missing)
        self.stdout.write(f"{'QUEUEING' if options['apply'] else 'DRY RUN'}: {len(posts)} published post(s) have no excerpt")
        for blog in posts:
            self.stdout.write(f"  [{blog.pk}] {blog.title}")
        if not options["apply"]:
            self.stdout.write("\nNothing was queued. Re-run with --apply to queue them.")
            return
        for blog in posts:
            Blog.objects.filter(pk=blog.pk).update(excerpt_status=Blog.GEN_STATUS_PENDING)
            executor.submit(run_generate_blog_excerpt, blog.pk)
        # This process exits when the command does; wait so the queued jobs are not dropped.
        executor.shutdown(wait=True)
        self.stdout.write(self.style.SUCCESS(f"Queued {len(posts)} excerpt generation job(s)."))
