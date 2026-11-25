import base64
import csv
import io
import json
import re
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.db.models.functions import Length
from django.http import StreamingHttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.games.api import _get_actor, _parse_iso_date
from apps.games.crossword import (
    GRID_SIZE,
    MAX_ELAPSED_SECONDS,
    apply_difficulty_to_puzzle,
    auto_generate,
    check_cells,
    compute_bank_health,
    compute_difficulty,
    draft_clues,
    empty_letters_for_pattern,
    generate_fill,
    grid_state,
    is_solved,
    number_grid,
    reveal_cell,
    share_tokens,
    stats_for,
    validate_solution,
)
from apps.games.models import ClueBank, MiniAttempt, MiniCrossword
from apps.games.services.engine import utc_today
from apps.stats.streaks import compute_streak
from apps.story.api import IsSuperUser
from apps.story.models import Story
from core.libs.images import get_cover_image_url

_LETTER_CELL_RE = re.compile(r"^[A-Z.#]$")


def _get_approved_mini(target_date):
    if target_date > utc_today():
        return None
    return MiniCrossword.objects.filter(
        date=target_date, status=MiniCrossword.STATUS_APPROVED
    ).first()


def _get_mini_attempt(puzzle: MiniCrossword, user, guest_key: str) -> MiniAttempt | None:
    if user is not None:
        return MiniAttempt.objects.filter(puzzle=puzzle, user=user).first()
    if guest_key:
        return MiniAttempt.objects.filter(puzzle=puzzle, guest_key=guest_key).first()
    return None


def _validate_and_merge_letters(
    puzzle: MiniCrossword, attempt: MiniAttempt, incoming_letters
) -> tuple[list[str] | None, str | None]:
    if not isinstance(incoming_letters, list) or len(incoming_letters) != GRID_SIZE:
        return None, f"letters must be a list of {GRID_SIZE} strings."

    revealed_set = {(p[0], p[1]) for p in (attempt.revealed or []) if len(p) == 2}
    cleaned: list[str] = []
    for r in range(GRID_SIZE):
        row = incoming_letters[r]
        if not isinstance(row, str) or len(row) != GRID_SIZE:
            return None, f"Row {r + 1} must be a string of length {GRID_SIZE}."
        row_upper = row.upper()
        out_chars: list[str] = []
        for c in range(GRID_SIZE):
            ch = row_upper[c]
            if not _LETTER_CELL_RE.match(ch):
                return None, f"Invalid character at ({r + 1},{c + 1})."
            if puzzle.pattern[r][c] == "#":
                if ch != "#":
                    return None, f"Black cell mismatch at ({r + 1},{c + 1})."
                out_chars.append("#")
            else:
                if ch == "#":
                    return None, f"White cell cannot be '#' at ({r + 1},{c + 1})."
                if (r, c) in revealed_set:
                    # Never allow overwriting revealed cells
                    out_chars.append(puzzle.solution[r][c])
                else:
                    out_chars.append(ch)
        cleaned.append("".join(out_chars))
    return cleaned, None


def _serialize_public_mini(
    puzzle: MiniCrossword,
    attempt: MiniAttempt | None,
    request=None,
) -> dict:
    numbered = number_grid(puzzle.pattern)
    entry_len_map = {
        (e["number"], e["direction"]): e["length"] for e in numbered["entries"]
    }

    # CRITICAL: NEVER include clue answer or story_id before solved
    public_clues = [
        {
            "number": c["number"],
            "direction": c["direction"],
            "clue": c.get("clue", ""),
            "length": entry_len_map.get((c["number"], c["direction"]), 0),
        }
        for c in (puzzle.clues or [])
    ]

    attempt_payload = None
    is_complete = False
    if attempt is not None:
        is_complete = attempt.status == MiniAttempt.STATUS_SOLVED
        attempt_payload = {
            "status": attempt.status,
            "letters": grid_state(attempt),
            "revealed": attempt.revealed or [],
            "started_at": attempt.started_at.isoformat(),
            "hints_used": attempt.hints_used,
            "elapsed_seconds": attempt.elapsed_seconds,
            "is_archive": attempt.is_archive,
        }

    payload = {
        "number": puzzle.number,
        "date": puzzle.date.isoformat(),
        "title": puzzle.title,
        "difficulty": puzzle.difficulty or "",
        "pattern": puzzle.pattern,
        "numbering": numbered["numbering"],
        "entries": numbered["entries"],
        "clues": public_clues,
        "is_archive": puzzle.date < utc_today(),
        "attempt": attempt_payload,
    }

    if is_complete and attempt is not None:
        payload["solution"] = puzzle.solution
        payload["share_tokens"] = share_tokens(puzzle, attempt)

        story_ids = [
            c["story_id"] for c in (puzzle.clues or []) if c.get("story_id")
        ]
        story_map = {}
        if story_ids:
            for st in Story.objects.published().select_related("author").filter(id__in=story_ids):
                story_map[st.id] = st

        linked_stories = []
        seen_story_ids = set()
        for c in puzzle.clues or []:
            sid = c.get("story_id")
            if sid and sid in story_map and sid not in seen_story_ids:
                seen_story_ids.add(sid)
                st = story_map[sid]
                linked_stories.append(
                    {
                        "number": c["number"],
                        "direction": c["direction"],
                        "answer": c.get("answer", ""),
                        "story_id": st.id,
                        "title": st.title,
                        "slug": st.slug,
                        "author": st.author.name if st.author_id else "",
                        "cover": get_cover_image_url(
                            st.cover_image_file, st.cover_image, request, size="480x640"
                        ),
                    }
                )
        payload["linked_stories"] = linked_stories

    return payload


class MiniTodayAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        puzzle = _get_approved_mini(utc_today())
        if puzzle is None:
            return Response(
                {"detail": "No mini crossword is available for today."},
                status=status.HTTP_404_NOT_FOUND,
            )
        user, guest_key = _get_actor(request)
        attempt = _get_mini_attempt(puzzle, user, guest_key)
        return Response(_serialize_public_mini(puzzle, attempt, request=request))


class MiniTodaySummaryAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        today = utc_today()
        puzzle = _get_approved_mini(today)
        if puzzle is None:
            resp = Response(
                {"detail": "No mini crossword is available for today."},
                status=status.HTTP_404_NOT_FOUND,
            )
            resp["Cache-Control"] = "private, no-store"
            return resp

        user, guest_key = _get_actor(request)
        attempt = _get_mini_attempt(puzzle, user, guest_key)

        safe_pattern = [
            "".join("#" if ch == "#" else "." for ch in str(row))
            for row in (puzzle.pattern or [])
        ]
        white_cells = sum(row.count(".") for row in safe_pattern)

        if attempt is None:
            attempt_status = "not_started"
            filled_cells = 0
            elapsed_seconds = None
            hints_used = 0
        else:
            attempt_status = attempt.status
            letters = grid_state(attempt)
            filled_cells = sum(
                1
                for r in range(min(GRID_SIZE, len(safe_pattern), len(letters)))
                for c in range(min(GRID_SIZE, len(safe_pattern[r]), len(letters[r])))
                if safe_pattern[r][c] == "." and letters[r][c] not in (".", "#")
            )
            if attempt.elapsed_seconds is not None:
                elapsed_seconds = attempt.elapsed_seconds
            elif attempt.status == MiniAttempt.STATUS_IN_PROGRESS and attempt.started_at:
                delta = int((timezone.now() - attempt.started_at).total_seconds())
                elapsed_seconds = max(0, min(MAX_ELAPSED_SECONDS, delta))
            else:
                elapsed_seconds = None
            hints_used = attempt.hints_used

        current_streak = 0
        if user is not None and getattr(user, "is_authenticated", False):
            solved_dates = set(
                MiniAttempt.objects.filter(
                    user=user,
                    status=MiniAttempt.STATUS_SOLVED,
                    is_archive=False,
                ).values_list("puzzle__date", flat=True)
            )
            current_streak, _ = compute_streak(solved_dates, today)
        elif guest_key:
            solved_dates = set(
                MiniAttempt.objects.filter(
                    guest_key=guest_key,
                    status=MiniAttempt.STATUS_SOLVED,
                    is_archive=False,
                ).values_list("puzzle__date", flat=True)
            )
            current_streak, _ = compute_streak(solved_dates, today)

        resp = Response(
            {
                "date": puzzle.date.isoformat(),
                "number": puzzle.number,
                "title": puzzle.title,
                "difficulty": puzzle.difficulty or "",
                "pattern": safe_pattern,
                "white_cells": white_cells,
                "status": attempt_status,
                "filled_cells": filled_cells,
                "elapsed_seconds": elapsed_seconds,
                "hints_used": hints_used,
                "current_streak": current_streak,
            }
        )
        resp["Cache-Control"] = "private, no-store"
        return resp


class MiniByDateAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, puzzle_date: str):
        parsed = _parse_iso_date(puzzle_date)
        if parsed is None:
            return Response({"detail": "Invalid date format."}, status=status.HTTP_400_BAD_REQUEST)
        puzzle = _get_approved_mini(parsed)
        if puzzle is None:
            return Response({"detail": "Mini crossword not found."}, status=status.HTTP_404_NOT_FOUND)
        user, guest_key = _get_actor(request)
        attempt = _get_mini_attempt(puzzle, user, guest_key)
        return Response(_serialize_public_mini(puzzle, attempt, request=request))


class MiniStartAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        target_date = _parse_iso_date(request.data.get("date")) or utc_today()
        puzzle = _get_approved_mini(target_date)
        if puzzle is None:
            return Response({"detail": "Mini crossword not found."}, status=status.HTTP_404_NOT_FOUND)

        user, guest_key = _get_actor(request)
        if user is None and not guest_key:
            return Response(
                {"detail": "Sign in or provide an X-Guest-Key header to play."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        defaults = {
            "letters": empty_letters_for_pattern(puzzle.pattern),
            "revealed": [],
            "is_archive": puzzle.date < utc_today(),
        }
        with transaction.atomic():
            if user is not None:
                attempt, _ = MiniAttempt.objects.get_or_create(
                    puzzle=puzzle, user=user, defaults=defaults
                )
            else:
                attempt, _ = MiniAttempt.objects.get_or_create(
                    puzzle=puzzle, guest_key=guest_key, defaults=defaults
                )

        return Response(_serialize_public_mini(puzzle, attempt, request=request))


class MiniSaveAPIView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "mini_save"

    def put(self, request):
        target_date = _parse_iso_date(request.data.get("date")) or utc_today()
        puzzle = _get_approved_mini(target_date)
        if puzzle is None:
            return Response({"detail": "Mini crossword not found."}, status=status.HTTP_404_NOT_FOUND)

        user, guest_key = _get_actor(request)
        attempt = _get_mini_attempt(puzzle, user, guest_key)
        if attempt is None:
            return Response({"detail": "Start the puzzle before saving."}, status=status.HTTP_400_BAD_REQUEST)

        if attempt.status == MiniAttempt.STATUS_SOLVED:
            return Response({"saved": False, "letters": grid_state(attempt)})

        cleaned, err = _validate_and_merge_letters(puzzle, attempt, request.data.get("letters"))
        if err:
            return Response({"detail": err}, status=status.HTTP_400_BAD_REQUEST)

        attempt.letters = cleaned
        attempt.save(update_fields=["letters", "last_saved_at"])
        return Response({"saved": True, "letters": cleaned})


class MiniCheckAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        target_date = _parse_iso_date(request.data.get("date")) or utc_today()
        puzzle = _get_approved_mini(target_date)
        if puzzle is None:
            return Response({"detail": "Mini crossword not found."}, status=status.HTTP_404_NOT_FOUND)

        user, guest_key = _get_actor(request)
        attempt = _get_mini_attempt(puzzle, user, guest_key)
        if attempt is None:
            return Response({"detail": "Start the puzzle first."}, status=status.HTTP_400_BAD_REQUEST)

        # Optional latest letters can be included in check request
        if "letters" in request.data and attempt.status != MiniAttempt.STATUS_SOLVED:
            cleaned, err = _validate_and_merge_letters(puzzle, attempt, request.data.get("letters"))
            if err:
                return Response({"detail": err}, status=status.HTTP_400_BAD_REQUEST)
            attempt.letters = cleaned

        scope = (request.data.get("scope") or "grid").strip()
        if scope not in ("word", "grid"):
            return Response({"detail": "scope must be 'word' or 'grid'."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            row = int(request.data.get("row", 0))
            col = int(request.data.get("col", 0))
        except (TypeError, ValueError):
            row, col = 0, 0
        direction = (request.data.get("direction") or "across").strip()

        wrong = check_cells(puzzle, attempt, scope, row=row, col=col, direction=direction)
        attempt.checks_used += 1
        attempt.save(update_fields=["letters", "checks_used", "last_saved_at"])
        return Response({"wrong": wrong})


class MiniRevealAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        target_date = _parse_iso_date(request.data.get("date")) or utc_today()
        puzzle = _get_approved_mini(target_date)
        if puzzle is None:
            return Response({"detail": "Mini crossword not found."}, status=status.HTTP_404_NOT_FOUND)

        user, guest_key = _get_actor(request)
        attempt = _get_mini_attempt(puzzle, user, guest_key)
        if attempt is None:
            return Response({"detail": "Start the puzzle first."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            row = int(request.data.get("row"))
            col = int(request.data.get("col"))
        except (TypeError, ValueError):
            return Response({"detail": "Valid row and col are required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            letter = reveal_cell(puzzle, attempt, row, col)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "letter": letter,
                "hints_used": attempt.hints_used,
                "revealed": attempt.revealed,
                "letters": attempt.letters,
            }
        )


class MiniSubmitAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        target_date = _parse_iso_date(request.data.get("date")) or utc_today()
        puzzle = _get_approved_mini(target_date)
        if puzzle is None:
            return Response({"detail": "Mini crossword not found."}, status=status.HTTP_404_NOT_FOUND)

        user, guest_key = _get_actor(request)
        attempt = _get_mini_attempt(puzzle, user, guest_key)
        if attempt is None:
            return Response({"detail": "Start the puzzle first."}, status=status.HTTP_400_BAD_REQUEST)

        if attempt.status == MiniAttempt.STATUS_SOLVED:
            return Response(
                {
                    "solved": True,
                    "puzzle": _serialize_public_mini(puzzle, attempt, request=request),
                }
            )

        cleaned, err = _validate_and_merge_letters(puzzle, attempt, request.data.get("letters"))
        if err:
            return Response({"detail": err}, status=status.HTTP_400_BAD_REQUEST)

        attempt.letters = cleaned
        if is_solved(puzzle, cleaned):
            now = timezone.now()
            elapsed = max(0, int((now - attempt.started_at).total_seconds()))
            attempt.status = MiniAttempt.STATUS_SOLVED
            attempt.completed_at = now
            attempt.elapsed_seconds = min(elapsed, MAX_ELAPSED_SECONDS)
            attempt.save(
                update_fields=[
                    "letters",
                    "status",
                    "completed_at",
                    "elapsed_seconds",
                    "last_saved_at",
                ]
            )
            return Response(
                {
                    "solved": True,
                    "puzzle": _serialize_public_mini(puzzle, attempt, request=request),
                }
            )

        attempt.save(update_fields=["letters", "last_saved_at"])
        return Response({"solved": False})


class MiniStatsAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        user, guest_key = _get_actor(request)
        return Response(stats_for(user=user, guest_key=guest_key))


class MiniArchiveAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        today = utc_today()
        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except (TypeError, ValueError):
            page = 1
        page_size = 30

        qs = MiniCrossword.objects.filter(
            date__lt=today, status=MiniCrossword.STATUS_APPROVED
        ).order_by("-date")
        total = qs.count()
        offset = (page - 1) * page_size
        puzzles = list(qs[offset : offset + page_size])

        user, guest_key = _get_actor(request)
        attempts_map = {}
        if puzzles:
            pids = [p.id for p in puzzles]
            if user is not None:
                att_qs = MiniAttempt.objects.filter(puzzle_id__in=pids, user=user)
            elif guest_key:
                att_qs = MiniAttempt.objects.filter(puzzle_id__in=pids, guest_key=guest_key)
            else:
                att_qs = MiniAttempt.objects.none()
            attempts_map = {a.puzzle_id: a for a in att_qs}

        results = []
        for p in puzzles:
            att = attempts_map.get(p.id)
            results.append(
                {
                    "number": p.number,
                    "date": p.date.isoformat(),
                    "title": p.title,
                    "difficulty": p.difficulty or "",
                    "status": att.status if att else "unplayed",
                    "elapsed_seconds": att.elapsed_seconds if att else None,
                    "hints_used": att.hints_used if att else 0,
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


class MiniMergeGuestAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        guest_key = (request.data.get("guest_key") or "").strip()[:64]
        if not guest_key:
            return Response({"merged": 0, "skipped": 0})

        merged = 0
        skipped = 0
        with transaction.atomic():
            existing_pids = set(
                MiniAttempt.objects.filter(user=request.user).values_list("puzzle_id", flat=True)
            )
            guest_atts = list(
                MiniAttempt.objects.select_for_update().filter(
                    guest_key=guest_key, user__isnull=True
                )
            )
            for att in guest_atts:
                if att.puzzle_id in existing_pids:
                    skipped += 1
                    continue
                att.user = request.user
                att.guest_key = ""
                att.save(update_fields=["user", "guest_key"])
                existing_pids.add(att.puzzle_id)
                merged += 1

        return Response({"merged": merged, "skipped": skipped})


# =====================================================================
# ADMIN ENDPOINTS (/api/admin/games/mini-crossword/)
# =====================================================================


def _admin_mini_status(puzzle: MiniCrossword | None, target_date, today) -> str:
    if puzzle is None:
        return "empty"
    if puzzle.status == MiniCrossword.STATUS_APPROVED:
        if target_date == today:
            return "live_today"
        return "scheduled"
    if puzzle.auto_generated:
        return "auto"
    return "draft"


def _filled_clues_label(puzzle: MiniCrossword) -> str:
    numbered = number_grid(puzzle.pattern)
    total = len(numbered["entries"])
    filled = sum(
        1 for c in (puzzle.clues or []) if isinstance(c, dict) and (c.get("clue") or "").strip()
    )
    return f"{filled} / {total}"


def _parse_bool_flag(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    s = str(raw or "").strip().lower()
    return s in ("true", "1", "yes", "y")


def _serialize_admin_mini(puzzle: MiniCrossword) -> dict:
    today = utc_today()
    attempts_count = puzzle.attempts.count()
    val = validate_solution(
        puzzle.pattern, puzzle.solution, puzzle.clues or [], exclude_puzzle_id=puzzle.id
    )
    # Mark which (answer, clue) pairs already exist in ClueBank
    clues_list = puzzle.clues or []
    answers = {
        (c.get("answer") or "").strip().upper()
        for c in clues_list
        if isinstance(c, dict) and c.get("answer")
    }
    bank_pairs = set(
        ClueBank.objects.filter(answer__in=answers).values_list("answer", "clue")
    )
    enriched_clues = []
    for c in clues_list:
        if not isinstance(c, dict):
            continue
        ans = (c.get("answer") or "").strip().upper()
        ct = (c.get("clue") or "").strip()
        item = dict(c)
        item["in_bank"] = bool(ans and ct and (ans, ct) in bank_pairs)
        if "save_to_bank" not in item:
            item["save_to_bank"] = True
        enriched_clues.append(item)

    return {
        "id": puzzle.id,
        "number": puzzle.number,
        "date": puzzle.date.isoformat(),
        "title": puzzle.title,
        "pattern": puzzle.pattern,
        "solution": puzzle.solution,
        "clues": enriched_clues,
        "difficulty": puzzle.difficulty or "",
        "difficulty_score": puzzle.difficulty_score,
        "difficulty_override": puzzle.difficulty_override,
        "difficulty_breakdown": val.get("difficulty"),
        "approval_status": puzzle.status,
        "status": _admin_mini_status(puzzle, puzzle.date, today),
        "auto_generated": puzzle.auto_generated,
        "attempts_count": attempts_count,
        "locked_for_edit": attempts_count > 0,
        "validation_errors": val["errors"],
        "validation_warnings": val["warnings"],
        "created_at": puzzle.created_at.isoformat(),
        "updated_at": puzzle.updated_at.isoformat(),
    }


class AdminMiniListCreateAPIView(APIView):
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

        puzzles = (
            MiniCrossword.objects.filter(date__gte=from_date, date__lte=to_date)
            .annotate(
                players_count=Count("attempts"),
                solved_count=Count(
                    "attempts", filter=Q(attempts__status=MiniAttempt.STATUS_SOLVED)
                ),
            )
        )
        by_date = {p.date: p for p in puzzles}

        rows = []
        cursor = from_date
        while cursor <= to_date:
            p = by_date.get(cursor)
            row_status = _admin_mini_status(p, cursor, today)
            if p is not None:
                players = p.players_count
                solve_rate = round((p.solved_count / players) * 100) if players else None
                item = {
                    "id": p.id,
                    "date": cursor.isoformat(),
                    "number": p.number,
                    "title": p.title,
                    "difficulty": p.difficulty or "",
                    "difficulty_score": p.difficulty_score,
                    "difficulty_override": p.difficulty_override,
                    "filled_clues": _filled_clues_label(p),
                    "status": row_status,
                    "approval_status": p.status,
                    "auto_generated": p.auto_generated,
                    "players_count": players,
                    "solve_rate": solve_rate,
                }
            else:
                item = {
                    "id": None,
                    "date": cursor.isoformat(),
                    "number": None,
                    "title": "",
                    "difficulty": "",
                    "difficulty_score": None,
                    "difficulty_override": False,
                    "filled_clues": "0 / 0",
                    "status": "empty",
                    "approval_status": None,
                    "auto_generated": False,
                    "players_count": 0,
                    "solve_rate": None,
                }

            if not status_filter or item["status"] == status_filter:
                rows.append(item)
            cursor += timedelta(days=1)

        return Response({"from": from_date.isoformat(), "to": to_date.isoformat(), "results": rows})

    def post(self, request):
        parsed_date = _parse_iso_date(request.data.get("date"))
        if parsed_date is None:
            return Response({"detail": "Valid ISO date is required."}, status=status.HTTP_400_BAD_REQUEST)

        pattern = request.data.get("pattern") or ["....#", ".....", ".....", ".....", "#...."]
        solution = request.data.get("solution") or list(pattern)
        clues = request.data.get("clues") or []
        title = (request.data.get("title") or "").strip()

        puzzle = MiniCrossword(
            date=parsed_date,
            title=title,
            pattern=pattern,
            solution=solution,
            clues=clues,
            status=MiniCrossword.STATUS_DRAFT,  # Never auto-approve
            auto_generated=False,
            created_by=request.user,
        )
        _apply_difficulty_params(puzzle, request.data)
        apply_difficulty_to_puzzle(puzzle)

        try:
            puzzle.save()
        except IntegrityError:
            return Response(
                {"detail": "A mini crossword already exists for this date."},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(_serialize_admin_mini(puzzle), status=status.HTTP_201_CREATED)


def _apply_difficulty_params(puzzle: MiniCrossword, data: dict) -> None:
    valid_labels = {
        MiniCrossword.DIFFICULTY_EASY,
        MiniCrossword.DIFFICULTY_MEDIUM,
        MiniCrossword.DIFFICULTY_HARD,
    }
    if "difficulty" in data:
        raw = data.get("difficulty")
        norm = (raw or "").strip().lower() if isinstance(raw, str) else ""
        if norm in valid_labels:
            puzzle.difficulty = norm
            puzzle.difficulty_override = True
        else:
            # null, "", or "auto" resets to computed difficulty
            puzzle.difficulty_override = False
    elif "difficulty_override" in data:
        puzzle.difficulty_override = _parse_bool_flag(data.get("difficulty_override"))


class AdminMiniDetailAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request, pk: int):
        puzzle = MiniCrossword.objects.filter(pk=pk).first()
        if puzzle is None:
            return Response({"detail": "Mini crossword not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(_serialize_admin_mini(puzzle))

    def patch(self, request, pk: int):
        puzzle = MiniCrossword.objects.filter(pk=pk).first()
        if puzzle is None:
            return Response({"detail": "Mini crossword not found."}, status=status.HTTP_404_NOT_FOUND)

        has_attempts = puzzle.attempts.exists()
        content_fields = {"pattern", "solution", "clues"}
        if has_attempts and any(f in request.data for f in content_fields):
            return Response(
                {"detail": "Cannot modify pattern, solution, or clues of a puzzle with attempts."},
                status=status.HTTP_409_CONFLICT,
            )

        if "date" in request.data:
            new_date = _parse_iso_date(request.data.get("date"))
            if new_date is None:
                return Response({"detail": "Invalid date."}, status=status.HTTP_400_BAD_REQUEST)
            puzzle.date = new_date
        if "title" in request.data:
            puzzle.title = (request.data.get("title") or "").strip()
        if "pattern" in request.data:
            puzzle.pattern = request.data["pattern"]
        if "solution" in request.data:
            puzzle.solution = request.data["solution"]
        if "clues" in request.data:
            puzzle.clues = request.data["clues"]

        _apply_difficulty_params(puzzle, request.data)
        if any(f in request.data for f in ("pattern", "solution", "difficulty", "difficulty_override")):
            apply_difficulty_to_puzzle(puzzle)

        if any(f in request.data for f in content_fields):
            puzzle.status = MiniCrossword.STATUS_DRAFT
            puzzle.approved_by = None
            puzzle.approved_at = None
            puzzle.auto_generated = False

        try:
            puzzle.save()
        except IntegrityError:
            return Response(
                {"detail": "A mini crossword already exists for this date."},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(_serialize_admin_mini(puzzle))

    def delete(self, request, pk: int):
        puzzle = MiniCrossword.objects.filter(pk=pk).first()
        if puzzle is None:
            return Response({"detail": "Mini crossword not found."}, status=status.HTTP_404_NOT_FOUND)
        if puzzle.date <= utc_today() and puzzle.attempts.exists():
            return Response(
                {"detail": "Cannot delete a live or past mini crossword that has attempts."},
                status=status.HTTP_409_CONFLICT,
            )
        puzzle.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminMiniApproveAPIView(APIView):
    permission_classes = [IsSuperUser]

    def post(self, request, pk: int):
        puzzle = MiniCrossword.objects.filter(pk=pk).first()
        if puzzle is None:
            return Response({"detail": "Mini crossword not found."}, status=status.HTTP_404_NOT_FOUND)

        val = validate_solution(
            puzzle.pattern, puzzle.solution, puzzle.clues or [], exclude_puzzle_id=puzzle.id
        )
        if val["errors"]:
            return Response(
                {
                    "detail": "Validation failed.",
                    "errors": val["errors"],
                    "warnings": val["warnings"],
                    "difficulty": val.get("difficulty"),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            was_draft = puzzle.status == MiniCrossword.STATUS_DRAFT
            apply_difficulty_to_puzzle(puzzle)
            puzzle.status = MiniCrossword.STATUS_APPROVED
            puzzle.approved_by = request.user
            puzzle.approved_at = timezone.now()
            puzzle.auto_generated = False
            puzzle.save(
                update_fields=[
                    "status",
                    "approved_by",
                    "approved_at",
                    "auto_generated",
                    "difficulty",
                    "difficulty_score",
                    "updated_at",
                ]
            )
            if was_draft:
                for c in puzzle.clues or []:
                    if not isinstance(c, dict):
                        continue
                    if c.get("save_to_bank") is False:
                        continue
                    ans = (c.get("answer") or "").strip().upper()
                    ct = (c.get("clue") or "").strip()
                    if not ans or not ct:
                        continue
                    sid = c.get("story_id")
                    story = Story.objects.filter(pk=sid).first() if sid else None
                    obj, created = ClueBank.objects.get_or_create(
                        answer=ans,
                        clue=ct,
                        defaults={"story": story, "times_used": 1},
                    )
                    if not created:
                        ClueBank.objects.filter(pk=obj.pk).update(
                            times_used=F("times_used") + 1
                        )

        return Response(_serialize_admin_mini(puzzle))


class AdminMiniUnapproveAPIView(APIView):
    permission_classes = [IsSuperUser]

    def post(self, request, pk: int):
        puzzle = MiniCrossword.objects.filter(pk=pk).first()
        if puzzle is None:
            return Response({"detail": "Mini crossword not found."}, status=status.HTTP_404_NOT_FOUND)
        if puzzle.attempts.exists():
            return Response(
                {"detail": "Cannot unapprove a puzzle that already has attempts."},
                status=status.HTTP_409_CONFLICT,
            )
        puzzle.status = MiniCrossword.STATUS_DRAFT
        puzzle.approved_by = None
        puzzle.approved_at = None
        puzzle.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
        return Response(_serialize_admin_mini(puzzle))


class AdminMiniValidateAPIView(APIView):
    permission_classes = [IsSuperUser]

    def post(self, request):
        pattern = request.data.get("pattern") or []
        solution = request.data.get("solution") or []
        clues = request.data.get("clues") or []
        exclude_id = request.data.get("exclude_puzzle_id") or None
        res = validate_solution(pattern, solution, clues, exclude_puzzle_id=exclude_id)
        return Response(res)


class AdminMiniGenerateFillAPIView(APIView):
    permission_classes = [IsSuperUser]

    def post(self, request):
        pattern = request.data.get("pattern") or []
        theme_words = request.data.get("theme_words") or []
        seed = request.data.get("seed")
        solutions = generate_fill(pattern, seed=seed, theme_words=theme_words, limit=3)
        return Response({"solutions": solutions})


class AdminMiniDraftCluesAPIView(APIView):
    permission_classes = [IsSuperUser]

    def post(self, request):
        pattern = request.data.get("pattern") or []
        solution = request.data.get("solution") or []
        clues = draft_clues(solution, pattern)
        return Response({"clues": clues})


class AdminMiniClueSuggestionsAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        answer = (request.query_params.get("answer") or "").strip().upper()
        if not answer:
            return Response({"suggestions": []})
        rows = (
            ClueBank.objects.select_related("story")
            .filter(answer=answer)
            .order_by("times_used", "id")[:20]
        )
        return Response(
            {
                "suggestions": [
                    {
                        "id": r.id,
                        "answer": r.answer,
                        "clue": r.clue,
                        "story_id": r.story_id,
                        "story_title": r.story.title if r.story_id else None,
                        "is_name": r.is_name,
                        "times_used": r.times_used,
                    }
                    for r in rows
                ]
            }
        )


class AdminMiniAutogenerateAPIView(APIView):
    permission_classes = [IsSuperUser]

    def post(self, request):
        try:
            days = int(request.data.get("days", 14))
        except (TypeError, ValueError):
            days = 14
        raw_count = request.data.get("count")
        count = None
        if raw_count is not None and str(raw_count).strip() != "":
            try:
                count = int(raw_count)
            except (TypeError, ValueError):
                count = None
        seed = request.data.get("seed")
        return Response(auto_generate(utc_today(), days=days, count=count, seed=seed))


class AdminMiniSummaryAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        today = utc_today()
        horizon = today + timedelta(days=13)

        upcoming = {
            p.date: p
            for p in MiniCrossword.objects.filter(date__gte=today, date__lte=horizon)
        }

        scheduled_ahead_days = 0
        for offset in range(90):
            d = today + timedelta(days=offset)
            if MiniCrossword.objects.filter(date=d, status=MiniCrossword.STATUS_APPROVED).exists():
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
                        "reason": "empty",
                        "label": "No grid scheduled",
                    }
                )
            elif p.status == MiniCrossword.STATUS_DRAFT:
                needs_attention.append(
                    {
                        "date": d.isoformat(),
                        "puzzle_id": p.id,
                        "reason": "auto" if p.auto_generated else "draft",
                        "label": f"{'Auto draft' if p.auto_generated else 'Draft'}: {p.title or _filled_clues_label(p)}",
                    }
                )

        players_today = MiniAttempt.objects.filter(puzzle__date=today).count()
        total_attempts = MiniAttempt.objects.count()
        total_solved = MiniAttempt.objects.filter(status=MiniAttempt.STATUS_SOLVED).count()
        avg_solve_rate = round((total_solved / total_attempts) * 100) if total_attempts else 0

        return Response(
            {
                "scheduled_ahead_days": scheduled_ahead_days,
                "empty_in_next_14": empty_in_next_14,
                "average_solve_rate": avg_solve_rate,
                "players_today": players_today,
                "needs_attention": needs_attention,
            }
        )


CLUE_BANK_DEFAULT_PAGE_SIZE = 50
CLUE_BANK_MAX_PAGE_SIZE = 100


def _encode_clue_bank_cursor(row: ClueBank) -> str:
    payload = json.dumps(
        {"a": row.answer, "c": row.clue, "i": row.id},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_clue_bank_cursor(token: str) -> tuple[str, str, int]:
    if not token or not isinstance(token, str):
        raise ValueError("Invalid cursor.")
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Invalid cursor.") from exc
    if not isinstance(data, dict):
        raise ValueError("Invalid cursor.")
    ans = data.get("a")
    clue = data.get("c")
    row_id = data.get("i")
    if (
        not isinstance(ans, str)
        or not isinstance(clue, str)
        or not isinstance(row_id, int)
        or isinstance(row_id, bool)
    ):
        raise ValueError("Invalid cursor.")
    return ans, clue, row_id


def _filter_clue_bank_queryset(params):
    qs = ClueBank.objects.select_related("story").all()

    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(answer__icontains=q) | Q(clue__icontains=q))

    answer_exact = (params.get("answer") or "").strip().upper()
    if answer_exact:
        qs = qs.filter(answer=answer_exact)

    length_raw = (params.get("length") or "").strip()
    if length_raw:
        try:
            length_val = int(length_raw)
        except ValueError as exc:
            raise ValueError("length must be 3, 4, or 5.") from exc
        if length_val not in (3, 4, 5):
            raise ValueError("length must be 3, 4, or 5.")
        qs = qs.annotate(_answer_len=Length("answer")).filter(_answer_len=length_val)

    story_id_raw = (params.get("story_id") or "").strip()
    if story_id_raw:
        try:
            story_id_val = int(story_id_raw)
        except ValueError as exc:
            raise ValueError("Invalid story_id.") from exc
        qs = qs.filter(story_id=story_id_val)

    is_name_raw = (params.get("is_name") or "").strip().lower()
    if is_name_raw:
        if is_name_raw in ("true", "1", "yes"):
            qs = qs.filter(is_name=True)
        elif is_name_raw in ("false", "0", "no"):
            qs = qs.filter(is_name=False)
        else:
            raise ValueError("Invalid is_name filter.")

    return qs.order_by("answer", "clue", "id")


class _CsvEcho:
    def write(self, value: str) -> str:
        return value


def iter_clue_bank_csv_rows(qs, include_usage: bool = True):
    writer = csv.writer(_CsvEcho(), lineterminator="\n")
    header = ["answer", "clue", "story_slug", "is_name"]
    if include_usage:
        header.append("times_used")
    yield writer.writerow(header)

    for row in qs.iterator(chunk_size=500):
        fields = [
            row.answer,
            row.clue,
            row.story.slug if row.story_id else "",
            "true" if row.is_name else "false",
        ]
        if include_usage:
            fields.append(str(row.times_used))
        yield writer.writerow(fields)


class AdminClueBankListCreateAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        try:
            base_qs = _filter_clue_bank_queryset(request.query_params)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        page_size_raw = (request.query_params.get("page_size") or "").strip()
        if page_size_raw:
            try:
                page_size = int(page_size_raw)
            except ValueError:
                return Response(
                    {"detail": "page_size must be an integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            page_size = max(1, min(page_size, CLUE_BANK_MAX_PAGE_SIZE))
        else:
            page_size = CLUE_BANK_DEFAULT_PAGE_SIZE

        cursor_raw = (request.query_params.get("cursor") or "").strip()
        qs = base_qs
        if cursor_raw:
            try:
                cur_answer, cur_clue, cur_id = _decode_clue_bank_cursor(cursor_raw)
            except ValueError:
                return Response(
                    {"detail": "Invalid cursor."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            qs = qs.filter(
                Q(answer__gt=cur_answer)
                | Q(answer=cur_answer, clue__gt=cur_clue)
                | Q(answer=cur_answer, clue=cur_clue, id__gt=cur_id)
            )

        total = base_qs.count()
        fetched = list(qs[: page_size + 1])
        has_more = len(fetched) > page_size
        rows = fetched[:page_size]
        next_cursor = _encode_clue_bank_cursor(rows[-1]) if has_more and rows else None

        return Response(
            {
                "results": [
                    {
                        "id": r.id,
                        "answer": r.answer,
                        "clue": r.clue,
                        "story_id": r.story_id,
                        "story_slug": r.story.slug if r.story_id else None,
                        "story_title": r.story.title if r.story_id else None,
                        "is_name": r.is_name,
                        "times_used": r.times_used,
                    }
                    for r in rows
                ],
                "next_cursor": next_cursor,
                "total": total,
            }
        )

    def post(self, request):
        answer = (request.data.get("answer") or "").strip().upper()
        clue = (request.data.get("clue") or "").strip()
        story_slug = (request.data.get("story_slug") or "").strip()
        story_id = request.data.get("story_id") or None
        is_name_flag = _parse_bool_flag(request.data.get("is_name", False))

        if not answer or not clue:
            return Response(
                {"detail": "Both answer and clue are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        story = None
        if story_id:
            story = Story.objects.filter(pk=story_id).first()
        elif story_slug:
            story = Story.objects.filter(slug=story_slug).first()

        obj, created = ClueBank.objects.update_or_create(
            answer=answer,
            clue=clue,
            defaults={"story": story, "is_name": is_name_flag},
        )
        return Response(
            {
                "id": obj.id,
                "answer": obj.answer,
                "clue": obj.clue,
                "story_id": obj.story_id,
                "story_title": obj.story.title if obj.story_id else None,
                "is_name": obj.is_name,
                "times_used": obj.times_used,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class AdminClueBankDetailAPIView(APIView):
    permission_classes = [IsSuperUser]

    def patch(self, request, pk: int):
        obj = ClueBank.objects.select_related("story").filter(pk=pk).first()
        if obj is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if "is_name" in request.data:
            obj.is_name = _parse_bool_flag(request.data.get("is_name"))
            obj.save(update_fields=["is_name"])
        return Response(
            {
                "id": obj.id,
                "answer": obj.answer,
                "clue": obj.clue,
                "story_id": obj.story_id,
                "story_title": obj.story.title if obj.story_id else None,
                "is_name": obj.is_name,
                "times_used": obj.times_used,
            }
        )

    def delete(self, request, pk: int):
        obj = ClueBank.objects.filter(pk=pk).first()
        if obj is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def import_clue_bank_csv_text(csv_text: str) -> dict:
    cleaned = (csv_text or "").lstrip("\ufeff").strip()
    reader = csv.DictReader(io.StringIO(cleaned))
    imported = 0
    updated = 0
    skipped = 0

    for row in reader:
        norm = {
            k.strip().lower(): (v or "").strip()
            for k, v in row.items()
            if k is not None
        }
        # times_used column is accepted and intentionally ignored
        ans = norm.get("answer", "").upper()
        clue = norm.get("clue", "")
        slug = norm.get("story_slug", "")
        is_name_flag = _parse_bool_flag(norm.get("is_name", ""))

        if not ans or not clue or not (3 <= len(ans) <= 5) or not ans.isalpha():
            skipped += 1
            continue

        story = Story.objects.filter(slug=slug).first() if slug else None
        _, created = ClueBank.objects.update_or_create(
            answer=ans,
            clue=clue,
            defaults={"story": story, "is_name": is_name_flag},
        )
        if created:
            imported += 1
        else:
            updated += 1

    return {"imported": imported, "updated": updated, "skipped": skipped}


class AdminClueBankImportAPIView(APIView):
    permission_classes = [IsSuperUser]

    def post(self, request):
        csv_text = request.data.get("csv") or ""
        if not csv_text.strip():
            return Response({"detail": "csv text is required."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(import_clue_bank_csv_text(csv_text))


class AdminClueBankExportAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        try:
            qs = _filter_clue_bank_queryset(request.query_params)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        filename = f"clue-bank-{utc_today().isoformat()}.csv"
        response = StreamingHttpResponse(
            iter_clue_bank_csv_rows(qs, include_usage=True),
            content_type="text/csv; charset=utf-8",
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class AdminClueBankHealthAPIView(APIView):
    permission_classes = [IsSuperUser]

    def get(self, request):
        return Response(compute_bank_health())
