# Changelog

## 0.3.0

- Package the SSH MCP service for independent Linux deployments with portable examples.
- Add private inventory initialization, offline validation, selected-target validation,
  target listing, and foreground execution to the CLI.
- Keep existing host IDs, two-node groups, credentials, and MCP tools compatible.
- Support spaces in configured filesystem paths and home expansion in local upload roots.
- Reject malformed YAML mapping keys and non-integer schema versions with stable errors.
- Read the service version from one package version definition.
- Add setup, migration, authentication, routing, and troubleshooting documentation.
- Add Python 3.10–3.13 CI and SSH/SFTP integration regression tests.

## 0.2.0

- Local deployment supporting standalone hosts and media/main groups, pooled SSH,
  background commands, atomic file editing, and checksum-verified binary uploads.
