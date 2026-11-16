# Kurai

A self-hosted music server for a household: it keeps your library, learns what each person likes, listens to the
music itself, and makes playlists for everyone every night.

You listen in any Subsonic app (Symfonium, Feishin, Substreamer, Amperfy…): Kurai's playlists simply appear there. Or
right in Kurai's own web app (installable as a PWA): its main page is one big button that plays «My wave», a stream
that changes as you listen, and the playlists. The web app and an optional Telegram bot also do what a player can't:
asking for a playlist in words, explanations, settings.

> The interface, its documentation and the Telegram bot are in **Russian** for now; English will follow.

## What it does

- **Learns tastes** from what each user plays, stars and rates in their player (Navidrome), and turns them into taste
  vectors.
- **«My wave»** per user: a playlist rebuilt from what they play and skip right now. Kurai's web player plays it as an
  endless stream and picks up each new version at once (a skip changes what comes next within seconds); other apps
  get the new version when they open the playlist again.
- **Nightly mixes** per user: a varied selection (maximal marginal relevance) ordered so neighbours sound alike,
  named by a language model. Private to their owner.
- **«Ask»**: a playlist from a request in words. The model only plans the query (mood, artists, length); retrieval over
  your library picks the tracks, so the result is always music you have.
- **Listens to the audio** (optional, the `audio` profile): Essentia's Discogs-EffNet gives each track a sound vector
  plus styles, moods, tempo and key; CLAP puts sound and words in one space. Mixes then follow how music sounds, not
  only its tags, and «Ask» finds tracks by a description of their sound. On the CPU, at night, about 4–5 s per track
  on two cores.
- **Any model**: OpenAI-compatible servers (llama.cpp, vLLM, OpenAI, OpenRouter, LM Studio), Ollama, or Claude.
  Embeddings are configured separately. A «think» switch per task. Works without a model too (no names, no «Ask»).
- **One outbound gateway**: one SOCKS5/HTTP proxy with a checkbox per subsystem and an optional DNS server, never a
  silent direct fallback.
- **Accounts** added by an admin only, password plus optional TOTP with recovery codes, admin and user roles.
- **Material 3** UI with a per-user accent colour and light/dark mode; Navidrome's admin page is embedded behind
  Kurai's login.
- **Plugins** can add the rest (pages, settings tabs, jobs, their own tables) without changing Kurai.

## Requirements

- Docker with Compose v2.24+, a Linux host.
- Your music on the host (`$KURAI_DIR/data/music`; Navidrome reads the tags).
- For local models: about 6 GB of RAM for an 8B model on the CPU (a request then takes about a minute), or an NVIDIA
  GPU. Or a cloud API, or no model at all.
- A reverse proxy with TLS in front (nginx, Caddy, Traefik).

## Quick start

```sh
git clone https://github.com/whobscr/kurai && cd kurai
cp .env.example .env
$EDITOR .env          # KURAI_SECRET_KEY, KURAI_DIR, KURAI_BASE_URL, POSTGRES_PASSWORD, PUID/PGID
mkdir -p "$(grep ^KURAI_DIR .env | cut -d= -f2)"/{data/music,config/navidrome}
docker compose up -d
```

Open the web app (port 8095 behind your proxy) and log in as **admin / admin**; you'll be asked for a new password at
once. Then follow «Справка → Для администраторов → Первый запуск»: check the models, add the other people, switch
on the audio analysis if you run it.

### Profiles

Set `COMPOSE_PROFILES` in `.env`:

| Profile | Adds |
|---|---|
| `db` | PostgreSQL 17 with pgvector (omit it and set `KURAI_DATABASE_URL` to use your own; it needs the `vector` extension) |
| `cpu` | llama.cpp on the CPU: Qwen3 8B for text, Qwen3-Embedding 0.6B for embeddings, downloaded on first start |
| `gpu` | the same on an NVIDIA GPU (needs the NVIDIA container toolkit) |
| `audio` | the audio analysis (`ghcr.io/whobscr/kurai-audio`, Linux x86_64): downloads ~25 MB of Essentia models and ~590 MB of CLAP on first use, uses ~1.5 GB of RAM; switch it on at «Система → Звук» |

The audio models have their own licenses: Essentia's models are CC BY-NC-SA 4.0 (non-commercial use only), LAION's
CLAP is Apache 2.0. They are downloaded at run time, not shipped in the image.


### Reverse proxy

Send everything to Kurai's web port. For music apps, `/rest/` can go straight to Navidrome
(`http://127.0.0.1:4533/admin/apps/navidrome/rest/`) so streams don't pass through Python; Kurai also forwards
`/rest/` itself for setups without that. **Clear the `Remote-User` header** on every request coming in from outside:
Navidrome trusts it from Kurai's web container (Kurai uses it to log its admin into Navidrome's own pages). That
container has a fixed address in Kurai's network (`KURAI_SUBNET`, `KURAI_WEB_IP`), and Navidrome trusts only it
(`ND_REVERSEPROXYWHITELIST`).

nginx example:

```nginx
server {
    listen 443 ssl;
    server_name music.example.org;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Remote-User "";
    location /rest/ { proxy_pass http://127.0.0.1:4533/admin/apps/navidrome/rest/; proxy_buffering off; }
    location /      { proxy_pass http://127.0.0.1:8095; proxy_buffering off; proxy_read_timeout 600s; }
}
```

### Proxies and networks

Kurai runs in Compose's network. A proxy you configure in Kurai's gateway must be reachable **from the containers**
(not a SOCKS5 that listens only on the host's 127.0.0.1). If yours does, run the services in the host's network with a
`docker-compose.override.yml` (`network_mode: host` and `ports: !reset []` per service, and `networks: !reset {}` for
`web`), point the `KURAI_*_URL` variables at `127.0.0.1` and set `ND_REVERSEPROXYWHITELIST=127.0.0.1/32` (every
process on the host is then trusted with Navidrome's login header: keep Navidrome's port closed to the network).

### Plugins

Optional parts can come as plugins: Python packages with a `kurai.plugins` entry point, installed into Kurai's image.
A plugin adds settings, jobs, steps of the night's work, pages, system settings tabs, checks, admin apps, docs,
Telegram handlers and its own tables. How to write one: [docs/plugins.md](docs/plugins.md), with a working example in
[examples/kurai-incoming](examples/kurai-incoming).

## Backups

Kurai doesn't make its own backups. Keep:

- the database: `docker compose exec postgres pg_dump -U kurai -Fc kurai > kurai-$(date +%F).dump`
- Navidrome's config: `tar -C "$KURAI_DIR" --exclude=cache -czf kurai-config.tar.gz config`
- your music, `$KURAI_DIR/data/music`, the way you back up any files.

Restore: `pg_restore -U kurai -d kurai --clean kurai-….dump` into an empty database, unpack the config, start.

## Updating

```sh
git pull && docker compose pull && docker compose up -d
```

Database migrations run by themselves when Kurai starts.

## Administration from the command line

```sh
docker compose exec web kurai reset-password <user>   # a temporary password, changed at the next login
docker compose exec web kurai reset-2fa <user>        # turn off someone's TOTP
```

## Development

```sh
uv sync                    # add --extra audio for the audio analysis (Essentia, PyTorch CPU; Linux x86_64)
docker run -d --name kurai-pg -e POSTGRES_USER=kurai -e POSTGRES_PASSWORD=kurai -e POSTGRES_DB=kurai \
  -p 127.0.0.1:55432:5432 pgvector/pgvector:pg17
KURAI_TEST_DATABASE_URL=postgresql+psycopg://kurai:kurai@127.0.0.1:55432/kurai uv run pytest
uv run ruff check src tests
# the real audio models on a generated song (a folder with them, as the audio container downloads them):
KURAI_AUDIO_MODELS=/path/to/models uv run pytest tests/test_audio.py -k real
```

Layout: `src/kurai/` — `web/` (FastAPI, Jinja2 templates, Beer CSS, HTMX), `jobs/` (the worker and its queue),
`recommend/` (library, taste, retrieval across the vector spaces, playlists), `audio/` (the analysis
container: models, the job, its small API), `integrations/` (Navidrome), `plugins.py` (the plugin registry), `llm/` (model providers), `outbound.py` (the gateway), `migrations/` (Alembic), `docs/ru/` (the in-app docs).
Contributor rules are in `AGENTS.md`.

Releases: a tag `vX.Y.Z` builds and publishes `ghcr.io/<owner>/kurai:X.Y.Z` and `kurai-audio:X.Y.Z`
(`.github/workflows/release.yml`).

## License

[GNU AGPL v3.0](LICENSE). Bundled front-end libraries keep their own licenses: Beer CSS and material-dynamic-colors
(MIT), htmx (0BSD), in `src/kurai/web/static/vendor/`.
