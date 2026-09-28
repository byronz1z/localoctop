# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | yes (preview) |

This project is in preview (v0.1.x). Security fixes land on the latest
release line; there are no long-term-support branches yet.

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

- Use GitHub's **private vulnerability reporting** on this repository
  (Security tab → "Report a vulnerability"), or
- contact the repository owner directly.

You should receive an initial response within a few days. Once a report is
accepted, we aim to publish a fix and a release (with credit, unless you
prefer to stay anonymous) as quickly as the severity warrants.

## Scope notes

The security model this project defends is described in the README
("安全模型 / Security model"): directory whitelist with traversal and
symlink-escape protection, Bearer-token authentication, loopback-only web
console, and full local audit logging. Findings that weaken any of these
guarantees are treated as high severity.
