# Telegram Restricted Content Migrator

A small, queue-based tool for moving Telegram messages and media that you are allowed to access. It scans a chosen message range, remembers work in SQLite, and processes the queue in short, resumable runs instead of one fragile all-day loop.

It is deliberately a Telegram migration tool, not a general-purpose cloud sync service.

## What it handles

- Text-only messages, photos, videos, documents, and media albums.
- A media message keeps its caption by default. Set `transfer.drop_caption: true` to remove it.
- `transfer.include.text: false` skips text-only messages but does not remove captions from selected media.
- Native Telegram copy/forward is tried first whenever the same user account reads and writes. This avoids downloading and does not use local disk space.
- Optional keyword and regular-expression filters inspect text bodies and media captions. A matching message, or any matching item in an album, skips the whole item.
- Sources and destinations can point to forum topics.

Use it only for content that you have permission to access and migrate.

## Install

Python 3.13 has been checked with the pinned `Pyrogram 2.0.106` dependency: install, import, and `Client` construction work. Use a virtual environment on a server:

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Keep secrets outside Git, for example in `.env`:

```env
API_ID=123456
API_HASH=your_api_hash
BOT_TOKEN=123456:your_bot_token
WEBDAV_USERNAME=archive-user
WEBDAV_PASSWORD=archive-password
```

The app expands `${NAME}` values in `config.yaml`. Session files, `.env`, the queue database, and downloads are ignored by Git.

## Login, including two-factor authentication

Create the user session interactively before scheduling anything:

```bash
python main.py login --session my_user
```

If Telegram requests two-factor authentication, the password prompt is hidden and the password is never written to logs. Login must remain interactive; do not put the password into YAML, `.env`, or a service file.

## Configure sources, targets, and message types

Edit `config.yaml` and make the IDs/ranges real:

```yaml
migration:
  sources:
    - chat: "@source_channel_or_-100_id"
      message_range:
        start: 1
        end: 2000
  destinations:
    - chat: "@destination_channel_or_-100_id"

transfer:
  include:
    videos: true
    photos: true
    text: true
    documents: false
  hide_sender: true
  drop_caption: false
```

When a bot uploads, make it an administrator of the destination. The user session is still used to read the source because bots often cannot read old history.

### Telegram file-size limits

Local download-and-upload uses the account that actually sends the file:

- Bot: 2,000 MiB (2 GiB)
- Ordinary user: 2,000 MiB (2 GiB)
- Premium user: 4,000 MiB (4 GiB)

`transfer.max_upload_bytes: 0` uses that automatic choice. Set a positive value only when you intentionally need a stricter or test-only limit. Files above the limit are never downloaded merely to discover that Telegram cannot accept them. If the size is unavailable, the default is also safe: the job is recorded as `unknown_size` and no local download starts.

```yaml
transfer:
  max_upload_bytes: 0
  allow_download_unknown_size: false
```

An existing server-side Telegram copy can still succeed for a large file, so the queue keeps known oversized candidates pending long enough to try that zero-download route first.

### Optional content filters

Filters are off by default. They are case-insensitive unless changed, inspect only text and captions (not file names), and never write matching source text to the log.

```yaml
filters:
  enabled: true
  case_sensitive: false
  keywords:
    - "sponsored"
    - "contact @"
  regex:
    - "https?://\\S+"
```

The filter runs both while scanning and immediately before delivery. That means a new rule also applies to jobs that were already waiting in SQLite.

## Disk safety on a small server

The default keeps 5 GiB free and does not retain failed media:

```yaml
downloads:
  keep_failed: false
  keep_completed: false
  min_free_bytes: 5368709120
  max_failed_bytes: 0
  max_job_bytes: 0
```

Before each local download, the program reserves the full known job size plus `min_free_bytes`. It checks again while data arrives. A low-space job is delayed without using up its normal retry count. If the filesystem reports `ENOSPC` or a quota failure, the current `active/job-*` directory is deleted instead of being moved into `failed/`.

If you explicitly want a small troubleshooting cache, enable both settings below. Only program-created `job-*` directories are pruned, oldest first.

```yaml
downloads:
  keep_failed: true
  max_failed_bytes: 2147483648
```

At the start of a transfer run, the log reports free space, the protected reserve, and failed-cache use. Leftover `active/job-*` directories from an interrupted run are cleaned before new transfer work begins.

## Oversized media: record only or archive remotely

The safe default is `record`: an oversized file stays in the SQLite queue as a clear record but does not land in `downloads/`.

```yaml
transfer:
  oversized:
    action: record
    remote:
      enabled: false
```

If you need to retain oversized source media, opt in to `remote`. The tool supports a preconfigured `rclone` remote or direct HTTPS WebDAV without adding a heavy cloud SDK.

```yaml
transfer:
  oversized:
    action: remote
    remote:
      enabled: true
      method: rclone
      dest: "archive:telegram/oversized"
      extra_args: []
      delete_local_after: true
```

Configure and test the `rclone` remote yourself before running this tool; it never opens an interactive remote-login prompt. For direct WebDAV, use an HTTPS destination and environment variables for credentials:

```yaml
transfer:
  oversized:
    action: remote
    remote:
      enabled: true
      method: webdav
      dest: "https://cloud.example/dav/telegram/oversized"
      username: "${WEBDAV_USERNAME}"
      password: "${WEBDAV_PASSWORD}"
```

WebDAV certificates are verified normally. When the server has enough safe free space, the file is downloaded to `active/`, uploaded remotely, then deleted by default. If the server cannot safely hold it, the tool streams from Telegram directly to the remote. A failed direct stream is retried from the beginning; it is not falsely presented as resumable. Failed remote attempts do not create an unlimited local failed-file cache. A successfully archived source is reused for additional configured Telegram destinations.

Process only known oversized candidates when desired:

```bash
python main.py process --oversized-only
```

Inspect what was found or export it for review:

```bash
python main.py report-oversized
python main.py report-oversized --csv oversized.csv
```

The CSV contains one row per media item, with a constructible source link where possible, its size/type, a short caption summary, target, active writer limit, queue state, reason, and remote location.

## Forum topics

Put the topic's root message ID in `topic_id`:

```yaml
migration:
  destinations:
    - chat: "-1001234567890"
      topic_id: 456
```

For a private forum group, open any message in the desired topic and look at a link shaped like:

```text
https://t.me/c/1234567890/456/789
```

Here `456` is the `topic_id`; it is the number before the final message ID. The tool uses Telegram's topic-aware forwarding field for forwards and checks the returned message before marking the job copied. If it cannot confirm the target topic, it records a topic error rather than silently accepting a post in General.

## Commands

```bash
# Scan the configured ranges into SQLite.
python main.py scan

# Process pending work in configured batches.
python main.py process

# Check destination messages for copied jobs.
python main.py verify

# Scan, then process, in one one-shot invocation.
python main.py run

# Show state counts, or recover rows left in a transfer phase after a crash.
python main.py stats
python main.py recover
```

Queue states are `pending`, `downloading`, `uploading`, `copied`, `failed`, and `skipped`. Extra reason codes distinguish cases such as `oversized`, `unknown_size`, `disk_low`, `disk_full`, `flood_wait`, `account_restricted`, `source_missing`, `permission_denied`, `topic_error`, and `ad_filtered`.

Short FloodWaits are waited out. Longer ones are scheduled for a later run. Account-risk restrictions pause the current run and defer the job; the tool never rotates to another account automatically.

## One-shot systemd scheduling

This program is a good fit for a one-shot service plus an optional timer, not a permanently restarting daemon. Editable examples are in [`deploy/telegram-save.service.example`](deploy/telegram-save.service.example) and [`deploy/telegram-save.timer.example`](deploy/telegram-save.timer.example).

Before enabling either example:

1. Install the project and virtual environment in a stable directory such as `/srv/telegram-save`.
2. Run the interactive login as the same service user.
3. Create `/etc/telegram-save.env` with API credentials and any optional WebDAV credentials; restrict it to that user.
4. Ensure that `sessions/`, `data/`, and `downloads/` are owned by the service user and are not shared with another running copy.
5. Adjust the paths, user, group, and command in the example files. The supplied command uses `flock` so a timer cannot open the same session/database concurrently.

The service intentionally has no `Restart=always`: each invocation exits after its available batch. Install the edited files, then reload and enable the timer:

```bash
sudo cp deploy/telegram-save.service.example /etc/systemd/system/telegram-save.service
sudo cp deploy/telegram-save.timer.example /etc/systemd/system/telegram-save.timer
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-save.timer
sudo systemctl start telegram-save.service
```

Use `journalctl -u telegram-save.service -n 100 --no-pager` to review a run.
