from django.conf import settings
from django.db import models


class DailyPuzzle(models.Model):
    PUZZLE_TYPE_OPENING_LINE = "opening_line"
    PUZZLE_TYPE_QUOTE = "quote"
    PUZZLE_TYPE_TRIVIA = "trivia"
    PUZZLE_TYPE_AUTHOR_BIO = "author_bio"
    PUZZLE_TYPE_CHOICES = [
        (PUZZLE_TYPE_OPENING_LINE, "Opening line"),
        (PUZZLE_TYPE_QUOTE, "In-book quote"),
        (PUZZLE_TYPE_TRIVIA, "Trivia ladder"),
        (PUZZLE_TYPE_AUTHOR_BIO, "Who wrote it"),
    ]

    ANSWER_KIND_STORY = "story"
    ANSWER_KIND_AUTHOR = "author"
    ANSWER_KIND_CHOICES = [
        (ANSWER_KIND_STORY, "Story"),
        (ANSWER_KIND_AUTHOR, "Author"),
    ]

    DIFFICULTY_EASY = "easy"
    DIFFICULTY_MEDIUM = "medium"
    DIFFICULTY_HARD = "hard"
    DIFFICULTY_CHOICES = [
        (DIFFICULTY_EASY, "Easy"),
        (DIFFICULTY_MEDIUM, "Medium"),
        (DIFFICULTY_HARD, "Hard"),
    ]

    STATUS_DRAFT = "draft"
    STATUS_APPROVED = "approved"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_APPROVED, "Approved"),
    ]

    date = models.DateField(unique=True, db_index=True)
    puzzle_type = models.CharField(
        max_length=24,
        choices=PUZZLE_TYPE_CHOICES,
        default=PUZZLE_TYPE_OPENING_LINE,
    )
    answer_kind = models.CharField(
        max_length=16,
        choices=ANSWER_KIND_CHOICES,
        default=ANSWER_KIND_STORY,
    )
    answer_story = models.ForeignKey(
        "story.Story",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="daily_puzzles",
    )
    answer_author = models.ForeignKey(
        "story.Author",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="daily_puzzles",
    )
    clues = models.JSONField(default=list, blank=True)
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
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_DRAFT,
    )
    auto_filled = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_daily_puzzles",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approved_daily_puzzles",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date"]
        indexes = [
            models.Index(fields=["date", "status"], name="games_dp_date_status_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                check=(
                    (
                        models.Q(answer_kind="story")
                        & models.Q(answer_story__isnull=False)
                        & models.Q(answer_author__isnull=True)
                    )
                    | (
                        models.Q(answer_kind="author")
                        & models.Q(answer_author__isnull=False)
                        & models.Q(answer_story__isnull=True)
                    )
                ),
                name="games_dp_answer_matches_kind",
            ),
            models.CheckConstraint(
                check=(
                    (models.Q(puzzle_type="author_bio") & models.Q(answer_kind="author"))
                    | (~models.Q(puzzle_type="author_bio") & models.Q(answer_kind="story"))
                ),
                name="games_dp_type_matches_kind",
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
    def answer(self):
        if self.answer_kind == self.ANSWER_KIND_AUTHOR:
            return self.answer_author
        return self.answer_story

    def save(self, *args, **kwargs):
        self.answer_kind = (
            self.ANSWER_KIND_AUTHOR
            if self.puzzle_type == self.PUZZLE_TYPE_AUTHOR_BIO
            else self.ANSWER_KIND_STORY
        )
        if not self.difficulty and (self.answer_story_id or self.answer_author_id):
            try:
                from apps.games.services.engine import compute_difficulty

                self.difficulty = compute_difficulty(self)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self):
        if self.answer_kind == self.ANSWER_KIND_AUTHOR and self.answer_author_id:
            label = self.answer_author.name
        elif self.answer_story_id:
            label = self.answer_story.title
        else:
            label = "Unassigned"
        return f"#{self.number} ({self.date}) [{self.puzzle_type}] — {label}"


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


class PuzzleRotation(models.Model):
    WEEKDAY_CHOICES = [
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    ]

    weekday = models.PositiveSmallIntegerField(unique=True, choices=WEEKDAY_CHOICES)
    puzzle_type = models.CharField(
        max_length=24,
        choices=DailyPuzzle.PUZZLE_TYPE_CHOICES,
    )

    class Meta:
        ordering = ["weekday"]

    def __str__(self):
        return f"{self.get_weekday_display()} -> {self.puzzle_type}"
