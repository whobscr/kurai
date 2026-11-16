# Deploying Kurai with Ansible

A small playbook for one host that already has Docker with its compose plugin. It does what the main README's
«Quick start» and «Updating» do, from your machine: copies this repository's `docker-compose.yml` to the host, writes
the host's `.env` from `.env.example` with your values (secrets from Ansible Vault), pulls the images of the release
you set, starts them and waits until the web app answers.

It doesn't install Docker, set up your reverse proxy and TLS certificate, or back anything up: see the main README for
those («Reverse proxy», «Backups»).

## Setting up

Run these from this folder, on a checkout of the release you deploy (`git checkout v1.5.0`): the compose file and
`.env.example` are taken from it.

```sh
cp inventory.example.ini inventory.ini                        # your host
$EDITOR group_vars/kurai/vars.yml                             # version, address, folders, profiles
cp group_vars/kurai/vault.yml.example group_vars/kurai/vault.yml
$EDITOR group_vars/kurai/vault.yml                            # openssl rand -hex 32, twice
ansible-vault encrypt group_vars/kurai/vault.yml
ansible-playbook -i inventory.ini deploy.yml --ask-vault-pass
```

`inventory.ini` and `vault.yml` are in `.gitignore`. Keep the vault password and `vault_kurai_secret_key` safe: a
lost or changed secret key logs everyone out and makes the API keys and tokens Kurai stored unreadable.

| Variable | Where | What |
|---|---|---|
| `kurai_version` | `vars.yml` | The release to run: a tag of this repository without the «v». |
| `kurai_base_url` | `vars.yml` | The address people open, behind your reverse proxy. |
| `kurai_dir` | `vars.yml` | Where the music, Navidrome's config, the database and the models live on the host. |
| `kurai_profiles` | `vars.yml` | `db`, `cpu` or `gpu`, `audio` (main README, «Profiles»). |
| `kurai_env` | `vars.yml` | Any other variable of `.env.example` by its name, e.g. `TZ`, `KURAI_BIND`, `KURAI_OUTBOUND_PROXY`. |
| `vault_kurai_secret_key` | `vault.yml` | Signs sessions and encrypts stored secrets; set once, never change. |
| `vault_kurai_db_password` | `vault.yml` | PostgreSQL's password (with the `db` profile). |
| `kurai_compose_dir`, `kurai_puid`, `kurai_pgid`, `kurai_web_port` | `roles/kurai/defaults/main.yml` | The project's folder on the host (`/opt/kurai`), the account the containers write as, the web port. |

## Updating

Check out the new release, set `kurai_version` to it and run the playbook again. Only the changed containers are
recreated; the database's migrations run by themselves when Kurai starts. A variable that a new release adds to
`.env.example` comes along with its default.
