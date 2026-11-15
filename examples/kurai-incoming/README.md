# kurai-incoming

An example [Kurai](../../README.md) plugin, the one [docs/plugins.md](../../docs/plugins.md) walks through: albums
put into an «incoming» folder appear on a new system settings tab, «Входящие», and go into the library once an admin
approves them.

It uses most of the plugin hooks: settings, a recurring job, a manual job, an admin tab with its own validation and
buttons, a «Проверить» check, its own table and migrations, docs. Copy the folder, rename the package, and change it.

```sh
cd examples/kurai-incoming
uv sync                        # installs Kurai from ../.. in editable mode
KURAI_TEST_DATABASE_URL=postgresql+psycopg://kurai:kurai@127.0.0.1:55432/kurai uv run pytest
```
