# Deployment

Libremapper runs as a sidecar. It does not replace, derive, or pin the image
used by the existing LibreNMS service. A native PHP client is installed into
that stock container through LibreNMS's supported Composer plugin command.

## Compatibility

Libremapper has been tested on Linux amd64 with these stock `librenms/librenms`
images:

| LibreNMS version | Tested image digest |
| --- | --- |
| 26.5.1 | `sha256:9e817ca2c2b5f99641fee15e71486ec09bb002cf04726c975fa95a0f50d25d21` |
| 26.10.2 | `sha256:c2208d11f4a9e240dcd763a70b5ba856ae475e26c53675f675e1e346514a6066` |

Other versions, image variants, and architectures are unverified. The
Libremapper image targets Linux amd64. Installation requires Docker Engine and
the `docker compose` plugin. Docker Engine 29.7.2, Docker CLI 28.1.1, and
Docker Compose 5.4.0 were used for compatibility testing; a minimum version or
supported range has not been established. The installer uses `docker compose`.

On 2026-11-13, `composer audit` reported security advisories in dependencies
already present in both tested images. Passing the compatibility tests does
not establish that an image is secure or that every advisory is exploitable in
a given installation. Check current LibreNMS security updates and audit the
image you plan to deploy; newer images may require separate compatibility
testing.

## Add the sidecar to an existing Compose stack

The provided overlay expects the main service to be named `librenms`, as in the
official LibreNMS Compose example. Copy [`compose.plugin.yaml`](../compose.plugin.yaml)
beside the existing `compose.yml` as `compose.libremapper.yaml`.

Add the database values already used by LibreNMS and a new API token to the
Compose `.env` file:

```dotenv
DB_HOST=db
DB_PORT=3306
DB_NAME=librenms
DB_USER=librenms
DB_PASSWORD=replace-with-the-existing-password
LIBREMAPPER_API_TOKEN=replace-with-openssl-rand-hex-32-output
PUID=1000
PGID=1000
```

Generate the token locally with `openssl rand -hex 32`. `PUID` and `PGID` must
match the account used inside the LibreNMS container. Do not add a host port for
the API: the sidecar uses `network_mode: service:librenms` and listens only on
that shared namespace's loopback interface.

The matching PHP package is bundled in the sidecar image.

Copy [`scripts/install-or-update`](../scripts/install-or-update) beside the
Compose files. If you do not have a repository checkout, both files can instead
be extracted from the image:

```bash
docker pull ghcr.io/echotechnic/libremapper:0.1.0
docker run --rm --entrypoint cat ghcr.io/echotechnic/libremapper:0.1.0 \
  /usr/local/share/libremapper/compose.libremapper.yaml > compose.libremapper.yaml
docker run --rm --entrypoint cat ghcr.io/echotechnic/libremapper:0.1.0 \
  /usr/local/share/libremapper/install-or-update > install-or-update
chmod +x install-or-update
```

Install the sidecar and native UI together:

```bash
chmod +x install-or-update
./install-or-update 0.1.0
```

Compose may recreate `librenms` once to add its environment and mounts. Its
declared image, database, main `/data` volume, ports, networks, and other
sidecars remain unchanged. The helper upgrades the sidecar first, verifies its
reported version, installs and enables the exact same plugin version from the
image, clears routes, restarts LibreNMS, and verifies the plugin routes. It
persists `LIBREMAPPER_VERSION` in `.env` so later Compose commands keep the
selected version.

If the Compose files have nonstandard names, pass every file in merge order:

```bash
./install-or-update 0.1.0 docker-compose.yml compose.libremapper.yaml
```

Open **Plugins → Libremapper → Settings** as a LibreNMS administrator. The UI
client is part of the container's writable layer, so run the helper again after
LibreNMS is recreated or upgraded. The database and shared Libremapper volumes
persist independently.

`/data/plugins` cannot supply this UI. LibreNMS local plugins are hook-only;
Libremapper uses Composer/Laravel routes and views.

## Persistent data and permissions

The overlay creates two dedicated volumes mounted read-write into both
containers:

- `libremapper-config:/config` contains the complete configuration tree.
- `libremapper-data:/data/libremapper` contains reviewed plans, SQLite
  ownership state, run history, locks, runtime candidates, and backups.

The sidecar initializes the volume roots, clears supplemental groups, and
drops from root to `PUID:PGID` before serving or running a CLI command. Files
copied into existing volumes must also be readable and writable by that ID.
At startup it writes the configured bearer token to
`/data/libremapper/api-token` with mode `0600`; this lets the native client
authenticate without weakening stock PHP-FPM's `clear_env` protection.

For direct host access, replace the config volume on both services with the
same bind mount and make it writable by the configured IDs.

### Oxidized Git neighbors

If the Gitea repository already contains
`topology/<hostname>--lldp-neighbors`, Libremapper needs only a read-only fetch
key. It never needs the Oxidized container's credentials. Generate a dedicated
key if one does not already exist:

```bash
ssh-keygen -t ed25519 -N '' -C libremapper-fetch -f libremapper-fetch-key
```

Add `libremapper-fetch-key.pub` to that Gitea repository as a read-only deploy
key. Open **Plugins → Libremapper → Settings → Advanced → Oxidized connection**,
choose **Remote Git
repository**, enter the SSH URL, paste `libremapper-fetch-key`, and select
**Save and test source**. This stores the private key with owner-only
permissions in the existing `libremapper-data` volume; the key is never written
to YAML or redisplayed. No Compose edit, container restart, or
`install-or-update` run is needed.

The first successful connection pins the Git server's host key. A later host
key change is refused. This automatic first-contact trust is the simple path;
verify the server fingerprint separately before pasting the key when your
environment requires protection against a first-connection interception.

Select the desired parsers separately for each map in **Links → Parsers → Selection**
and choose **Save parser selection**. Edit shared definitions in **Advanced → Parser catalog**. The NNI configuration parser can run alongside LLDP and
reads main Ciena backups without needing a separate command-output artifact.

#### If Gitea does not contain topology files yet

For the LLDP command-output parser, install the included [`saos10.rb`](../examples/oxidized/models/saos10.rb) model
override, then merge these settings into the existing Oxidized configuration:

```yaml
output:
  default: git
  git:
    single_repo: true
    type_as_directory: true
    repo: /home/oxidized/.config/oxidized/oxidized.git
```

If Gitea does not already receive that repository's commits, add Oxidized's
documented `githubrepo` hook using a separate write-enabled key:

```yaml
hooks:
  push_to_gitea:
    type: githubrepo
    events: [post_store]
    remote_repo: git@gitea.example.net:network/oxidized.git
    privatekey: /home/oxidized/.ssh/gitea_push
    publickey: /home/oxidized/.ssh/gitea_push.pub
```

Oxidized must also trust the verified Gitea host key. Its
[upstream hook documentation](https://github.com/ytti/oxidized/blob/master/docs/Hooks.md#hook-type-githubrepo)
describes the required legacy-PEM writer key and image-specific paths. This is
one-time setup; normal Libremapper operation remains fetch-only.

For a same-host Oxidized installation, Libremapper can read the repository
directly. Add one read-only mount to the `libremapper` service in
`compose.libremapper.yaml`; the source may be Oxidized's bare Git output or a
normal checked-out worktree:

```yaml
services:
  libremapper:
    volumes:
      - ./oxidized/config/oxidized.git:/oxidized.git:ro
```

The configured `PUID` must be able to read that repository. Choose **Local
Oxidized repository** in Settings; no key, export script, or Oxidized hook is
needed. Apply this mount without rerunning the update helper:

```bash
docker compose -f compose.yml -f compose.libremapper.yaml up -d --no-deps --force-recreate libremapper
```

The Files tab remains available for custom `regex_artifacts` providers and
other advanced sources.

## External scheduling

After a successful manual Apply, enable `scheduled` for the map and invoke the
sidecar from cron or a systemd timer. For a five-minute host cron entry:

```cron
*/5 * * * * cd /absolute/path/to/stack && docker compose -f compose.yml -f compose.libremapper.yaml exec -T libremapper libremapper reconcile
```

Only scheduled maps recorded in SQLite by a prior Apply are eligible. The
command refuses warnings and conflicts, continues through independent maps,
and exits nonzero if any map fails. Manual and scheduled actions share one
filesystem lock.

## Upgrade the sidecar and plugin

Back up the LibreNMS database and both Libremapper volumes first. Install the
target release's `compose.libremapper.yaml` and helper, then run the helper
with the desired version:

```bash
./install-or-update X.Y.Z
```

The helper enforces the required sidecar-first order and stops if the health
version, Composer package, or plugin routes do not match. To roll back, use the
previous release's overlay and helper with its version. No LibreNMS schema
migration is added by Libremapper.

## Failure behavior

Missing or invalid bearer tokens are rejected. The API accepts only validate,
inventory, plan, and apply with managed config and plan paths; it does not accept shell
commands and is not host-published. If the sidecar is unavailable, Files
remain readable, while validation, saves, Preview, and Apply fail without
committing staged configuration.
