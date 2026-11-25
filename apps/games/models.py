from django.conf import settings
from django.db import models


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

    @property
    def number(self) -> int:
        first_date = (
            DailyPuzzle.objects.order_by("date").values_list("date", flat=True).first()
        )
        if first_date is None or self.date < first_date:
            return 1
        return (self.date - first_date).days + 1

    def save(self, *args, **kwargs):
        if not self.difficulty and self.story_id:
            from apps.games.services import compute_difficulty

            self.difficulty = compute_difficulty(self.opening_line, self.story)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"#{self.number} ({self.date}) — {self.story.title}"


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
