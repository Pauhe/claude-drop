# claude-drop

Get images from a phone or desktop clipboard into a Claude Code session that is
running over SSH on another machine.

## The problem

When you drive Claude Code over SSH — from a phone, or from a desktop into a
remote VM — there is no way to paste an image into the prompt. The clipboard
stays on the local device and the terminal carries only text. What does work is
a file path in the prompt: Claude Code reads the image from disk.

## The shape of the fix

A small upload service on one host in a private network (built for a Tailscale
tailnet), plus a `drop` CLI on each VM that pulls uploaded files to a
predictable path.

```
phone / desktop browser  ──upload──>  drop service
                                            │
                       Claude runs `drop pull` on the VM
                                            ↓
                            ~/drop/latest.jpg  +  ~/drop/latest/
```

You upload from the browser, then tell Claude "analyse this, I dropped images
for you". A Claude Code skill on the VM makes Claude run `drop pull` itself and
read the paths it prints — no path typing, no scp.

What it handles so you do not have to:

- HEIC, rotation stored as EXIF, oversized photos: images are detected by their
  bytes, rotated, stripped of metadata and kept within Claude's image limits.
  The original stays untouched on the server.
- An empty pull is reported as empty. The skill never re-reads an older
  `latest.jpg` and presents it as new.
- Several sessions on one machine: each upload gets a short batch id, so a
  session can fetch exactly that batch.
- Uploaded content is data, not instruction.

## Security model — read this first

**There is no application login. The private network is the boundary.** Anyone
who can reach the service can upload, list, download and delete drops. Only
deploy it where every device that can reach it is yours (e.g. a personal
tailnet), never on the public internet. Stored originals keep their EXIF,
including GPS; only the normalised variants are stripped. See
[`docs/design.md`](docs/design.md) for why the earlier token-based draft was
dropped.

## Run the service

The image is built by CI (`.github/workflows/build.yml`). To run your own:

```bash
docker build -t claude-drop .
docker network create drop-proxy   # or reuse your proxy's network
docker run -d --name claude-drop --network drop-proxy \
  --memory 2g --cpus 1.5 \
  --cap-drop ALL --security-opt no-new-privileges \
  -v claude-drop-data:/data claude-drop
```

No port is published: put it behind a reverse proxy on the same Docker network
(upstream `claude-drop:8000`) that is reachable only from your private network,
and raise the proxy's request size limit (e.g. `client_max_body_size 550m` in
Nginx); at the common 1 MB default every phone photo fails with 413. Limits,
quota and retention are configurable via `DROP_*` environment variables, see
[`server/app/config.py`](server/app/config.py). Defaults: 25 MB per file, 20
files per upload, 8 GB quota, 30 days or 500 drops.

## Install the CLI and skill on a VM

```bash
curl -fsSL https://drop.example.ts.net/install.sh | DROP_URL=https://drop.example.ts.net sh
```

This installs `~/.local/bin/drop` (Python 3.11+, standard library only), the
skill under `~/.claude/skills/claude-drop/`, and records the URL in
`~/.config/claude-drop/config`. Start a new Claude Code session afterwards.

```
drop pull                  # everything new since the last pull
drop pull --batch k7f3a2   # exactly that upload
drop pull --since 2h       # also --today, --last 5
drop pull --full           # full resolution, within API limits
drop list --since 1d       # metadata only
```

## Docs

- [`docs/design.md`](docs/design.md) — the design, its contracts and the
  verification results.
- [`docs/reviews/`](docs/reviews/) — the adversarial reviews (by GPT-6 Astra)
  that shaped it. Host and network names in the docs are generalised.

## Tests

```bash
(cd server && pip install -r requirements.txt && pytest)
(cd cli && pytest)
sh tests/test_install.sh
```

## License

MIT
