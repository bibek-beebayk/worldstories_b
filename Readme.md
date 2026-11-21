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

Send one test copy without creating an issue or delivery record:

```bash
python manage.py send_weekly_digest --to you@example.com
```

Preview the selection and plain-text rendering without sending:

```bash
python manage.py send_weekly_digest --dry-run
```

### Railway schedule

Create a separate Railway cron service using the same application image and environment variables. Its command is:

```bash
python manage.py send_weekly_digest
```

For Saturday at 09:00 UTC, use `0 9 * * 6`. Railway cron expressions run in UTC. Do not run the command from the web service startup path.

### Sending-domain DNS

Verify the sending domain in Resend and publish the exact records it supplies:

- SPF TXT record authorizing Resend for the envelope-sender domain.
- DKIM records supplied by Resend for message signing.
- DMARC TXT record at `_dmarc`, beginning with a monitoring policy such as `v=DMARC1; p=none;` and an aggregate-report address you monitor. Tighten the policy after SPF and DKIM alignment are confirmed.

DNS hostnames and verification values are account/domain-specific; copy them from the Resend domain-verification screen rather than using example values.
