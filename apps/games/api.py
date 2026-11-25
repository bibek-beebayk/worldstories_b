"""Public and admin REST endpoints for the Name That Story daily puzzle game."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.games.models import DailyPuzzle, PuzzleAttempt
from apps.games.services import (
    CARD_COVER_SIZE,
    MAX_GUESSES,
    auto_fill,
    clues_for,
    compare_guess,
    extract_candidates,
    opening_line_in_story,
    result_share_text,
    stats_for,
    utc_today,
)
from apps.story.api import IsSuperUser
from apps.story.models import Audiobook, Story
from core.libs.images import get_cover_image_url


def _parse_iso_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw).strip())
    except ValueError:
        return None


def _get_guest_key(request) -> str:
    if request.user and request.user.is_authenticated:
        return ""
    return (request.headers.get("X-Guest-Key") or "").strip()[:64]


def _find_attempt(puzzle: DailyPuzzle, request) -> PuzzleAttempt | None:
    if request.user and request.user.is_authenticated:
        return PuzzleAttempt.objects.filter(puzzle=puzzle, user=request.user).first()
    guest_key = _get_guest_key(request)
    if not guest_key:
        return None
    return PuzzleAttempt.objects.filter(puzzle=puzzle, user__isnull=True, guest_key=guest_key).first()


def _get_or_create_attempt(puzzle: DailyPuzzle, request) -> tuple[PuzzleAttempt | None, str | None]:
    if request.user and request.user.is_authenticated:
        attempt, _ = PuzzleAttempt.objects.get_or_create(
            puzzle=puzzle,
            user=request.user,
            defaults={"guest_key": ""},
        )
        return attempt, None
    guest_key = _get_guest_key(request)
    if not guest_key:
        return None, "Missing X-Guest-Key header."
    attempt, _ = PuzzleAttempt.objects.get_or_create(
        puzzle=puzzle,
        user=None,
        guest_key=guest_key,
    )
    return attempt, None


def _serialize_answer_card(story: Story, request) -> dict:
    cover = get_cover_image_url(story.cover_image_file, story.cover_image, request, size=CARD_COVER_SIZE)
    audiobook = (
        Audiobook.objects.published()
        .filter(story=story, track_count__gt=0)
        .order_by("id")
        .only("slug")
        .first()
    )
    return {
        "id": story.id,
        "title": story.title,
        "slug": story.slug,
        "author": story.author.name if story.author_id else "",
        "year": story.original_published_year,
        "cover": cover or "",
        "read_time": story.cached_chapter_reading_minutes,
        "has_audiobook": audiobook is not None,
        "audiobook_slug": audiobook.slug if audiobook else None,
    }


def _serialize_puzzle_payload(
    puzzle: DailyPuzzle,
    attempt: PuzzleAttempt | None,
    request,
    include_yesterday: bool = False,
    newly_unlocked_clue: dict | None = None,
) -> dict:
    raw_guesses = attempt.guesses if attempt else []
    guesses_used = attempt.guess_count if attempt else 0
    attempt_status = attempt.status if attempt else PuzzleAttempt.STATUS_IN_PROGRESS
    unlocked_count = min(MAX_GUESSES, guesses_used + 1)

    guessed_ids = [g["story_id"] for g in raw_guesses if g.get("story_id")]
    stories_by_id = {
        s.id: s
        for s in Story.objects.filter(id__in=guessed_ids).select_related("author").only(
            "id", "title", "author__name"
        )
    }

    history = []
    for entry in raw_guesses:
        sid = entry.get("story_id")
        s = stories_by_id.get(sid) if sid else None
        history.append(
            {
                "story_id": sid,
                "title": s.title if s else None,
                "author": s.author.name if (s and s.author_id) else "",
                "correct": bool(entry.get("correct")),
                "matches": entry.get("matches") or {"genre": False, "country": False, "era": "different"},
                "skipped": bool(entry.get("skipped")),
            }
        )

    payload: dict = {
        "puzzle_number": puzzle.number,
        "date": puzzle.date.isoformat(),
        "opening_line": puzzle.opening_line,
        "clues": clues_for(puzzle, unlocked_count, request=request),
        "guesses_used": guesses_used,
        "guesses": history,
        "status": attempt_status,
        "max_guesses": MAX_GUESSES,
    }

    if newly_unlocked_clue is not None:
        payload["newly_unlocked_clue"] = newly_unlocked_clue

    # CRITICAL: Answer and share tokens are ONLY returned once the attempt is complete.
    if attempt is not None and attempt_status in (
        PuzzleAttempt.STATUS_SOLVED,
        PuzzleAttempt.STATUS_FAILED,
    ):
        payload["answer"] = _serialize_answer_card(puzzle.story, request)
        payload["share"] = result_share_text(puzzle, attempt)

    if include_yesterday:
        yesterday_date = utc_today() - timedelta(days=1)
        y_puzzle = (
            DailyPuzzle.objects.select_related("story", "story__author")
            .filter(date=yesterday_date)
            .first()
        )
        if y_puzzle is not None:
            payload["yesterday"] = {
                "puzzle_number": y_puzzle.number,
                "date": y_puzzle.date.isoformat(),
                **_serialize_answer_card(y_puzzle.story, request),
            }
        else:
            payload["yesterday"] = None

    return payload


class TodayPuzzleAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        today = utc_today()
        puzzle = (
            DailyPuzzle.objects.select_related("story", "story__author")
            .prefetch_related("story__genres")
            .filter(date=today)
            .first()
        )
        if puzzle is None:
            return Response(
                {"detail": "No puzzle is scheduled for today yet."},
                status=status.HTTP_404_NOT_FOUND,
            )
        attempt = _find_attempt(puzzle, request)
        return Response(_serialize_puzzle_payload(puzzle, attempt, request, include_yesterday=True))


class PuzzleByDateAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, puzzle_date: str):
        parsed = _parse_iso_date(puzzle_date)
        if parsed is None or parsed > utc_today():
            return Response({"detail": "Puzzle not found."}, status=status.HTTP_404_NOT_FOUND)
        puzzle = (
            DailyPuzzle.objects.select_related("story", "story__author")
            .prefetch_related("story__genres")
            .filter(date=parsed)
            .first()
        )
        if puzzle is None:
            return Response({"detail": "Puzzle not found."}, status=status.HTTP_404_NOT_FOUND)
        attempt = _find_attempt(puzzle, request)
        return Response(_serialize_puzzle_payload(puzzle, attempt, request))


class PuzzleSearchStoriesAPIView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "game_search"

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        if len(q) < 2:
            return Response({"results": []})
        stories = (
            Story.objects.published()
            .filter(title__icontains=q)
            .select_related("author")
            .order_by("-views", "title")[:8]
        )
        results = [
            {
                "id": s.id,
                "title": s.title,
                "author": s.author.name if s.author_id else "",
                "cover_thumb": get_cover_image_url(
                    s.cover_image_file, s.cover_image, request, size=CARD_COVER_SIZE
                )
                or "",
            }
            for s in stories
        ]
        return Response({"results": results})


class PuzzleGuessAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        raw_date = request.data.get("date")
        target_date = _parse_iso_date(raw_date) if raw_date else utc_today()
        if target_date is None or target_date > utc_today():
            return Response({"detail": "Puzzle not found."}, status=status.HTTP_404_NOT_FOUND)

        story_id = request.data.get("story_id")
        if not isinstance(story_id, int) or isinstance(story_id, bool):
            return Response({"error": "Provide a valid story_id."}, status=status.HTTP_400_BAD_REQUEST)

        puzzle = (
            DailyPuzzle.objects.select_related("story", "story__author")
            .prefetch_related("story__genres")
            .filter(date=target_date)
            .first()
        )
        if puzzle is None:
            return Response({"detail": "Puzzle not found."}, status=status.HTTP_404_NOT_FOUND)

        guessed_story = (
            Story.objects.published()
            .prefetch_related("genres")
            .select_related("author")
            .filter(pk=story_id)
            .first()
        )
        if guessed_story is None:
            return Response({"error": "Story not found."}, status=status.HTTP_400_BAD_REQUEST)

        attempt, owner_err = _get_or_create_attempt(puzzle, request)
        if owner_err or attempt is None:
            return Response({"error": owner_err}, status=status.HTTP_400_BAD_REQUEST)

        if attempt.status != PuzzleAttempt.STATUS_IN_PROGRESS:
            return Response(
                {"error": "already_completed"},
                status=status.HTTP_409_CONFLICT,
            )

        existing_ids = {g.get("story_id") for g in (attempt.guesses or []) if not g.get("skipped")}
        if story_id in existing_ids:
            return Response(
                {"error": "already_guessed"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        prev_unlocked = min(MAX_GUESSES, attempt.guess_count + 1)
        result = compare_guess(puzzle.story, guessed_story)

        guesses = list(attempt.guesses or [])
        guesses.append(
            {
                "story_id": guessed_story.id,
                "correct": result["correct"],
                "matches": result["matches"],
                "skipped": False,
            }
        )
        attempt.guesses = guesses
        attempt.guess_count = len(guesses)

        if result["correct"]:
            attempt.status = PuzzleAttempt.STATUS_SOLVED
            attempt.completed_at = timezone.now()
        elif attempt.guess_count >= MAX_GUESSES:
            attempt.status = PuzzleAttempt.STATUS_FAILED
            attempt.completed_at = timezone.now()
        attempt.save()

        new_unlocked = min(MAX_GUESSES, attempt.guess_count + 1)
        newly_unlocked = None
        if attempt.status == PuzzleAttempt.STATUS_IN_PROGRESS and new_unlocked > prev_unlocked:
            all_clues = clues_for(puzzle, new_unlocked, request=request)
            newly_unlocked = all_clues[-1] if all_clues else None

        return Response(
            _serialize_puzzle_payload(
                puzzle, attempt, request, newly_unlocked_clue=newly_unlocked
            )
        )


class PuzzleSkipAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        raw_date = request.data.get("date")
        target_date = _parse_iso_date(raw_date) if raw_date else utc_today()
        if target_date is None or target_date > utc_today():
            return Response({"detail": "Puzzle not found."}, status=status.HTTP_404_NOT_FOUND)

        puzzle = (
            DailyPuzzle.objects.select_related("story", "story__author")
            .prefetch_related("story__genres")
            .filter(date=target_date)
            .first()
        )
        if puzzle is None:
            return Response({"detail": "Puzzle not found."}, status=status.HTTP_404_NOT_FOUND)

        attempt, owner_err = _get_or_create_attempt(puzzle, request)
        if owner_err or attempt is None:
            return Response({"error": owner_err}, status=status.HTTP_400_BAD_REQUEST)

        if attempt.status != PuzzleAttempt.STATUS_IN_PROGRESS:
            return Response(
                {"error": "already_completed"},
                status=status.HTTP_409_CONFLICT,
            )

        prev_unlocked = min(MAX_GUESSES, attempt.guess_count + 1)
        guesses = list(attempt.guesses or [])
        guesses.append(
            {
                "story_id": None,
                "correct": False,
                "matches": {"genre": False, "country": False, "era": "different"},
                "skipped": True,
            }
        )
        attempt.guesses = guesses
        attempt.guess_count = len(guesses)
        if attempt.guess_count >= MAX_GUESSES:
            attempt.status = PuzzleAttempt.STATUS_FAILED
            attempt.completed_at = timezone.now()
        attempt.save()

        new_unlocked = min(MAX_GUESSES, attempt.guess_count + 1)
        newly_unlocked = None
        if attempt.status == PuzzleAttempt.STATUS_IN_PROGRESS and new_unlocked > prev_unlocked:
            all_clues = clues_for(puzzle, new_unlocked, request=request)
            newly_unlocked = all_clues[-1] if all_clues else None

        return Response(
            _serialize_puzzle_payload(
                puzzle, attempt, request, newly_unlocked_clue=newly_unlocked
            )
        )


class PuzzleStatsAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        user = request.user if request.user and request.user.is_authenticated else None
        guest_key = _get_guest_key(request)
        return Response(stats_for(user=user, guest_key=guest_key))


class PuzzleArchiveAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        today = utc_today()
        page_size = 30
        try:
            page = max(1, int(request.query_params.get("page") or 1))
        except (TypeError, ValueError):
            page = 1

        qs = DailyPuzzle.objects.filter(date__lt=today).order_by("-date")
        total = qs.count()
        offset = (page - 1) * page_size
        puzzles = list(qs[offset : offset + page_size])
        puzzle_ids = [p.id for p in puzzles]

        attempts_by_puzzle: dict[int, PuzzleAttempt] = {}
        if puzzle_ids:
            if request.user and request.user.is_authenticated:
                a_qs = PuzzleAttempt.objects.filter(user=request.user, puzzle_id__in=puzzle_ids)
            else:
                guest_key = _get_guest_key(request)
                a_qs = (
                    PuzzleAttempt.objects.filter(
                        user__isnull=True, guest_key=guest_key, puzzle_id__in=puzzle_ids
                    )
                    if guest_key
                    else PuzzleAttempt.objects.none()
                )
            attempts_by_puzzle = {a.puzzle_id: a for a in a_qs}

        results = []
        for p in puzzles:
            att = attempts_by_puzzle.get(p.id)
            results.append(
                {
                    "puzzle_number": p.number,
                    "date": p.date.isoformat(),
                    "status": att.status if att else "none",
                    "guess_count": att.guess_count if att else None,
                }
            )

        pages = max(1, (total + page_size - 1) // page_size)
        return Response(
            {
                "results": results,
                "pagination": {
                    "page": page,
                    "pages": pages,
                    "count": total,
                    "size": page_size,
                },
            }
        )


class MergeGuestAttemptsAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        guest_key = (request.data.get("guest_key") or "").strip()[:64]
        if not guest_key:
            return Response({"merged": 0, "skipped": 0})

        merged = 0
        skipped = 0
        with transaction.atomic():
            guest_attempts = list(
                PuzzleAttempt.objects.filter(user__isnull=True, guest_key=guest_key).order_by("id")
            )
            existing_puzzle_ids = set(
                PuzzleAttempt.objects.filter(
                    user=request.user,
                    puzzle_id__in=[a.puzzle_id for a in guest_attempts],
                ).values_list("puzzle_id", flat=True)
            )
            for attempt in guest_attempts:
                if attempt.puzzle_id in existing_puzzle_ids:
                    skipped += 1
                    continue
                attempt.user = request.user
                attempt.guest_key = ""
                attempt.save(update_fields=["user", "guest_key"])
                existing_puzzle_ids.add(attempt.puzzle_id)
                merged += 1

        return Response({"merged": merged, "skipped": skipped})


# ---------------------------------------------------------------------------
# Admin Endpoints (/api/admin/games/name-that-story/)
# ---------------------------------------------------------------------------


def _admin_row_for_puzzle(puzzle: DailyPuzzle, today: date, players_count: int, solved_count: int) -> dict:
    if puzzle.date == today:
        row_status = "live_today"
    elif puzzle.auto_filled:
        row_status = "auto"
    else:
        row_status = "scheduled"
    solve_rate = round((solved_count / players_count) * 100) if players_count > 0 else None
    return {
        "id": puzzle.id,
        "date": puzzle.date.isoformat(),
        "number": puzzle.number,
        "opening_line": puzzle.opening_line,
        "story": {"id": puzzle.story_id, "title": puzzle.story.title},
        "difficulty": puzzle.difficulty,
        "auto_filled": puzzle.auto_filled,
        "status": row_status,
        "players_count": players_count,
        "solve_rate": solve_rate,
    }


class AdminPuzzlesListCreateAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        today = utc_today()
        from_date = _parse_iso_date(request.query_params.get("from")) or today
        to_date = _parse_iso_date(request.query_params.get("to")) or (from_date + timedelta(days=13))
        if to_date < from_date:
            to_date = from_date
        if (to_date - from_date).days > 90:
            to_date = from_date + timedelta(days=90)

        status_filter = (request.query_params.get("status") or "").strip().lower()
        q = (request.query_params.get("q") or "").strip().lower()

        puzzles_qs = (
            DailyPuzzle.objects.select_related("story")
            .filter(date__gte=from_date, date__lte=to_date)
            .annotate(
                players_count=Count("attempts"),
                solved_count=Count("attempts", filter=Q(attempts__status=PuzzleAttempt.STATUS_SOLVED)),
            )
        )
        by_date = {p.date: p for p in puzzles_qs}

        rows = []
        cursor = from_date
        while cursor <= to_date:
            p = by_date.get(cursor)
            if p is not None:
                row = _admin_row_for_puzzle(p, today, p.players_count, p.solved_count)
            else:
                row = {
                    "id": None,
                    "date": cursor.isoformat(),
                    "number": None,
                    "opening_line": "",
                    "story": None,
                    "difficulty": "",
                    "auto_filled": False,
                    "status": "empty",
                    "players_count": 0,
                    "solve_rate": None,
                }
            if status_filter and row["status"] != status_filter:
                cursor += timedelta(days=1)
                continue
            if q:
                hay = f"{row['opening_line']} {(row['story'] or {}).get('title', '')}".lower()
                if q not in hay:
                    cursor += timedelta(days=1)
                    continue
            rows.append(row)
            cursor += timedelta(days=1)

        return Response({"results": rows})

    def post(self, request):
        parsed_date = _parse_iso_date(request.data.get("date"))
        if parsed_date is None:
            return Response({"date": "Provide a valid YYYY-MM-DD date."}, status=status.HTTP_400_BAD_REQUEST)
        if DailyPuzzle.objects.filter(date=parsed_date).exists():
            return Response({"date": "A puzzle is already scheduled for this date."}, status=status.HTTP_400_BAD_REQUEST)

        story_id = request.data.get("story_id")
        story = Story.objects.filter(pk=story_id).first() if story_id else None
        if story is None:
            return Response({"story_id": "Select a valid story."}, status=status.HTTP_400_BAD_REQUEST)

        opening_line = (request.data.get("opening_line") or "").strip()
        if not opening_line:
            return Response({"opening_line": "Opening line is required."}, status=status.HTTP_400_BAD_REQUEST)

        allow_custom = bool(request.data.get("allow_custom"))
        if not allow_custom and not opening_line_in_story(story, opening_line):
            return Response(
                {
                    "opening_line": (
                        "Opening line was not found in the story's chapter text. "
                        "Select a candidate or enable 'Write my own line'."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        difficulty = (request.data.get("difficulty") or "").strip()
        if difficulty and difficulty not in {c[0] for c in DailyPuzzle.DIFFICULTY_CHOICES}:
            return Response({"difficulty": "Invalid difficulty level."}, status=status.HTTP_400_BAD_REQUEST)

        first_chapter = story.chapters.order_by("order").first()
        puzzle = DailyPuzzle.objects.create(
            date=parsed_date,
            story=story,
            opening_line=opening_line,
            source_chapter=first_chapter,
            difficulty=difficulty,
            auto_filled=False,
            created_by=request.user,
        )
        return Response(
            _admin_row_for_puzzle(puzzle, utc_today(), 0, 0),
            status=status.HTTP_201_CREATED,
        )


class AdminPuzzleDetailAPIView(APIView):
    permission_classes = [IsSuperUser]

    def patch(self, request, pk: int):
        puzzle = DailyPuzzle.objects.select_related("story").filter(pk=pk).first()
        if puzzle is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if "date" in request.data:
            parsed_date = _parse_iso_date(request.data.get("date"))
            if parsed_date is None:
                return Response({"date": "Provide a valid YYYY-MM-DD date."}, status=status.HTTP_400_BAD_REQUEST)
            if DailyPuzzle.objects.exclude(pk=puzzle.pk).filter(date=parsed_date).exists():
                return Response({"date": "A puzzle is already scheduled for this date."}, status=status.HTTP_400_BAD_REQUEST)
            puzzle.date = parsed_date

        story = puzzle.story
        if "story_id" in request.data:
            story = Story.objects.filter(pk=request.data.get("story_id")).first()
            if story is None:
                return Response({"story_id": "Select a valid story."}, status=status.HTTP_400_BAD_REQUEST)
            puzzle.story = story
            puzzle.source_chapter = story.chapters.order_by("order").first()

        if "opening_line" in request.data:
            opening_line = (request.data.get("opening_line") or "").strip()
            if not opening_line:
                return Response({"opening_line": "Opening line is required."}, status=status.HTTP_400_BAD_REQUEST)
            allow_custom = bool(request.data.get("allow_custom"))
            if not allow_custom and not opening_line_in_story(story, opening_line):
                return Response(
                    {
                        "opening_line": (
                            "Opening line was not found in the story's chapter text. "
                            "Select a candidate or enable 'Write my own line'."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            puzzle.opening_line = opening_line

        if "difficulty" in request.data:
            difficulty = (request.data.get("difficulty") or "").strip()
            if difficulty and difficulty not in {c[0] for c in DailyPuzzle.DIFFICULTY_CHOICES}:
                return Response({"difficulty": "Invalid difficulty level."}, status=status.HTTP_400_BAD_REQUEST)
            puzzle.difficulty = difficulty

        puzzle.auto_filled = False
        puzzle.save()

        players_count = puzzle.attempts.count()
        solved_count = puzzle.attempts.filter(status=PuzzleAttempt.STATUS_SOLVED).count()
        return Response(_admin_row_for_puzzle(puzzle, utc_today(), players_count, solved_count))

    def delete(self, request, pk: int):
        puzzle = DailyPuzzle.objects.filter(pk=pk).first()
        if puzzle is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if puzzle.date <= utc_today() and puzzle.attempts.exists():
            return Response(
                {"error": "Cannot delete a past or live puzzle that already has player attempts."},
                status=status.HTTP_409_CONFLICT,
            )
        puzzle.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminPuzzleCandidatesAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        story_id = request.query_params.get("story_id")
        story = (
            Story.objects.select_related("author")
            .prefetch_related("genres")
            .filter(pk=story_id)
            .first()
            if story_id
            else None
        )
        if story is None:
            return Response({"detail": "Story not found."}, status=status.HTTP_404_NOT_FOUND)
        candidates = extract_candidates(story, limit=5)
        preview = clues_for(SimpleNamespace(story=story), MAX_GUESSES, request=request)
        return Response({"candidates": candidates, "clues_preview": preview})


class AdminPuzzleAutofillAPIView(APIView):
    permission_classes = [IsSuperUser]

    def post(self, request):
        try:
            days = max(1, min(60, int(request.data.get("days") or 14)))
        except (TypeError, ValueError):
            days = 14

        today = utc_today()
        if not DailyPuzzle.objects.filter(date=today).exists():
            start_date = today + timedelta(days=1)
        else:
            cursor = today + timedelta(days=1)
            while DailyPuzzle.objects.filter(date=cursor).exists():
                cursor += timedelta(days=1)
            start_date = cursor

        result = auto_fill(start_date, days)
        return Response(result)


class AdminPuzzleSummaryAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        today = utc_today()
        scheduled_ahead_days = DailyPuzzle.objects.filter(date__gte=today).count()

        next_14_dates = {today + timedelta(days=i) for i in range(14)}
        filled_next_14 = set(
            DailyPuzzle.objects.filter(date__gte=today, date__lt=today + timedelta(days=14)).values_list(
                "date", flat=True
            )
        )
        empty_in_next_14 = len(next_14_dates - filled_next_14)

        completed_qs = PuzzleAttempt.objects.filter(
            status__in=[PuzzleAttempt.STATUS_SOLVED, PuzzleAttempt.STATUS_FAILED]
        )
        total_completed = completed_qs.count()
        total_solved = completed_qs.filter(status=PuzzleAttempt.STATUS_SOLVED).count()
        average_solve_rate = round((total_solved / total_completed) * 100) if total_completed > 0 else 0

        players_today = PuzzleAttempt.objects.filter(puzzle__date=today).count()

        return Response(
            {
                "scheduled_ahead_days": scheduled_ahead_days,
                "empty_in_next_14": empty_in_next_14,
                "average_solve_rate": average_solve_rate,
                "players_today": players_today,
            }
        )
