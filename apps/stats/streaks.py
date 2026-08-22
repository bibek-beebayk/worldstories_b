from datetime import date, timedelta


def compute_streak(activity_dates: set[date], today: date) -> tuple[int, int]:
    """Given the distinct calendar dates a user read/listened on, return
    (current_streak, longest_streak) as of `today`. current_streak counts
    consecutive days ending at the most recent activity date, but only
    stays "alive" if that date is today or yesterday — the day isn't over
    yet, so a streak shouldn't reset before the user has had a chance to
    keep it going today. It's broken once a full day has passed with zero
    activity (most recent activity 2+ days before today).
    """
    if not activity_dates:
        return 0, 0

    dates = sorted(activity_dates)

    longest = run = 1
    for previous, current in zip(dates, dates[1:]):
        if (current - previous).days == 1:
            run += 1
        else:
            longest = max(longest, run)
            run = 1
    longest = max(longest, run)

    most_recent = dates[-1]
    if (today - most_recent).days > 1:
        return 0, longest

    current_streak = 1
    cursor = most_recent
    while (cursor - timedelta(days=1)) in activity_dates:
        cursor -= timedelta(days=1)
        current_streak += 1

    return current_streak, longest
