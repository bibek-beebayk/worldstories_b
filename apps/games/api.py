from datetime import date, timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.games.models import DailyPuzzle, PuzzleAttempt, PuzzleRotation
from apps.games.services import (
    MAX_GUESSES,
    TriviaBuilder,
    answer_kind_for_type,
    auto_fill,
    clues_for,
    compute_difficulty,
    get_builder,
    get_resolver,
    leak_check,
    result_share_tokens,
    rotation_map,
    stats_for,
    unlocked_count_for,
    utc_today,
    validate_for_approval,
)
from apps.story.api import IsSuperUser
from apps.story.models import Author, Story

WEEKDAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def _get_actor(request):
    user = request.user if getattr(request, "user", None) and request.user.is_authenticated else None
    guest_key = ""
    if user is None:
        raw = (request.headers.get("X-Guest-Key") or "").strip()
        if raw:
            guest_key = raw[:64]
    return user, guest_key


def _get_attempt(puzzle: DailyPuzzle, user, guest_key: str) -> PuzzleAttempt | None:
    if user is not None:
        return PuzzleAttempt.objects.filter(puzzle=puzzle, user=user).first()
    if guest_key:
        return PuzzleAttempt.objects.filter(puzzle=puzzle, guest_key=guest_key).first()
    return None


def _serialize_guess_history(puzzle: DailyPuzzle, attempt: PuzzleAttempt | None, request=None) -> list[dict]:
    if attempt is None or not attempt.guesses:
        return []
    target_ids = [
        g["target_id"]
        for g in attempt.guesses
        if not g.get("skipped") and g.get("target_id") is not None
    ]
    story_map = {}
    author_map = {}
    if target_ids:
        if puzzle.answer_kind == DailyPuzzle.ANSWER_KIND_AUTHOR:
            author_map = {
                a.id: a for a in Author.objects.filter(id__in=target_ids)
            }
        else:
            story_map = {
                s.id: s
                for s in Story.objects.select_related("author").filter(id__in=target_ids)
            }

    resolver = get_resolver(puzzle.answer_kind)
    out = []
    for entry in attempt.guesses:
        if entry.get("skipped"):
            out.append(
                {
                    "target_id": None,
                    "title": "Skipped",
                    "subtitle": "",
                    "initials": "",
                    "correct": False,
                    "matches": {},
                    "skipped": True,
                }
            )
            continue

        tid = entry.get("target_id")
        if puzzle.answer_kind == DailyPuzzle.ANSWER_KIND_AUTHOR:
            author = author_map.get(tid)
            if author:
                attrs = resolver.attributes(author, request=request)
                subtitle_parts = [p for p in (attrs.get("country"), attrs.get("era")) if p]
                title = author.name
                subtitle = ", ".join(subtitle_parts)
                initials = attrs.get("initials") or ""
            else:
                title = "Unknown author"
                subtitle = ""
                initials = ""
        else:
            story = story_map.get(tid)
            if story:
                title = story.title
                subtitle = story.author.name if story.author_id else ""
                initials = ""
            else:
                title = "Unknown story"
                subtitle = ""
                initials = ""

        out.append(
            {
                "target_id": tid,
                "title": title,
                "subtitle": subtitle,
                "initials": initials,
                "correct": bool(entry.get("correct")),
                "matches": entry.get("matches") or {},
                "skipped": False,
            }
        )
    return out


def _serialize_public_puzzle(
    puzzle: DailyPuzzle,
    attempt: PuzzleAttempt | None,
    request=None,
    include_yesterday: bool = False,
) -> dict:
    today = utc_today()
    guesses_used = attempt.guess_count if attempt else 0
    attempt_status = attempt.status if attempt else PuzzleAttempt.STATUS_IN_PROGRESS
    is_complete = attempt_status in (PuzzleAttempt.STATUS_SOLVED, PuzzleAttempt.STATUS_FAILED)

    if is_complete:
        unlocked = len(puzzle.clues or [])
    else:
        unlocked = unlocked_count_for(puzzle, guesses_used)

    payload = {
        "puzzle_number": puzzle.number,
        "date": puzzle.date.isoformat(),
        "puzzle_type": puzzle.puzzle_type,
        "answer_kind": puzzle.answer_kind,
        "max_guesses": MAX_GUESSES,
        "guesses_used": guesses_used,
        "status": attempt_status,
        "is_archive": puzzle.date < today,
        "clues": clues_for(puzzle, unlocked, request=request),
        "guesses": _serialize_guess_history(puzzle, attempt, request=request),
    }

    if is_complete and attempt is not None:
        resolver = get_resolver(puzzle.answer_kind)
        payload["answer"] = resolver.card(puzzle.answer, request=request)
        payload["share_tokens"] = result_share_tokens(puzzle, attempt)

    if include_yesterday:
        yday = today - timedelta(days=1)
        y_puzzle = (
            DailyPuzzle.objects.select_related("answer_story__author", "answer_author")
            .filter(date=yday, status=DailyPuzzle.STATUS_APPROVED)
            .first()
        )
        if y_puzzle and y_puzzle.answer is not None:
            y_resolver = get_resolver(y_puzzle.answer_kind)
            payload["yesterday"] = {
                "date": y_puzzle.date.isoformat(),
                "puzzle_number": y_puzzle.number,
                "puzzle_type": y_puzzle.puzzle_type,
                "answer_kind": y_puzzle.answer_kind,
                "card": y_resolver.card(y_puzzle.answer, request=request),
            }
        else:
            payload["yesterday"] = None

    return payload


def _get_approved_puzzle_for_date(target_date: date) -> DailyPuzzle | None:
    if target_date > utc_today():
        return None
    return (
        DailyPuzzle.objects.select_related("answer_story__author", "answer_author")
        .prefetch_related("answer_story__genres", "answer_story__chapters")
        .filter(date=target_date, status=DailyPuzzle.STATUS_APPROVED)
        .first()
    )


class TodayPuzzleAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        today = utc_today()
        puzzle = _get_approved_puzzle_for_date(today)
        if puzzle is None:
            return Response(
                {"detail": "No puzzle is available for today."},
                status=status.HTTP_404_NOT_FOUND,
            )
        user, guest_key = _get_actor(request)
        attempt = _get_attempt(puzzle, user, guest_key)
        return Response(
            _serialize_public_puzzle(puzzle, attempt, request=request, include_yesterday=True)
        )


class PuzzleByDateAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, puzzle_date: str):
        parsed = _parse_iso_date(puzzle_date)
        if parsed is None:
            return Response({"detail": "Invalid date format."}, status=status.HTTP_400_BAD_REQUEST)
        puzzle = _get_approved_puzzle_for_date(parsed)
        if puzzle is None:
            return Response({"detail": "Puzzle not found."}, status=status.HTTP_404_NOT_FOUND)
        user, guest_key = _get_actor(request)
        attempt = _get_attempt(puzzle, user, guest_key)
        return Response(
            _serialize_public_puzzle(
                puzzle,
                attempt,
                request=request,
                include_yesterday=(parsed == utc_today()),
            )
        )


class PuzzleSearchAPIView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "game_search"

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        if len(q) < 2:
            return Response({"results": [], "kind": "story"})

        date_param = _parse_iso_date(request.query_params.get("date"))
        req_kind = (request.query_params.get("kind") or "").strip()
        is_admin_override = (
            date_param is None
            and req_kind in ("story", "author")
            and getattr(request.user, "is_authenticated", False)
            and getattr(request.user, "is_superuser", False)
        )

        # CRITICAL: do NOT trust client-supplied kind for public requests
        if is_admin_override:
            resolved_kind = req_kind
        else:
            puzzle = _get_approved_puzzle_for_date(date_param or utc_today())
            if puzzle is not None:
                resolved_kind = puzzle.answer_kind
            else:
                resolved_kind = req_kind if req_kind in ("story", "author") else "story"

        resolver = get_resolver(resolved_kind)
        results = resolver.search(q, limit=8, request=request)
        return Response({"kind": resolved_kind, "results": results})


class PuzzleGuessAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        target_date = _parse_iso_date(request.data.get("date")) or utc_today()
        puzzle = _get_approved_puzzle_for_date(target_date)
        if puzzle is None:
            return Response({"detail": "Puzzle not found."}, status=status.HTTP_404_NOT_FOUND)

        user, guest_key = _get_actor(request)
        if user is None and not guest_key:
            return Response(
                {"detail": "Sign in or provide an X-Guest-Key header to play."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw_target_id = request.data.get("target_id")
        try:
            target_id = int(raw_target_id)
        except (TypeError, ValueError):
            return Response({"detail": "Valid target_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        # Reject mismatched kind if client explicitly sent one
        client_kind = request.data.get("answer_kind") or request.data.get("kind")
        if client_kind and client_kind != puzzle.answer_kind:
            return Response(
                {"detail": f"This puzzle expects a {puzzle.answer_kind} guess."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if puzzle.answer_kind == DailyPuzzle.ANSWER_KIND_AUTHOR:
            guess_obj = Author.objects.filter(pk=target_id).first()
            if guess_obj is None:
                return Response(
                    {"detail": "Author not found."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            guess_obj = (
                Story.objects.published()
                .select_related("author")
                .prefetch_related("genres", "chapters")
                .filter(pk=target_id)
                .first()
            )
            if guess_obj is None:
                return Response(
                    {"detail": "Story not found."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        with transaction.atomic():
            if user is not None:
                attempt, _ = PuzzleAttempt.objects.select_for_update().get_or_create(
                    puzzle=puzzle, user=user
                )
            else:
                attempt, _ = PuzzleAttempt.objects.select_for_update().get_or_create(
                    puzzle=puzzle, guest_key=guest_key
                )

            if attempt.status in (PuzzleAttempt.STATUS_SOLVED, PuzzleAttempt.STATUS_FAILED):
                return Response(
                    {"detail": "Puzzle already completed."},
                    status=status.HTTP_409_CONFLICT,
                )

            guesses = list(attempt.guesses or [])
            if any(not g.get("skipped") and g.get("target_id") == target_id for g in guesses):
                return Response(
                    {"error": "already_guessed", "detail": "You already guessed that option."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            resolver = get_resolver(puzzle.answer_kind)
            cmp_result = resolver.compare(puzzle.answer, guess_obj)
            guesses.append(
                {
                    "target_id": target_id,
                    "correct": cmp_result["correct"],
                    "matches": cmp_result["matches"],
                    "skipped": False,
                }
            )
            attempt.guesses = guesses
            attempt.guess_count = len(guesses)

            if cmp_result["correct"]:
                attempt.status = PuzzleAttempt.STATUS_SOLVED
                attempt.completed_at = timezone.now()
            elif attempt.guess_count >= MAX_GUESSES:
                attempt.status = PuzzleAttempt.STATUS_FAILED
                attempt.completed_at = timezone.now()

            attempt.save(update_fields=["guesses", "guess_count", "status", "completed_at"])

        return Response(
            _serialize_public_puzzle(
                puzzle,
                attempt,
                request=request,
                include_yesterday=(puzzle.date == utc_today()),
            )
        )


class PuzzleSkipAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        target_date = _parse_iso_date(request.data.get("date")) or utc_today()
        puzzle = _get_approved_puzzle_for_date(target_date)
        if puzzle is None:
            return Response({"detail": "Puzzle not found."}, status=status.HTTP_404_NOT_FOUND)

        user, guest_key = _get_actor(request)
        if user is None and not guest_key:
            return Response(
                {"detail": "Sign in or provide an X-Guest-Key header to play."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            if user is not None:
                attempt, _ = PuzzleAttempt.objects.select_for_update().get_or_create(
                    puzzle=puzzle, user=user
                )
            else:
                attempt, _ = PuzzleAttempt.objects.select_for_update().get_or_create(
                    puzzle=puzzle, guest_key=guest_key
                )

            if attempt.status in (PuzzleAttempt.STATUS_SOLVED, PuzzleAttempt.STATUS_FAILED):
                return Response(
                    {"detail": "Puzzle already completed."},
                    status=status.HTTP_409_CONFLICT,
                )

            guesses = list(attempt.guesses or [])
            guesses.append(
                {
                    "target_id": None,
                    "correct": False,
                    "matches": {},
                    "skipped": True,
                }
            )
            attempt.guesses = guesses
            attempt.guess_count = len(guesses)
            if attempt.guess_count >= MAX_GUESSES:
                attempt.status = PuzzleAttempt.STATUS_FAILED
                attempt.completed_at = timezone.now()

            attempt.save(update_fields=["guesses", "guess_count", "status", "completed_at"])

        return Response(
            _serialize_public_puzzle(
                puzzle,
                attempt,
                request=request,
                include_yesterday=(puzzle.date == utc_today()),
            )
        )


class PuzzleStatsAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        user, guest_key = _get_actor(request)
        return Response(stats_for(user=user, guest_key=guest_key))


class PuzzleArchiveAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        today = utc_today()
        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except (TypeError, ValueError):
            page = 1
        page_size = 30

        qs = DailyPuzzle.objects.filter(
            date__lt=today, status=DailyPuzzle.STATUS_APPROVED
        ).order_by("-date")
        total = qs.count()
        offset = (page - 1) * page_size
        puzzles = list(qs[offset : offset + page_size])

        user, guest_key = _get_actor(request)
        attempts_by_puzzle = {}
        if puzzles:
            puzzle_ids = [p.id for p in puzzles]
            if user is not None:
                att_qs = PuzzleAttempt.objects.filter(puzzle_id__in=puzzle_ids, user=user)
            elif guest_key:
                att_qs = PuzzleAttempt.objects.filter(puzzle_id__in=puzzle_ids, guest_key=guest_key)
            else:
                att_qs = PuzzleAttempt.objects.none()
            attempts_by_puzzle = {a.puzzle_id: a for a in att_qs}

        results = []
        for p in puzzles:
            att = attempts_by_puzzle.get(p.id)
            results.append(
                {
                    "puzzle_number": p.number,
                    "date": p.date.isoformat(),
                    "puzzle_type": p.puzzle_type,
                    "answer_kind": p.answer_kind,
                    "status": att.status if att else "unplayed",
                    "guess_count": att.guess_count if att else 0,
                }
            )

        return Response(
            {
                "count": total,
                "page": page,
                "page_size": page_size,
                "has_next": (offset + page_size) < total,
                "results": results,
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
            existing_puzzle_ids = set(
                PuzzleAttempt.objects.filter(user=request.user).values_list("puzzle_id", flat=True)
            )
            guest_attempts = list(
                PuzzleAttempt.objects.select_for_update().filter(
                    guest_key=guest_key, user__isnull=True
                )
            )
            for att in guest_attempts:
                if att.puzzle_id in existing_puzzle_ids:
                    skipped += 1
                    continue
                att.user = request.user
                att.guest_key = ""
                att.save(update_fields=["user", "guest_key"])
                existing_puzzle_ids.add(att.puzzle_id)
                merged += 1

        return Response({"merged": merged, "skipped": skipped})


class PuzzleWeekAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        today = utc_today()
        end_date = today + timedelta(days=6)
        approved = {
            p.date: p
            for p in DailyPuzzle.objects.filter(
                date__gte=today,
                date__lte=end_date,
                status=DailyPuzzle.STATUS_APPROVED,
            ).only("date", "puzzle_type", "answer_kind")
        }
        rot = rotation_map()
        days = []
        for offset in range(7):
            d = today + timedelta(days=offset)
            p = approved.get(d)
            if p is not None:
                ptype = p.puzzle_type
                akind = p.answer_kind
            else:
                ptype = rot[d.weekday()]
                akind = answer_kind_for_type(ptype)
            days.append(
                {
                    "date": d.isoformat(),
                    "puzzle_type": ptype,
                    "answer_kind": akind,
                    "is_today": offset == 0,
                }
            )
        return Response({"days": days})


# =====================================================================
# ADMIN ENDPOINTS (/api/admin/games/name-that-story/)
# =====================================================================


def _admin_row_status(puzzle: DailyPuzzle | None, target_date: date, today: date) -> str:
    if puzzle is None:
        return "empty"
    if puzzle.status == DailyPuzzle.STATUS_APPROVED:
        if target_date == today:
            return "live_today"
        if puzzle.auto_filled and puzzle.approved_by_id is None:
            return "auto"
        return "scheduled"
    return "draft"


def _clue1_preview(puzzle: DailyPuzzle) -> str:
    clues = puzzle.clues or []
    if not clues or not isinstance(clues[0], dict):
        return ""
    first = clues[0]
    if first.get("kind") == "text":
        val = (first.get("value") or "").strip()
        return val[:120] + ("..." if len(val) > 120 else "")
    return f"[{first.get('label') or first.get('key') or first.get('kind')}]"


def _answer_label(puzzle: DailyPuzzle) -> str:
    if puzzle.answer_kind == DailyPuzzle.ANSWER_KIND_AUTHOR and puzzle.answer_author_id:
        return puzzle.answer_author.name
    if puzzle.answer_story_id:
        return puzzle.answer_story.title
    return ""


def _serialize_admin_puzzle_detail(puzzle: DailyPuzzle) -> dict:
    today = utc_today()
    attempts_count = puzzle.attempts.count()
    locked_for_edit = (
        puzzle.status == DailyPuzzle.STATUS_APPROVED
        and puzzle.date <= today
        and attempts_count > 0
    )
    errors = validate_for_approval(puzzle)
    leaks = leak_check(puzzle)
    return {
        "id": puzzle.id,
        "number": puzzle.number,
        "date": puzzle.date.isoformat(),
        "puzzle_type": puzzle.puzzle_type,
        "answer_kind": puzzle.answer_kind,
        "answer_id": (
            puzzle.answer_author_id
            if puzzle.answer_kind == DailyPuzzle.ANSWER_KIND_AUTHOR
            else puzzle.answer_story_id
        ),
        "answer_label": _answer_label(puzzle),
        "clues": puzzle.clues or [],
        "source_chapter_id": puzzle.source_chapter_id,
        "difficulty": puzzle.difficulty,
        "approval_status": puzzle.status,
        "status": _admin_row_status(puzzle, puzzle.date, today),
        "auto_filled": puzzle.auto_filled,
        "approved_by_id": puzzle.approved_by_id,
        "approved_at": puzzle.approved_at.isoformat() if puzzle.approved_at else None,
        "attempts_count": attempts_count,
        "locked_for_edit": locked_for_edit,
        "validation_errors": errors,
        "leaks": leaks,
        "created_at": puzzle.created_at.isoformat(),
        "updated_at": puzzle.updated_at.isoformat(),
    }


class AdminPuzzlesListCreateAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        today = utc_today()
        from_date = _parse_iso_date(request.query_params.get("from")) or (today - timedelta(days=1))
        to_date = _parse_iso_date(request.query_params.get("to")) or (today + timedelta(days=13))
        if to_date < from_date:
            to_date = from_date
        if (to_date - from_date).days > 90:
            to_date = from_date + timedelta(days=90)

        status_filter = (request.query_params.get("status") or "").strip()
        type_filter = (request.query_params.get("type") or "").strip()
        q = (request.query_params.get("q") or "").strip().lower()

        puzzles = (
            DailyPuzzle.objects.select_related("answer_story", "answer_author")
            .filter(date__gte=from_date, date__lte=to_date)
            .annotate(
                players_count=Count("attempts"),
                solved_count=Count(
                    "attempts", filter=Q(attempts__status=PuzzleAttempt.STATUS_SOLVED)
                ),
            )
        )
        by_date = {p.date: p for p in puzzles}
        rot = rotation_map()

        rows = []
        cursor = from_date
        while cursor <= to_date:
            p = by_date.get(cursor)
            row_status = _admin_row_status(p, cursor, today)
            if p is not None:
                ptype = p.puzzle_type
                akind = p.answer_kind
                alabel = _answer_label(p)
                preview = _clue1_preview(p)
                players = p.players_count
                solve_rate = round((p.solved_count / players) * 100) if players else None
                item = {
                    "id": p.id,
                    "date": cursor.isoformat(),
                    "number": p.number,
                    "puzzle_type": ptype,
                    "answer_kind": akind,
                    "answer_id": p.answer_author_id if akind == "author" else p.answer_story_id,
                    "answer_label": alabel,
                    "clue1_preview": preview,
                    "difficulty": p.difficulty,
                    "status": row_status,
                    "approval_status": p.status,
                    "auto_filled": p.auto_filled,
                    "players_count": players,
                    "solve_rate": solve_rate,
                }
            else:
                ptype = rot[cursor.weekday()]
                akind = answer_kind_for_type(ptype)
                item = {
                    "id": None,
                    "date": cursor.isoformat(),
                    "number": None,
                    "puzzle_type": ptype,
                    "answer_kind": akind,
                    "answer_id": None,
                    "answer_label": "",
                    "clue1_preview": "",
                    "difficulty": "",
                    "status": "empty",
                    "approval_status": None,
                    "auto_filled": False,
                    "players_count": 0,
                    "solve_rate": None,
                }

            include = True
            if status_filter and item["status"] != status_filter:
                include = False
            if type_filter and item["puzzle_type"] != type_filter:
                include = False
            if q:
                haystack = f"{item['answer_label']} {item['clue1_preview']} {item['date']}".lower()
                if q not in haystack:
                    include = False
            if include:
                rows.append(item)
            cursor += timedelta(days=1)

        return Response(
            {
                "from": from_date.isoformat(),
                "to": to_date.isoformat(),
                "results": rows,
            }
        )

    def post(self, request):
        parsed_date = _parse_iso_date(request.data.get("date"))
        if parsed_date is None:
            return Response({"detail": "Valid ISO date is required."}, status=status.HTTP_400_BAD_REQUEST)

        puzzle_type = (request.data.get("puzzle_type") or DailyPuzzle.PUZZLE_TYPE_OPENING_LINE).strip()
        valid_types = {c[0] for c in DailyPuzzle.PUZZLE_TYPE_CHOICES}
        if puzzle_type not in valid_types:
            return Response({"detail": "Invalid puzzle_type."}, status=status.HTTP_400_BAD_REQUEST)

        answer_kind = answer_kind_for_type(puzzle_type)
        answer_id = request.data.get("answer_id")
        if not answer_id:
            return Response({"detail": "answer_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        answer_story = None
        answer_author = None
        if answer_kind == DailyPuzzle.ANSWER_KIND_AUTHOR:
            answer_author = Author.objects.filter(pk=answer_id).first()
            if answer_author is None:
                return Response({"detail": "Author not found."}, status=status.HTTP_400_BAD_REQUEST)
        else:
            answer_story = Story.objects.filter(pk=answer_id).first()
            if answer_story is None:
                return Response({"detail": "Story not found."}, status=status.HTTP_400_BAD_REQUEST)

        clues = request.data.get("clues")
        if not isinstance(clues, list):
            clues = []

        difficulty = (request.data.get("difficulty") or "").strip()
        source_chapter_id = request.data.get("source_chapter_id") or None

        try:
            puzzle = DailyPuzzle.objects.create(
                date=parsed_date,
                puzzle_type=puzzle_type,
                answer_kind=answer_kind,
                answer_story=answer_story,
                answer_author=answer_author,
                clues=clues,
                source_chapter_id=source_chapter_id,
                difficulty=difficulty,
                status=DailyPuzzle.STATUS_DRAFT,  # Saving NEVER auto-approves
                auto_filled=False,
                created_by=request.user,
            )
        except IntegrityError:
            return Response(
                {"detail": "A puzzle already exists for this date."},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(_serialize_admin_puzzle_detail(puzzle), status=status.HTTP_201_CREATED)


class AdminPuzzleDetailAPIView(APIView):
    permission_classes = [IsSuperUser]

    def _get_puzzle(self, pk: int) -> DailyPuzzle | None:
        return (
            DailyPuzzle.objects.select_related("answer_story__author", "answer_author")
            .prefetch_related("answer_story__genres", "answer_story__chapters")
            .filter(pk=pk)
            .first()
        )

    def get(self, request, pk: int):
        puzzle = self._get_puzzle(pk)
        if puzzle is None:
            return Response({"detail": "Puzzle not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(_serialize_admin_puzzle_detail(puzzle))

    def patch(self, request, pk: int):
        puzzle = self._get_puzzle(pk)
        if puzzle is None:
            return Response({"detail": "Puzzle not found."}, status=status.HTTP_404_NOT_FOUND)

        today = utc_today()
        has_attempts = puzzle.attempts.exists()
        is_locked = (
            puzzle.status == DailyPuzzle.STATUS_APPROVED
            and puzzle.date <= today
            and has_attempts
        )

        content_fields = {"puzzle_type", "answer_kind", "answer_id", "clues", "source_chapter_id"}
        if is_locked and any(field in request.data for field in content_fields):
            return Response(
                {"detail": "Cannot modify clues or answer for a live/past approved puzzle with attempts."},
                status=status.HTTP_409_CONFLICT,
            )

        if "date" in request.data:
            new_date = _parse_iso_date(request.data.get("date"))
            if new_date is None:
                return Response({"detail": "Invalid date."}, status=status.HTTP_400_BAD_REQUEST)
            puzzle.date = new_date

        new_type = request.data.get("puzzle_type", puzzle.puzzle_type)
        valid_types = {c[0] for c in DailyPuzzle.PUZZLE_TYPE_CHOICES}
        if new_type not in valid_types:
            return Response({"detail": "Invalid puzzle_type."}, status=status.HTTP_400_BAD_REQUEST)
        puzzle.puzzle_type = new_type
        puzzle.answer_kind = answer_kind_for_type(new_type)

        if "answer_id" in request.data:
            aid = request.data.get("answer_id")
            if puzzle.answer_kind == DailyPuzzle.ANSWER_KIND_AUTHOR:
                author = Author.objects.filter(pk=aid).first()
                if author is None:
                    return Response({"detail": "Author not found."}, status=status.HTTP_400_BAD_REQUEST)
                puzzle.answer_author = author
                puzzle.answer_story = None
            else:
                story = Story.objects.filter(pk=aid).first()
                if story is None:
                    return Response({"detail": "Story not found."}, status=status.HTTP_400_BAD_REQUEST)
                puzzle.answer_story = story
                puzzle.answer_author = None

        if "clues" in request.data:
            if not isinstance(request.data.get("clues"), list):
                return Response({"detail": "clues must be a list."}, status=status.HTTP_400_BAD_REQUEST)
            puzzle.clues = request.data["clues"]

        if "source_chapter_id" in request.data:
            puzzle.source_chapter_id = request.data.get("source_chapter_id") or None

        if "difficulty" in request.data:
            diff = (request.data.get("difficulty") or "").strip()
            puzzle.difficulty = diff or compute_difficulty(puzzle)

        # Saving never auto-approves; editing an approved future puzzle keeps it draft until re-approved
        if any(field in request.data for field in content_fields) and not is_locked:
            puzzle.status = DailyPuzzle.STATUS_DRAFT
            puzzle.approved_by = None
            puzzle.approved_at = None
            puzzle.auto_filled = False

        try:
            puzzle.save()
        except IntegrityError:
            return Response(
                {"detail": "Constraint conflict (check date uniqueness and answer matching puzzle type)."},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(_serialize_admin_puzzle_detail(puzzle))

    def delete(self, request, pk: int):
        puzzle = self._get_puzzle(pk)
        if puzzle is None:
            return Response({"detail": "Puzzle not found."}, status=status.HTTP_404_NOT_FOUND)
        if puzzle.date <= utc_today() and puzzle.attempts.exists():
            return Response(
                {"detail": "Cannot delete a live or past puzzle that already has player attempts."},
                status=status.HTTP_409_CONFLICT,
            )
        puzzle.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminPuzzleApproveAPIView(APIView):
    permission_classes = [IsSuperUser]

    def post(self, request, pk: int):
        puzzle = (
            DailyPuzzle.objects.select_related("answer_story__author", "answer_author")
            .prefetch_related("answer_story__genres", "answer_story__chapters")
            .filter(pk=pk)
            .first()
        )
        if puzzle is None:
            return Response({"detail": "Puzzle not found."}, status=status.HTTP_404_NOT_FOUND)

        errors = validate_for_approval(puzzle)
        if errors:
            return Response(
                {"detail": "Puzzle failed validation.", "errors": errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        puzzle.status = DailyPuzzle.STATUS_APPROVED
        puzzle.approved_by = request.user
        puzzle.approved_at = timezone.now()
        puzzle.auto_filled = False
        puzzle.save(update_fields=["status", "approved_by", "approved_at", "auto_filled", "updated_at"])
        return Response(_serialize_admin_puzzle_detail(puzzle))


class AdminPuzzleUnapproveAPIView(APIView):
    permission_classes = [IsSuperUser]

    def post(self, request, pk: int):
        puzzle = DailyPuzzle.objects.filter(pk=pk).first()
        if puzzle is None:
            return Response({"detail": "Puzzle not found."}, status=status.HTTP_404_NOT_FOUND)
        if puzzle.attempts.exists():
            return Response(
                {"detail": "Cannot unapprove a puzzle that already has player attempts."},
                status=status.HTTP_409_CONFLICT,
            )
        puzzle.status = DailyPuzzle.STATUS_DRAFT
        puzzle.approved_by = None
        puzzle.approved_at = None
        puzzle.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
        return Response(_serialize_admin_puzzle_detail(puzzle))


class AdminPuzzleValidateAPIView(APIView):
    permission_classes = [IsSuperUser]

    def post(self, request):
        payload = {
            "puzzle_type": request.data.get("puzzle_type", ""),
            "answer_kind": request.data.get("answer_kind"),
            "answer_id": request.data.get("answer_id"),
            "clues": request.data.get("clues") or [],
        }
        errors = validate_for_approval(payload)
        leaks = leak_check(payload)
        return Response(
            {
                "valid": len(errors) == 0,
                "errors": errors,
                "leaks": leaks,
            }
        )


class AdminPuzzleCandidatesAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        puzzle_type = (request.query_params.get("type") or DailyPuzzle.PUZZLE_TYPE_OPENING_LINE).strip()
        answer_kind = (request.query_params.get("answer_kind") or answer_kind_for_type(puzzle_type)).strip()
        answer_id = request.query_params.get("answer_id")
        if not answer_id:
            return Response({"candidates": []})

        try:
            builder = get_builder(puzzle_type)
        except ValueError:
            return Response({"detail": "Invalid puzzle type."}, status=status.HTTP_400_BAD_REQUEST)

        if answer_kind == DailyPuzzle.ANSWER_KIND_AUTHOR:
            answer = Author.objects.filter(pk=answer_id).first()
        else:
            answer = (
                Story.objects.select_related("author")
                .prefetch_related("genres", "chapters")
                .filter(pk=answer_id)
                .first()
            )
        if answer is None:
            return Response({"detail": "Answer not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response({"candidates": builder.candidates(answer)})


class AdminBuildCluesAPIView(APIView):
    permission_classes = [IsSuperUser]

    def post(self, request):
        puzzle_type = (request.data.get("type") or request.data.get("puzzle_type") or "").strip()
        answer_kind = (request.data.get("answer_kind") or answer_kind_for_type(puzzle_type)).strip()
        answer_id = request.data.get("answer_id")
        chosen_text = request.data.get("chosen_text")

        try:
            builder = get_builder(puzzle_type)
        except ValueError:
            return Response({"detail": "Invalid puzzle type."}, status=status.HTTP_400_BAD_REQUEST)

        if answer_kind == DailyPuzzle.ANSWER_KIND_AUTHOR:
            answer = Author.objects.filter(pk=answer_id).first()
        else:
            answer = (
                Story.objects.select_related("author")
                .prefetch_related("genres", "chapters")
                .filter(pk=answer_id)
                .first()
            )
        if answer is None:
            return Response({"detail": "Answer not found."}, status=status.HTTP_404_NOT_FOUND)

        clues = builder.default_clues(answer, chosen_text=chosen_text)
        extra = {}
        if isinstance(builder, TriviaBuilder):
            extra["fallback_attributes"] = builder.fallback_attribute_clues()
        return Response({"clues": clues, **extra})


class AdminPuzzleRotationAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        rot = rotation_map()
        entries = [
            {
                "weekday": day,
                "day_name": WEEKDAY_NAMES[day],
                "puzzle_type": rot[day],
                "answer_kind": answer_kind_for_type(rot[day]),
            }
            for day in range(7)
        ]
        return Response({"rotation": entries})

    def put(self, request):
        items = request.data.get("rotation")
        if not isinstance(items, list) or len(items) != 7:
            return Response(
                {"detail": "rotation must be a list of 7 weekday entries (0-6)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        valid_types = {c[0] for c in DailyPuzzle.PUZZLE_TYPE_CHOICES}
        seen_days = set()
        cleaned = []
        for entry in items:
            try:
                wd = int(entry.get("weekday"))
            except (TypeError, ValueError):
                return Response({"detail": "Invalid weekday."}, status=status.HTTP_400_BAD_REQUEST)
            ptype = (entry.get("puzzle_type") or "").strip()
            if wd < 0 or wd > 6 or wd in seen_days or ptype not in valid_types:
                return Response({"detail": "Invalid rotation entry."}, status=status.HTTP_400_BAD_REQUEST)
            seen_days.add(wd)
            cleaned.append((wd, ptype))

        with transaction.atomic():
            for wd, ptype in cleaned:
                PuzzleRotation.objects.update_or_create(
                    weekday=wd, defaults={"puzzle_type": ptype}
                )

        return self.get(request)


class AdminPuzzleAutofillAPIView(APIView):
    permission_classes = [IsSuperUser]

    def post(self, request):
        try:
            days = int(request.data.get("days", 14))
        except (TypeError, ValueError):
            days = 14
        result = auto_fill(utc_today(), days=days)
        return Response(result)


class AdminPuzzleSummaryAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        today = utc_today()
        horizon = today + timedelta(days=13)
        rot = rotation_map()

        upcoming = {
            p.date: p
            for p in DailyPuzzle.objects.select_related("answer_story", "answer_author")
            .filter(date__gte=today, date__lte=horizon)
        }

        scheduled_ahead_days = 0
        for offset in range(90):
            d = today + timedelta(days=offset)
            if DailyPuzzle.objects.filter(date=d, status=DailyPuzzle.STATUS_APPROVED).exists():
                scheduled_ahead_days += 1
            else:
                break

        empty_in_next_14 = 0
        needs_attention = []
        for offset in range(14):
            d = today + timedelta(days=offset)
            p = upcoming.get(d)
            if p is None:
                empty_in_next_14 += 1
                needs_attention.append(
                    {
                        "date": d.isoformat(),
                        "puzzle_id": None,
                        "puzzle_type": rot[d.weekday()],
                        "reason": "empty",
                        "label": "No puzzle scheduled",
                    }
                )
            elif p.status == DailyPuzzle.STATUS_DRAFT:
                needs_attention.append(
                    {
                        "date": d.isoformat(),
                        "puzzle_id": p.id,
                        "puzzle_type": p.puzzle_type,
                        "reason": "draft",
                        "label": f"Draft: {_answer_label(p) or 'Unassigned'}",
                    }
                )
            elif p.auto_filled and p.approved_by_id is None:
                needs_attention.append(
                    {
                        "date": d.isoformat(),
                        "puzzle_id": p.id,
                        "puzzle_type": p.puzzle_type,
                        "reason": "auto",
                        "label": f"Auto-filled (unreviewed): {_answer_label(p)}",
                    }
                )

        players_today = PuzzleAttempt.objects.filter(puzzle__date=today).count()
        completed_qs = PuzzleAttempt.objects.filter(
            status__in=[PuzzleAttempt.STATUS_SOLVED, PuzzleAttempt.STATUS_FAILED]
        )
        total_completed = completed_qs.count()
        total_solved = completed_qs.filter(status=PuzzleAttempt.STATUS_SOLVED).count()
        avg_solve_rate = round((total_solved / total_completed) * 100) if total_completed else 0

        return Response(
            {
                "scheduled_ahead_days": scheduled_ahead_days,
                "empty_in_next_14": empty_in_next_14,
                "average_solve_rate": avg_solve_rate,
                "players_today": players_today,
                "needs_attention": needs_attention,
            }
        )
