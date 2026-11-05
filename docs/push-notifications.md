# Push notifications to a phone, from a headless Linux box

Written for this project's failure alerting (P1/R-10), but the mechanism is
general — nothing here is specific to trading, and you can lift it into any
project that needs "tell me on my phone when this breaks."

---

## The problem

A background service on your own machine has no way to reach you. The options
people usually reach for each have a catch:

| Approach | Catch |
|---|---|
| Email (SMTP) | Needs an account, an app password, and a relay that will not mark you as spam. Gmail requires OAuth or an app password; deliverability is a real ongoing chore. |
| SMS (Twilio etc.) | Costs money per message, needs an account and a card, and A2P registration in the US is genuinely annoying now. |
| Slack / Discord webhook | Good if you already live there. Requires creating an app/webhook and keeping a secret URL. |
| Push service (ntfy, Pushover, Gotify) | Purpose-built for exactly this. ntfy needs no account at all. |
| Desktop notification | Only reaches you while sitting at the machine. |

For "a daemon on my workstation died at 3am", a **push service** is the least
friction: one HTTP POST, no account, no credentials to rotate, and a real
phone notification with sound.

---

## How ntfy works

[ntfy.sh](https://ntfy.sh) is an open-source pub/sub notification service.
The model is deliberately tiny:

```
              HTTP POST                        persistent connection
your script ────────────────► ntfy server ◄──────────────────── phone app
             "topic: my-alerts"              "subscribed to: my-alerts"
```

1. You pick a **topic** — just a string, e.g. `your-topic-a1b2c3`.
2. Your script does `POST https://ntfy.sh/<topic>` with the message as the body.
3. Your phone runs the ntfy app, subscribed to that topic, and shows the
   notification.

There is no registration step. A topic springs into existence the first time
someone posts to it.

### Sending is one HTTP call

Nothing exotic — this is the entire integration:

```bash
curl -d "fade-nearclose died at 03:14" ntfy.sh/your-topic-a1b2c3
```

```python
import requests
requests.post("https://ntfy.sh/your-topic-a1b2c3",
              data="fade-nearclose died at 03:14".encode("utf-8"),
              headers={"Title": "Arm failure", "Priority": "high", "Tags": "warning"},
              timeout=10)
```

Because it is plain HTTP with no auth handshake, it works from cron, from a
systemd `OnFailure=` unit, from a shell one-liner — anywhere you can make a
request.

### Useful headers

| Header | Effect |
|---|---|
| `Title` | Bold first line of the notification |
| `Priority` | `min`, `low`, `default`, `high`, `urgent`. `urgent` bypasses some do-not-disturb settings |
| `Tags` | Emoji/labels, e.g. `warning`, `skull`, `white_check_mark` |
| `Click` | URL opened when you tap the notification |
| `Markdown: yes` | Render the body as Markdown |

---

## The security model — read this part

**A public ntfy topic is readable and writable by anyone who knows the topic
name.** There is no access control on `ntfy.sh` by default. The protection is
purely that the topic name is unguessable.

Two consequences:

1. **Anyone who learns your topic can read every notification you send to it.**
   So: never put secrets, balances, position sizes, API keys or personal data
   in the message body. Send *"arm X failed, go look"*, not *"arm X failed,
   balance $1,234.56"*.
2. **Anyone who learns your topic can send you notifications.** Worst case is
   annoyance, but treat an unexpected message as untrusted input, not as a
   fact about your system.

Mitigations, in increasing order of effort:

- **Long random topic name.** The default here. Use ≥16 random hex characters,
  not `jeff-alerts`. Treat the topic name itself as a secret: it lives in
  `.env`, which is gitignored.
- **Reserve the topic with a free ntfy.sh account** so nobody else can claim it,
  and set it to private so only you can read/write.
- **Self-host.** ntfy is a single Go binary; run it behind your own TLS with
  auth enabled. Full control, no third party sees anything.
- **End-to-end encrypt the body yourself** before sending, if the content
  genuinely matters. At that point consider whether push is the right channel.

For failure alerting the content is deliberately boring — a service name and a
timestamp — so the public-topic tradeoff is acceptable. That is a judgement
about *this* payload, not a general endorsement.

---

## What this project does

`pm/core/notify.py` is the single place that sends anything. It fans out to three
channels, each independently optional, and **never raises** — an alerting path
that can crash the thing it is watching is worse than no alerting.

| Channel | Enabled by | Reaches you |
|---|---|---|
| Health log (JSONL) | always | via the control plane, and by `grep` |
| Desktop notification | a graphical session being active | at the machine |
| ntfy push | `NTFY_TOPIC` set in `.env` | on your phone, anywhere |

Configure it by adding to `.env`:

```bash
NTFY_TOPIC=your-topic-a1b2c33e5b2d4a86     # keep this unguessable
# NTFY_SERVER=https://ntfy.sh                    # override to self-host
# NTFY_PRIORITY=high
```

Then install the ntfy app (Android: Play Store or F-Droid; iOS: App Store),
tap **+**, and subscribe to that exact topic string.

Test it end to end:

```bash
python -m notify --test
```

### What actually triggers a push

1. **A systemd service failing.** Each unit gets
   `OnFailure=polymarket-alert@%n.service`, a template unit that calls
   `pm/core/notify.py` with the failed unit's name. This fires on a crash, a non-zero
   exit, or a start-limit hit — including the crash-loop case the ledger fix
   was written to prevent.
2. **The daily health digest.** A timer summarises arm liveness, last activity
   and anything that failed in the last 24h, and pushes only when something
   needs attention (a digest that always fires stops being read).

---

## Choosing for a different project

- **Just me, one machine, don't want an account** → ntfy public topic with a
  random name. This is the lowest-friction option that exists.
- **Content is sensitive** → self-hosted ntfy, or Pushover (paid one-time,
  per-device keys, no public topics), or a Slack webhook into a private channel.
- **Already in Slack/Discord all day** → use a webhook there; you will actually
  see it, which beats a technically-better channel you ignore.
- **Needs to page someone at 3am with escalation** → this whole category is the
  wrong tool. Use PagerDuty/Opsgenie; they exist for on-call rotation,
  acknowledgement and escalation, none of which ntfy does.

The general rule: **the best alerting channel is the one you will not learn to
ignore.** A channel that fires constantly gets muted within a week, and then
you have worse than nothing — you have the illusion of coverage. That is why
the digest here only fires when something is wrong.
