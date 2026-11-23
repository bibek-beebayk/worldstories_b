# WorldStories backend

## Weekly newsletter

The weekly digest uses double opt-in for anonymous addresses. Existing users are not subscribed automatically.

### Environment variables

- `RESEND_API_KEY`: enables Anymail's Resend backend. When absent, the existing SMTP backend and its `EMAIL_HOST_*` settings remain active.
- `NEWSLETTER_FROM_EMAIL`: required for real sends, for example `WorldStories <digest@worldstories.net>`.
- `NEWSLETTER_REPLY_TO`: optional reply address. When set, it is also included as a `mailto:` List-Unsubscribe option.
- `NEWSLETTER_SITE_URL`: public frontend origin without a trailing slash, for example `https://worldstories.net`.
- `NEWSLETTER_API_URL`: public backend origin without a trailing slash, for example `https://api.worldstories.net`.
- `NEWSLETTER_POSTAL_ADDRESS`: required physical postal address shown in every digest footer.
- `NEWSLETTER_SEND_DELAY_SECONDS`: delay between recipients; defaults to `0.6`.
- `NEWSLETTER_DAILY_SEND_LIMIT`: maximum digest emails sent per day across all runs; defaults to `80`.
- `NEWSLETTER_SEND_TIMEZONE`: IANA timezone used for weekday cohort selection and daily caps; defaults to `UTC`.

Send one test copy without creating an issue or delivery record:

```bash
python manage.py send_weekly_digest --to you@example.com
```

Preview the selection and plain-text rendering without sending:

```bash
python manage.py send_weekly_digest --dry-run
```

## Story maintenance commands

Backfill `Story.cached_summary_reading_minutes` after deploy (safe to run more than once):

```bash
python manage.py backfill_summary_minutes
```

### Railway schedule

Create a separate Railway cron service using the same application image and environment variables. Its command is:

```bash
python manage.py send_weekly_digest
```

Run the cron **DAILY** (for example `0 9 * * *`, which runs at 09:00 UTC). Railway cron expressions run in UTC. Do not run the command from the web service startup path.

Each confirmed subscriber is assigned a weekday cohort (`0` = Monday through `6` = Sunday, balanced across the week) so every subscriber still receives one digest per week while sends are spread evenly across the week. Each daily run sends to overdue subscribers first, then today's weekday cohort, up to `NEWSLETTER_DAILY_SEND_LIMIT`. Anyone left over is picked up on the next day's run.

### Sending-domain DNS

Verify the sending domain in Resend and publish the exact records it supplies:

- SPF TXT record authorizing Resend for the envelope-sender domain.
- DKIM records supplied by Resend for message signing.
- DMARC TXT record at `_dmarc`, beginning with a monitoring policy such as `v=DMARC1; p=none;` and an aggregate-report address you monitor. Tighten the policy after SPF and DKIM alignment are confirmed.

DNS hostnames and verification values are account/domain-specific; copy them from the Resend domain-verification screen rather than using example values.
