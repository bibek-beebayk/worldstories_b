#!/bin/bash
# Daily jobs for the Railway cron service.
# Runs every job even if an earlier one fails, and exits non-zero if any failed,
# so the run shows as failed in Railway's Cron Runs tab.
# Set DRY_RUN=1 in the service variables to test without changing anything.

if [ "${DRY_RUN:-0}" = "1" ]; then
  BOTS_ARGS=""
  DIGEST_ARGS="--dry-run"
else
  BOTS_ARGS="--apply"
  DIGEST_ARGS=""
fi

status=0

echo "== flag_suspected_bots ${BOTS_ARGS} =="
python manage.py flag_suspected_bots ${BOTS_ARGS} || status=1

echo "== send_weekly_digest ${DIGEST_ARGS} =="
python manage.py send_weekly_digest ${DIGEST_ARGS} || status=1

exit $status