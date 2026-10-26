# @hyadex/server

Hyadex's self-hosted server, distributed with its existing Python runtime
installer. The desktop app remains a separate signed native download.

This package has no installation lifecycle hooks. Installing the npm package
does not start, stop, upgrade, or log into a server. The service runs from its
own versioned installation directory, never from an npm or npx cache.

## First installation

After this package is published, a fresh user installation can use:

```sh
npx @hyadex/server@beta install --port 7870
```

Use an exact package version for reproducible installations. The command needs
the same prerequisites as the source installer: a trusted `uv`, Linux with a
working user systemd session or Apple silicon macOS with a GUI launchd session,
and the desired provider CLIs. It runs as the current user without `sudo`.

The npm entrypoint only permits a fresh default installation. It refuses an
existing managed installation, legacy service, or custom installation roots;
changing directories does not create a second service. Existing servers use the
source installer after the selected service has been stopped. Multiple named
services are not enabled by this package.

## Recovery

The npm recovery command is retired. For a current source installation, use
`./install.sh --recover-only` on the selected instance. Older activation journals
require their retained previous installer; the new installer preserves and
refuses them. See the source installation reference.

## Updating an existing server

The managed update command and its server API are removed. Neither
`hyadex-server update` nor updating this npm package upgrades a running service.
Use the source checkout's manual installation workflow, stopping the selected
service first. See [the server reference](../docs/SERVER_REFERENCE.md#updating-hyadexserver).
The source installer now owns manual installation and recovery.

## Packaging

The source `server/package.json` is deliberately private. Run
`python3 server/scripts/package_npm_release.py --output dist/npm-server` from
the repository root to stage only the audited runtime allowlist, stamp
`server/VERSION`, and produce the publishable tarball plus its unsigned release
descriptor. The manual preparation workflow signs that descriptor. This command
does not publish to npm. Publish only a reviewed and verified prepared tarball.
