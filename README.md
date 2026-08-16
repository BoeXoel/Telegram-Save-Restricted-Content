# Telegram Restricted Content Migrator

A small, queue-based tool for moving Telegram messages and media that you are allowed to access. It scans a chosen message range, remembers work in SQLite, and processes the queue in short, resumable runs instead of one fragile all-day loop.

It is deliberately a Telegram migration tool, not a general-purpose cloud sync service.

## What it handles

- Text-only messages, photos, videos, documents, and media albums.
- A media message keeps its caption by default. Set `transfer.drop_caption: true` to remove it.
- `transfer.include.text: false` skips text-only messages but does not remove captions from selected media.
- Native Telegram copy/forward is tried first whenever the same user account reads and writes. This avoids downloading and does not use local disk space.
- Optional keyword and regular-expression filters inspect text bodies and media captions. A matching message, or any matching item in an album, skips the whole item.
- Destinations can point to forum topics. A source `topic_id` is retained as queue metadata, but scanning deliberately follows the configured message-ID range; it is not a source-topic filter.

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

## Quick start

1. Set `telegram.user_session` to a private name such as `my_user`, then replace the source, destination, and small test range in `config.yaml`.
2. If you want bot uploads, put `BOT_TOKEN` in `.env` and make the bot an administrator of the destination. For a private destination or a fresh Bot session, run `warmup-bot` once before scanning. Otherwise set `telegram.bot.enabled: false`.
3. Create the user session interactively. The `--session` value must match `telegram.user_session`.
4. Scan the small range, inspect the queue, then process it. Verify only after the destination result looks correct.

```bash
python main.py login --session my_user
python main.py warmup-bot  # required once for a Bot that has not met a private target
python main.py scan
python main.py stats
python main.py process
python main.py verify
```

Every command accepts `--config PATH` when the configuration is not the `config.yaml` in the current directory. Start with a small range and a non-critical destination before increasing the range.

## Login, including two-factor authentication

Create the user session interactively before scheduling anything:

```bash
python main.py login --config config.yaml --session my_user
```

If Telegram requests two-factor authentication, the password prompt is hidden and the password is never written to logs. Login must remain interactive; do not put the password into YAML, `.env`, or a service file.

## Optional Telegram proxy

When the server's direct route to Telegram is unstable, route all Telegram traffic through one trusted proxy. The setting applies uniformly to the Reader, Bot writer, interactive login, Bot peer warmup, and media transfers.

```yaml
telegram:
  proxy:
    enabled: true
    scheme: "socks5" # socks5 | http
    hostname: "${TELEGRAM_PROXY_HOST}"
    port: 1080
    username: "${TELEGRAM_PROXY_USERNAME:}"
    password: "${TELEGRAM_PROXY_PASSWORD:}"
```

Put an authenticated proxy's credentials in `.env`, for example:

```env
TELEGRAM_PROXY_HOST=proxy.example.net
TELEGRAM_PROXY_USERNAME=proxy-user
TELEGRAM_PROXY_PASSWORD=proxy-password
```

`http` means an HTTP proxy that supports the CONNECT method; it is not a general HTTP forwarding setting. SOCKS5 is usually the simpler option for a Telegram relay. The program logs only the enabled proxy's protocol, host, and port, never its credentials.

When a proxy is enabled, the tool does not silently retry through the server's direct network connection. Fix the proxy or disable `telegram.proxy.enabled` explicitly. This setting only controls Telegram traffic; it does not configure rclone or WebDAV remote-archive traffic. Do not use an untrusted public proxy with a Telegram account session.

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

### Bot destination warmup

Telegram keeps peer information separately for each session. A fresh Bot session may know that it is in a private supergroup but still lack the peer information required to address that group by its numeric ID. Before the first Bot upload to such a target, run:

```bash
python main.py warmup-bot
```

If a target is not ready, the command tells you to send a command such as `/warmup@your_bot` inside that target group, then waits up to 120 seconds. Use `--warmup-timeout SECONDS` to change that limit. A direct command mention is reliable when the Bot uses Telegram privacy mode; making the Bot an administrator also lets it receive ordinary group messages.

For a supergroup, users and Bots use the same `-100…` ID. Do not remove the `-100` prefix for a Bot. A Bot writer cannot use private `t.me/+…` or `joinchat/…` links as a destination because Telegram does not allow Bots to resolve them. Configure the `-100…` ID or a public `@username` instead. A user writer may still use an invite link where Telegram allows it.

`scan` verifies the account that will actually write before adding any rows, so an unresolved Bot target cannot silently create unusable jobs. Existing queue rows are preserved: if `process` receives `PEER_ID_INVALID`, it records `peer_unresolved`, leaves the job pending without consuming an attempt, stops that run, names the target in the log, and points to `warmup-bot`.

Set `telegram.load_dialogs_on_start: true` only when a user session needs an extra peer-cache warmup. It now consumes that user's dialogs once at startup; it is not a substitute for Bot warmup.

### Delivery choices

The `transfer` settings below decide whether Telegram can copy a message directly or the tool needs to download and upload it again:

```yaml
transfer:
  # A native copy removes the visible source attribution. Set false to forward
  # when Telegram permits it.
  hide_sender: true
  # Remove a media caption or text attached to a copied/forwarded media item.
  drop_caption: false
  native_copy:
    enabled: true
    # true means skip a job when Telegram cannot copy/forward it directly;
    # false permits the normal local download-and-upload fallback.
    only: false
  # Retain successful locally downloaded job directories in downloads/completed/.
  save_to_local: false
```

Native copy/forward is available only when the same user account reads and writes, and Telegram permits the operation. With `hide_sender: true`, that native path uses copy rather than forward. When a bot writes, or native copy is unavailable, selected media uses the local download-and-upload path unless `native_copy.only` is enabled. `save_to_local` has the same successful-download retention effect as `downloads.keep_completed`; it does not create files for native copies, and remote oversized files follow their separate `delete_local_after` setting.

For locally re-uploaded videos, the tool carries forward the source video's known width, height, and duration so Telegram clients retain the original preview shape. It deliberately does not invent missing values or run a video probe. Messages already sent with missing video metadata must be uploaded again to change their preview.

### Telegram file-size limits

Local download-and-upload uses the account that actually sends the file:

- Bot: 2,000 MiB (2 GiB)
- Ordinary user: 2,000 MiB (2 GiB)
- Premium user: 4,000 MiB (4 GiB)

`transfer.max_upload_bytes: 0` uses that automatic choice. Set a positive value only when you intentionally need a stricter or test-only limit. Files above the limit are never downloaded merely to discover that Telegram cannot accept them. If the size is unavailable, the default is also safe: the job is recorded as `unknown_size` and no local download starts.

When a Bot is the normal sender but the reading account is Premium, you can explicitly allow that Premium account to send files between the Bot and Premium limits. This is off by default and is not an automatic account rotation: before any download, the tool checks that the Premium account can post to the chosen destination. If it cannot, the file remains an oversized record (or follows the separately configured remote archive path).

```yaml
transfer:
  max_upload_bytes: 0
  allow_download_unknown_size: false
  allow_premium_user_fallback: false
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

This remote path is only for known oversized files. It is not a fallback for an unresolved Bot peer, missing permissions, Telegram risk controls, download timeouts, or ordinary Telegram upload failures.

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
# Show every command and option. Add --config /path/to/config.yaml to any command.
python main.py --help

# Create or refresh the interactive user session.
python main.py login --session my_user

# Make a Bot session learn private destination peers before its first upload.
python main.py warmup-bot

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

# Process only pending rows already marked as oversized, or inspect/export them.
python main.py process --oversized-only
python main.py report-oversized
python main.py report-oversized --csv oversized.csv
```

`python bot.py ...` remains a compatibility wrapper for `python main.py ...`; new scripts should use `main.py`.

Queue states are `pending`, `downloading`, `uploading`, `copied`, `failed`, and `skipped`. Extra reason codes distinguish cases such as `oversized`, `unknown_size`, `disk_low`, `disk_full`, `flood_wait`, `account_restricted`, `peer_unresolved`, `source_missing`, `permission_denied`, `telegram_bad_request`, `topic_error`, and `ad_filtered`.

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
