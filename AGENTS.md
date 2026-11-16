1. Talk in english. UI terms and db data: don't translate.
2. Review your code against SOLID, KISS, YAGNI and DRY principles.
3. The UI, the in-app docs, the changelog and the Telegram bot are in Russian. Every user-facing text is wrapped in gettext's `_()`, even though there's no translation yet. `README.md` is in English.
4. Database changes only through Alembic migrations (`src/kurai/migrations`); they're applied automatically at start. Never change the schema by hand.
5. Every outgoing HTTP connection to the internet goes through the `outbound` module (the proxy gateway); no module creates its own HTTP client for the internet. Claude is called only through the official `anthropic` SDK, given the gateway's HTTP client.
6. No secrets, hostnames or other details of a particular server in the repo: they belong in `.env` (`.env.example` shows the names) or in the database settings.
7. The license is MIT; a new dependency must allow that: permissive (MIT, BSD, Apache) or weak copyleft used unchanged (LGPL, MPL). The audio analysis's Essentia is AGPL-3.0, so the `kurai-audio` image as a whole follows the AGPL; nothing else may bring a strong copyleft in.
8. The product name is written **Kurai**, with a capital K, everywhere people read it: the UI, page titles, the in-app docs and changelog, the Telegram bot, `README.md`. Lowercase `kurai` only in identifiers: the repo, package, image, file and variable names.
9. Optional parts live in plugins (`src/kurai/plugins.py`): the core never imports a plugin and knows nothing of what a plugin does; a new kind of extension is a new field of `Plugin`, used through the registry. A change to the plugin interface updates `docs/plugins.md` and `examples/kurai-incoming` (its tests run in CI) in the same commit, and the changelog says it.
