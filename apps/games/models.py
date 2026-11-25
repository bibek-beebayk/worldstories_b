from django.conf import settings
from django.db import models
from versatileimagefield.fields import VersatileImageField

from apps.story.models import COUNTRY_CHOICES


def _normalize_identity(value: str) -> str:
    return " ".join(str(value or "").split()).lower()


class PuzzleBook(models.Model):
    """A literary work outside the WorldStories catalogue used in Name That Story."""

    LENGTH_SHORT_STORY = "Short story"
    LENGTH_NOVELLA = "Novella"
    LENGTH_NOVEL = "Novel"
    LENGTH_CHOICES = [
        (LENGTH_SHORT_STORY, "Short story"),
        (LENGTH_NOVELLA, "Novella"),
        (LENGTH_NOVEL, "Novel"),
    ]

    title = models.CharField(max_length=256)
    author_name = models.CharField(max_length=256)
    normalized_title = models.CharField(max_length=256, editable=False, db_index=True)
    normalized_author = models.CharField(max_length=256, editable=False, db_index=True)
    genres = models.ManyToManyField("story.Genre", related_name="puzzle_books", blank=True)
    country = models.CharField(max_length=2, choices=COUNTRY_CHOICES, blank=True, default="")
    original_published_year = models.PositiveSmallIntegerField(null=True, blank=True)
    length_label = models.CharField(
        max_length=16,
        choices=LENGTH_CHOICES,
        default=LENGTH_NOVEL,
    )
    cover_image = models.URLField(blank=True, null=True)
    cover_image_file = VersatileImageField(upload_to="puzzle_book_covers/", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    SIZES = {
        "cover_image_file": {
            "card": "thumbnail__480x640",
        }
    }

    class Meta:
        ordering = ["title", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["normalized_title", "normalized_author"],
                name="games_puzzlebook_unique_norm_title_author",
            ),
        ]

    def save(self, *args, **kwargs):
        self.title = " ".join(str(self.title or "").split())
        self.author_name = " ".join(str(self.author_name or "").split())
        self.normalized_title = _normalize_identity(self.title)
        self.normalized_author = _normalize_identity(self.author_name)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} — {self.author_name}"


class DailyPuzzle(models.Model):
    DIFFICULTY_EASY = "easy"
    DIFFICULTY_MEDIUM = "medium"
    DIFFICULTY_HARD = "hard"
    DIFFICULTY_CHOICES = [
        (DIFFICULTY_EASY, "Easy"),
        (DIFFICULTY_MEDIUM, "Medium"),
        (DIFFICULTY_HARD, "Hard"),
    ]

    date = models.DateField(unique=True, db_index=True)
    story = models.ForeignKey(
        "story.Story",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="daily_puzzles",
    )
    book = models.ForeignKey(
        PuzzleBook,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="daily_puzzles",
    )
    opening_line = models.TextField()
    source_chapter = models.ForeignKey(
        "story.Chapter",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="daily_puzzles",
    )
    difficulty = models.CharField(
        max_length=16,
        choices=DIFFICULTY_CHOICES,
        blank=True,
        default="",
    )
    auto_filled = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_daily_puzzles",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date"]
        indexes = [
            models.Index(fields=["date"], name="games_dailypuzzle_date_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                check=(
                    (models.Q(story__isnull=False) & models.Q(book__isnull=True))
                    | (models.Q(story__isnull=True) & models.Q(book__isnull=False))
                ),
                name="games_dailypuzzle_exactly_one_answer",
            ),
        ]

    @property
    def number(self) -> int:
        first_date = (
            DailyPuzzle.objects.order_by("date").values_list("date", flat=True).first()
        )
        if first_date is None or self.date < first_date:
            return 1
        return (self.date - first_date).days + 1

    @property
    def answer_facts(self):
        from apps.games.services import facts_from_book, facts_from_story

        if self.story_id:
            return facts_from_story(self.story)
        if self.book_id:
            return facts_from_book(self.book)
        raise ValueError("DailyPuzzle has neither story nor book set.")

    def save(self, *args, **kwargs):
        if not self.difficulty and (self.story_id or self.book_id):
            from apps.games.services import compute_difficulty

            self.difficulty = compute_difficulty(self.opening_line, self.answer_facts)
        super().save(*args, **kwargs)

    def __str__(self):
        title = self.answer_facts.title if (self.story_id or self.book_id) else "Unassigned"
        return f"#{self.number} ({self.date}) — {title}"


class PuzzleAttempt(models.Model):
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_SOLVED = "solved"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_IN_PROGRESS, "In progress"),
        (STATUS_SOLVED, "Solved"),
        (STATUS_FAILED, "Failed"),
    ]

    puzzle = models.ForeignKey(
        DailyPuzzle,
        on_delete=models.CASCADE,
        related_name="attempts",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="puzzle_attempts",
    )
    guest_key = models.CharField(max_length=64, blank=True, default="", db_index=True)
    guesses = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_IN_PROGRESS,
    )
    guess_count = models.PositiveSmallIntegerField(default=0)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                check=(
                    (models.Q(user__isnull=False) & models.Q(guest_key=""))
                    | (models.Q(user__isnull=True) & ~models.Q(guest_key=""))
                ),
                name="games_attempt_exactly_one_owner",
            ),
            models.UniqueConstraint(
                fields=["puzzle", "user"],
                condition=models.Q(user__isnull=False),
                name="games_attempt_unique_puzzle_user",
            ),
            models.UniqueConstraint(
                fields=["puzzle", "guest_key"],
                condition=~models.Q(guest_key=""),
                name="games_attempt_unique_puzzle_guest",
            ),
        ]

    def __str__(self):
        owner = f"user={self.user_id}" if self.user_id else f"guest={self.guest_key[:8]}"
        return f"Attempt({self.puzzle.date}, {owner}, {self.status})"
