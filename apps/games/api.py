"""Public and admin REST endpoints for the Name That Story daily puzzle game."""

from __future__ import annotations

import csv
from datetime import date, timedelta
import io
import json

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.games.models import DailyPuzzle, PuzzleAttempt, PuzzleBook
from apps.games.services import (
    BookFacts,
    CARD_COVER_SIZE,
    MAX_GUESSES,
    auto_fill,
    clues_for,
    compare_guess,
    extract_candidates,
    facts_from_book,
    facts_from_story,
    find_matching_catalogue_story,
    normalize_identity,
    normalize_whitespace,
    opening_line_in_story,
    result_share_text,
    stats_for,
    utc_today,
)
from apps.story.api import IsSuperUser
from apps.story.models import Audiobook, COUNTRY_CHOICES, Genre, Story
from core.libs.images import get_cover_image_url

_COUNTRY_CODE_SET = {code.upper() for code, _ in COUNTRY_CHOICES}
_COUNTRY_NAME_TO_CODE = {name.lower(): code.upper() for code, name in COUNTRY_CHOICES}
_VALID_LENGTHS = {c[0] for c in PuzzleBook.LENGTH_CHOICES}


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


def _guess_entry_key(entry: dict) -> str | None:
    if entry.get("skipped"):
        return None
    if entry.get("key"):
        return str(entry["key"])
    if entry.get("story_id"):
        return f"story:{entry['story_id']}"
    if entry.get("book_id"):
        return f"book:{entry['book_id']}"
    return None


def _serialize_answer_card(facts: BookFacts, request) -> dict:
    audiobook_slug = None
    if facts.in_catalogue and facts.story_id:
        audiobook = (
            Audiobook.objects.published()
            .filter(story_id=facts.story_id, track_count__gt=0)
            .order_by("id")
            .only("slug")
            .first()
        )
        audiobook_slug = audiobook.slug if audiobook else None

    return {
        "key": facts.key,
        "id": facts.story_id if facts.in_catalogue else facts.book_id,
        "title": facts.title,
        "slug": facts.slug if facts.in_catalogue else None,
        "author": facts.author,
        "year": facts.year,
        "cover": facts.cover_url(request),
        "read_time": facts.read_time if facts.in_catalogue else None,
        "has_audiobook": bool(audiobook_slug),
        "audiobook_slug": audiobook_slug,
    }


def _puzzle_queryset():
    return DailyPuzzle.objects.select_related("story", "story__author", "book").prefetch_related(
        "story__genres", "book__genres"
    )


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

    guessed_story_ids = [g["story_id"] for g in raw_guesses if g.get("story_id")]
    guessed_book_ids = [g["book_id"] for g in raw_guesses if g.get("book_id")]

    stories_by_id = {
        s.id: s
        for s in Story.objects.filter(id__in=guessed_story_ids)
        .select_related("author")
        .only("id", "title", "author__name")
    }
    books_by_id = {
        b.id: b
        for b in PuzzleBook.objects.filter(id__in=guessed_book_ids).only("id", "title", "author_name")
    }

    history = []
    for entry in raw_guesses:
        sid = entry.get("story_id")
        bid = entry.get("book_id")
        title = None
        author = ""
        if sid:
            s = stories_by_id.get(sid)
            title = s.title if s else None
            author = s.author.name if (s and s.author_id) else ""
        elif bid:
            b = books_by_id.get(bid)
            title = b.title if b else None
            author = b.author_name if b else ""

        history.append(
            {
                "key": _guess_entry_key(entry),
                "story_id": sid,
                "book_id": bid,
                "title": title,
                "author": author,
                "correct": bool(entry.get("correct")),
                "matches": entry.get("matches")
                or {"genre": False, "country": False, "era": "different"},
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
        payload["answer"] = _serialize_answer_card(puzzle.answer_facts, request)
        payload["share"] = result_share_text(puzzle, attempt)

    if include_yesterday:
        yesterday_date = utc_today() - timedelta(days=1)
        y_puzzle = _puzzle_queryset().filter(date=yesterday_date).first()
        if y_puzzle is not None:
            payload["yesterday"] = {
                "puzzle_number": y_puzzle.number,
                "date": y_puzzle.date.isoformat(),
                **_serialize_answer_card(y_puzzle.answer_facts, request),
            }
        else:
            payload["yesterday"] = None

    return payload


class TodayPuzzleAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        today = utc_today()
        puzzle = _puzzle_queryset().filter(date=today).first()
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
        puzzle = _puzzle_queryset().filter(date=parsed).first()
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

        q_lower = q.lower()
        stories = list(
            Story.objects.published()
            .filter(title__icontains=q)
            .select_related("author")
            .order_by("title")[:24]
        )
        books = list(PuzzleBook.objects.filter(title__icontains=q).order_by("title")[:24])

        story_identities = {
            (normalize_identity(s.title), normalize_identity(s.author.name if s.author_id else ""))
            for s in stories
        }

        merged: list[dict] = []
        for s in stories:
            merged.append(
                {
                    "key": f"story:{s.id}",
                    "title": s.title,
                    "author": s.author.name if s.author_id else "",
                    "cover_thumb": get_cover_image_url(
                        s.cover_image_file, s.cover_image, request, size=CARD_COVER_SIZE
                    )
                    or "",
                }
            )

        for b in books:
            ident = (b.normalized_title, b.normalized_author)
            if ident in story_identities:
                continue
            merged.append(
                {
                    "key": f"book:{b.id}",
                    "title": b.title,
                    "author": b.author_name,
                    "cover_thumb": get_cover_image_url(
                        b.cover_image_file, b.cover_image, request, size=CARD_COVER_SIZE
                    )
                    or "",
                }
            )

        # Sort identically for both kinds: prefix match first, then alphabetical title.
        merged.sort(key=lambda item: (0 if item["title"].lower().startswith(q_lower) else 1, item["title"].lower()))
        return Response({"results": merged[:8]})


class PuzzleGuessAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        raw_date = request.data.get("date")
        target_date = _parse_iso_date(raw_date) if raw_date else utc_today()
        if target_date is None or target_date > utc_today():
            return Response({"detail": "Puzzle not found."}, status=status.HTTP_404_NOT_FOUND)

        story_id = request.data.get("story_id")
        book_id = request.data.get("book_id")
        has_story = story_id is not None and story_id != ""
        has_book = book_id is not None and book_id != ""
        if has_story == has_book:
            return Response(
                {"error": "Provide exactly one of story_id or book_id."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        puzzle = _puzzle_queryset().filter(date=target_date).first()
        if puzzle is None:
            return Response({"detail": "Puzzle not found."}, status=status.HTTP_404_NOT_FOUND)

        guessed_facts: BookFacts | None = None
        if has_story:
            if not isinstance(story_id, int) or isinstance(story_id, bool):
                return Response({"error": "Provide a valid story_id."}, status=status.HTTP_400_BAD_REQUEST)
            guessed_story = (
                Story.objects.published()
                .prefetch_related("genres")
                .select_related("author")
                .filter(pk=story_id)
                .first()
            )
            if guessed_story is None:
                return Response({"error": "Story not found."}, status=status.HTTP_400_BAD_REQUEST)
            guessed_facts = facts_from_story(guessed_story)
        else:
            if not isinstance(book_id, int) or isinstance(book_id, bool):
                return Response({"error": "Provide a valid book_id."}, status=status.HTTP_400_BAD_REQUEST)
            guessed_book = PuzzleBook.objects.prefetch_related("genres").filter(pk=book_id).first()
            if guessed_book is None:
                return Response({"error": "Book not found."}, status=status.HTTP_400_BAD_REQUEST)
            guessed_facts = facts_from_book(guessed_book)

        attempt, owner_err = _get_or_create_attempt(puzzle, request)
        if owner_err or attempt is None:
            return Response({"error": owner_err}, status=status.HTTP_400_BAD_REQUEST)

        if attempt.status != PuzzleAttempt.STATUS_IN_PROGRESS:
            return Response(
                {"error": "already_completed"},
                status=status.HTTP_409_CONFLICT,
            )

        existing_keys = {k for g in (attempt.guesses or []) if (k := _guess_entry_key(g))}
        if guessed_facts.key in existing_keys:
            return Response(
                {"error": "already_guessed"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        prev_unlocked = min(MAX_GUESSES, attempt.guess_count + 1)
        result = compare_guess(puzzle.answer_facts, guessed_facts)

        guesses = list(attempt.guesses or [])
        guesses.append(
            {
                "key": guessed_facts.key,
                "story_id": guessed_facts.story_id,
                "book_id": guessed_facts.book_id,
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

        puzzle = _puzzle_queryset().filter(date=target_date).first()
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
                "key": None,
                "story_id": None,
                "book_id": None,
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


def _serialize_admin_book(book: PuzzleBook, request, puzzle_count: int | None = None) -> dict:
    cover = get_cover_image_url(
        book.cover_image_file, book.cover_image, request, size=CARD_COVER_SIZE
    )
    return {
        "id": book.id,
        "key": f"book:{book.id}",
        "title": book.title,
        "author_name": book.author_name,
        "genres": [{"id": g.id, "name": g.name} for g in book.genres.order_by("name")],
        "country": book.country,
        "country_display": book.get_country_display() if book.country else "",
        "original_published_year": book.original_published_year,
        "length_label": book.length_label,
        "cover": cover or "",
        "cover_image": book.cover_image or "",
        "puzzle_count": puzzle_count if puzzle_count is not None else book.daily_puzzles.count(),
    }


def _parse_genre_ids(raw) -> list[int]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.startswith("["):
            try:
                raw = json.loads(raw)
            except ValueError:
                return []
        else:
            raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    ids: list[int] = []
    for item in raw:
        try:
            val = int(item)
            if val > 0:
                ids.append(val)
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(ids))


def _admin_row_for_puzzle(
    puzzle: DailyPuzzle, today: date, players_count: int, solved_count: int
) -> dict:
    if puzzle.date == today:
        row_status = "live_today"
    elif puzzle.auto_filled:
        row_status = "auto"
    else:
        row_status = "scheduled"
    solve_rate = round((solved_count / players_count) * 100) if players_count > 0 else None
    facts = puzzle.answer_facts
    return {
        "id": puzzle.id,
        "date": puzzle.date.isoformat(),
        "number": puzzle.number,
        "opening_line": puzzle.opening_line,
        "story": {"id": puzzle.story_id, "title": puzzle.story.title} if puzzle.story_id else None,
        "book": {"id": puzzle.book_id, "title": puzzle.book.title} if puzzle.book_id else None,
        "answer": {
            "key": facts.key,
            "title": facts.title,
            "author": facts.author,
            "in_catalogue": facts.in_catalogue,
        },
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
            _puzzle_queryset()
            .filter(date__gte=from_date, date__lte=to_date)
            .annotate(
                players_count=Count("attempts"),
                solved_count=Count(
                    "attempts", filter=Q(attempts__status=PuzzleAttempt.STATUS_SOLVED)
                ),
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
                    "book": None,
                    "answer": None,
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
                ans = row["answer"] or {}
                hay = f"{row['opening_line']} {ans.get('title', '')} {ans.get('author', '')}".lower()
                if q not in hay:
                    cursor += timedelta(days=1)
                    continue
            rows.append(row)
            cursor += timedelta(days=1)

        return Response({"results": rows})

    def post(self, request):
        parsed_date = _parse_iso_date(request.data.get("date"))
        if parsed_date is None:
            return Response(
                {"date": "Provide a valid YYYY-MM-DD date."}, status=status.HTTP_400_BAD_REQUEST
            )
        if DailyPuzzle.objects.filter(date=parsed_date).exists():
            return Response(
                {"date": "A puzzle is already scheduled for this date."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        story_id = request.data.get("story_id")
        book_id = request.data.get("book_id")
        has_story = story_id is not None and story_id != ""
        has_book = book_id is not None and book_id != ""
        if has_story == has_book:
            return Response(
                {"error": "Provide exactly one of story_id or book_id."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        opening_line = (request.data.get("opening_line") or "").strip()
        if not opening_line:
            return Response(
                {"opening_line": "Opening line is required."}, status=status.HTTP_400_BAD_REQUEST
            )

        story = None
        book = None
        first_chapter = None
        if has_story:
            story = Story.objects.filter(pk=story_id).first()
            if story is None:
                return Response({"story_id": "Select a valid story."}, status=status.HTTP_400_BAD_REQUEST)
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
            first_chapter = story.chapters.order_by("order").first()
        else:
            book = PuzzleBook.objects.filter(pk=book_id).first()
            if book is None:
                return Response({"book_id": "Select a valid external book."}, status=status.HTTP_400_BAD_REQUEST)

        difficulty = (request.data.get("difficulty") or "").strip()
        if difficulty and difficulty not in {c[0] for c in DailyPuzzle.DIFFICULTY_CHOICES}:
            return Response({"difficulty": "Invalid difficulty level."}, status=status.HTTP_400_BAD_REQUEST)

        puzzle = DailyPuzzle.objects.create(
            date=parsed_date,
            story=story,
            book=book,
            opening_line=opening_line,
            source_chapter=first_chapter,
            difficulty=difficulty,
            auto_filled=False,
            created_by=request.user,
        )
        puzzle = _puzzle_queryset().get(pk=puzzle.pk)
        return Response(
            _admin_row_for_puzzle(puzzle, utc_today(), 0, 0),
            status=status.HTTP_201_CREATED,
        )


class AdminPuzzleDetailAPIView(APIView):
    permission_classes = [IsSuperUser]

    def patch(self, request, pk: int):
        puzzle = _puzzle_queryset().filter(pk=pk).first()
        if puzzle is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if "date" in request.data:
            parsed_date = _parse_iso_date(request.data.get("date"))
            if parsed_date is None:
                return Response(
                    {"date": "Provide a valid YYYY-MM-DD date."}, status=status.HTTP_400_BAD_REQUEST
                )
            if DailyPuzzle.objects.exclude(pk=puzzle.pk).filter(date=parsed_date).exists():
                return Response(
                    {"date": "A puzzle is already scheduled for this date."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            puzzle.date = parsed_date

        story_supplied = "story_id" in request.data and request.data.get("story_id") not in (None, "")
        book_supplied = "book_id" in request.data and request.data.get("book_id") not in (None, "")
        if story_supplied and book_supplied:
            return Response(
                {"error": "Provide only one of story_id or book_id."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if story_supplied:
            story = Story.objects.filter(pk=request.data.get("story_id")).first()
            if story is None:
                return Response({"story_id": "Select a valid story."}, status=status.HTTP_400_BAD_REQUEST)
            puzzle.story = story
            puzzle.book = None
            puzzle.source_chapter = story.chapters.order_by("order").first()
        elif book_supplied:
            book = PuzzleBook.objects.filter(pk=request.data.get("book_id")).first()
            if book is None:
                return Response({"book_id": "Select a valid external book."}, status=status.HTTP_400_BAD_REQUEST)
            puzzle.book = book
            puzzle.story = None
            puzzle.source_chapter = None

        if "opening_line" in request.data:
            opening_line = (request.data.get("opening_line") or "").strip()
            if not opening_line:
                return Response(
                    {"opening_line": "Opening line is required."}, status=status.HTTP_400_BAD_REQUEST
                )
            if puzzle.story_id:
                allow_custom = bool(request.data.get("allow_custom"))
                if not allow_custom and not opening_line_in_story(puzzle.story, opening_line):
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
                return Response(
                    {"difficulty": "Invalid difficulty level."}, status=status.HTTP_400_BAD_REQUEST
                )
            puzzle.difficulty = difficulty

        puzzle.auto_filled = False
        puzzle.save()
        puzzle = _puzzle_queryset().get(pk=puzzle.pk)

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
        book_id = request.query_params.get("book_id")
        if book_id:
            book = PuzzleBook.objects.prefetch_related("genres").filter(pk=book_id).first()
            if book is None:
                return Response({"detail": "Book not found."}, status=status.HTTP_404_NOT_FOUND)
            return Response(
                {
                    "candidates": [],
                    "clues_preview": clues_for(facts_from_book(book), MAX_GUESSES, request=request),
                }
            )

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
        preview = clues_for(facts_from_story(story), MAX_GUESSES, request=request)
        return Response({"candidates": candidates, "clues_preview": preview})


class AdminPuzzleBooksListCreateAPIView(APIView):
    permission_classes = [IsSuperUser]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        qs = PuzzleBook.objects.prefetch_related("genres").annotate(
            p_count=Count("daily_puzzles")
        )
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(author_name__icontains=q))
        books = list(qs.order_by("title", "id")[:50])
        return Response(
            {"results": [_serialize_admin_book(b, request, b.p_count) for b in books]}
        )

    def post(self, request):
        title = normalize_whitespace(request.data.get("title") or "")
        author_name = normalize_whitespace(request.data.get("author_name") or "")
        if not title:
            return Response({"title": "Title is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not author_name:
            return Response(
                {"author_name": "Author name is required."}, status=status.HTTP_400_BAD_REQUEST
            )

        raw_genres = (
            request.data.getlist("genres")
            if hasattr(request.data, "getlist") and len(request.data.getlist("genres")) > 1
            else request.data.get("genres")
        )
        genre_ids = _parse_genre_ids(raw_genres)
        valid_genres = list(Genre.objects.filter(id__in=genre_ids))
        if not valid_genres:
            return Response(
                {"genres": "Select at least one genre."}, status=status.HTTP_400_BAD_REQUEST
            )

        existing_story = find_matching_catalogue_story(title, author_name)
        if existing_story is not None:
            return Response(
                {
                    "title": (
                        f"'{existing_story.title}' already exists in your catalogue. "
                        "Choose it under 'From my catalogue' instead."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if PuzzleBook.objects.filter(
            normalized_title=normalize_identity(title),
            normalized_author=normalize_identity(author_name),
        ).exists():
            return Response(
                {"title": "An external book with this title and author already exists."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        country = (request.data.get("country") or "").strip().upper()
        if country and country not in _COUNTRY_CODE_SET:
            return Response({"country": "Invalid 2-letter country code."}, status=status.HTTP_400_BAD_REQUEST)

        raw_year = request.data.get("original_published_year")
        year = None
        if raw_year not in (None, ""):
            try:
                year = int(raw_year)
                if year <= 0 or year > 3000:
                    raise ValueError()
            except (TypeError, ValueError):
                return Response(
                    {"original_published_year": "Enter a valid publication year."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        length_val = (request.data.get("length_label") or PuzzleBook.LENGTH_NOVEL).strip()
        if length_val not in _VALID_LENGTHS:
            return Response(
                {"length_label": "Length must be Short story, Novella, or Novel."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cover_image = (request.data.get("cover_image") or "").strip() or None
        cover_file = request.FILES.get("cover_image_file")

        book = PuzzleBook.objects.create(
            title=title,
            author_name=author_name,
            country=country,
            original_published_year=year,
            length_label=length_val,
            cover_image=cover_image,
            cover_image_file=cover_file,
        )
        book.genres.set(valid_genres)
        return Response(_serialize_admin_book(book, request, 0), status=status.HTTP_201_CREATED)


class AdminPuzzleBookDetailAPIView(APIView):
    permission_classes = [IsSuperUser]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def patch(self, request, pk: int):
        book = PuzzleBook.objects.prefetch_related("genres").filter(pk=pk).first()
        if book is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        title = normalize_whitespace(request.data.get("title", book.title))
        author_name = normalize_whitespace(request.data.get("author_name", book.author_name))
        if not title:
            return Response({"title": "Title is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not author_name:
            return Response(
                {"author_name": "Author name is required."}, status=status.HTTP_400_BAD_REQUEST
            )

        existing_story = find_matching_catalogue_story(title, author_name)
        if existing_story is not None:
            return Response(
                {
                    "title": (
                        f"'{existing_story.title}' already exists in your catalogue. "
                        "Choose it under 'From my catalogue' instead."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if (
            PuzzleBook.objects.exclude(pk=book.pk)
            .filter(
                normalized_title=normalize_identity(title),
                normalized_author=normalize_identity(author_name),
            )
            .exists()
        ):
            return Response(
                {"title": "An external book with this title and author already exists."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        book.title = title
        book.author_name = author_name

        if "country" in request.data:
            country = (request.data.get("country") or "").strip().upper()
            if country and country not in _COUNTRY_CODE_SET:
                return Response(
                    {"country": "Invalid 2-letter country code."}, status=status.HTTP_400_BAD_REQUEST
                )
            book.country = country

        if "original_published_year" in request.data:
            raw_year = request.data.get("original_published_year")
            if raw_year in (None, ""):
                book.original_published_year = None
            else:
                try:
                    year = int(raw_year)
                    if year <= 0 or year > 3000:
                        raise ValueError()
                    book.original_published_year = year
                except (TypeError, ValueError):
                    return Response(
                        {"original_published_year": "Enter a valid publication year."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        if "length_label" in request.data:
            length_val = (request.data.get("length_label") or "").strip()
            if length_val not in _VALID_LENGTHS:
                return Response(
                    {"length_label": "Length must be Short story, Novella, or Novel."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            book.length_label = length_val

        if "cover_image" in request.data:
            book.cover_image = (request.data.get("cover_image") or "").strip() or None
        if "cover_image_file" in request.FILES:
            book.cover_image_file = request.FILES["cover_image_file"]

        if "genres" in request.data:
            raw_genres = (
                request.data.getlist("genres")
                if hasattr(request.data, "getlist") and len(request.data.getlist("genres")) > 1
                else request.data.get("genres")
            )
            genre_ids = _parse_genre_ids(raw_genres)
            valid_genres = list(Genre.objects.filter(id__in=genre_ids))
            if not valid_genres:
                return Response(
                    {"genres": "Select at least one genre."}, status=status.HTTP_400_BAD_REQUEST
                )
            book.genres.set(valid_genres)

        book.save()
        return Response(_serialize_admin_book(book, request))

    def delete(self, request, pk: int):
        book = PuzzleBook.objects.filter(pk=pk).first()
        if book is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if book.daily_puzzles.exists():
            return Response(
                {"error": "Cannot delete a book that is used by a scheduled or past puzzle."},
                status=status.HTTP_409_CONFLICT,
            )
        book.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminPuzzleBooksImportCsvAPIView(APIView):
    permission_classes = [IsSuperUser]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        uploaded = request.FILES.get("file")
        if uploaded is None:
            return Response({"error": "Upload a CSV file in the 'file' field."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            decoded = uploaded.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            return Response({"error": "File must be UTF-8 encoded CSV."}, status=status.HTTP_400_BAD_REQUEST)

        reader = csv.DictReader(io.StringIO(decoded))
        genres_by_name = {g.name.lower(): g for g in Genre.objects.all()}

        created = 0
        skipped = 0
        errors: list[dict] = []

        for row_num, raw_row in enumerate(reader, start=2):
            row = {str(k or "").strip().lower(): (v or "").strip() for k, v in raw_row.items()}
            title = normalize_whitespace(row.get("title", ""))
            author_name = normalize_whitespace(row.get("author_name", ""))
            if not title or not author_name:
                errors.append({"row": row_num, "error": "title and author_name are required."})
                continue

            if find_matching_catalogue_story(title, author_name) is not None:
                skipped += 1
                continue

            if PuzzleBook.objects.filter(
                normalized_title=normalize_identity(title),
                normalized_author=normalize_identity(author_name),
            ).exists():
                skipped += 1
                continue

            genre_tokens = [
                g.strip().lower()
                for g in row.get("genres", "").replace("|", ",").split(",")
                if g.strip()
            ]
            matched_genres = [genres_by_name[t] for t in genre_tokens if t in genres_by_name]
            if not matched_genres:
                errors.append(
                    {
                        "row": row_num,
                        "error": f"No matching genres found for '{row.get('genres', '')}'.",
                    }
                )
                continue

            raw_country = row.get("country", "")
            country = ""
            if raw_country:
                upper_c = raw_country.upper()
                if upper_c in _COUNTRY_CODE_SET:
                    country = upper_c
                elif raw_country.lower() in _COUNTRY_NAME_TO_CODE:
                    country = _COUNTRY_NAME_TO_CODE[raw_country.lower()]
                else:
                    errors.append({"row": row_num, "error": f"Unknown country '{raw_country}'."})
                    continue

            raw_year = row.get("original_published_year", "")
            year = None
            if raw_year:
                try:
                    year = int(raw_year)
                except ValueError:
                    errors.append({"row": row_num, "error": f"Invalid year '{raw_year}'."})
                    continue

            length_val = row.get("length_label", "") or PuzzleBook.LENGTH_NOVEL
            if length_val not in _VALID_LENGTHS:
                errors.append(
                    {
                        "row": row_num,
                        "error": f"Invalid length_label '{length_val}' (use Short story, Novella, or Novel).",
                    }
                )
                continue

            cover_link = row.get("cover_image_link", "") or None

            try:
                with transaction.atomic():
                    book = PuzzleBook.objects.create(
                        title=title,
                        author_name=author_name,
                        country=country,
                        original_published_year=year,
                        length_label=length_val,
                        cover_image=cover_link,
                    )
                    book.genres.set(matched_genres)
                    created += 1
            except IntegrityError:
                skipped += 1

        return Response({"created": created, "skipped": skipped, "errors": errors})


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
            DailyPuzzle.objects.filter(
                date__gte=today, date__lt=today + timedelta(days=14)
            ).values_list("date", flat=True)
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
