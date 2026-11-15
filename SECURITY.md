# Security Policy

## Supported Versions

Only the latest major release receives security fixes. Older versions are
archived and unsupported.

| Version | Supported          |
| ------- | ------------------ |
| 7.x     | :white_check_mark: |
| < 7.0   | :x:                |

## Reporting a Vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's private vulnerability reporting on this repository:
**Security → Advisories → Report a vulnerability**. If that is unavailable,
contact the maintainer listed in `pyproject.toml` directly.

Include the version, operating system, Python version, and a minimal
reproduction. You can expect an acknowledgement within 7 days and an
assessment within 30 days. Once a fix is released, the advisory will be
published with credit unless you ask to remain anonymous.

## Scope notes

- Album passwords are sent to `mitene.us` over HTTPS. `--password` is visible
  in the local process list; prefer `--password-stdin`.
- `config.json` is plain text. Do not commit it — it may contain a password.
- The tool writes EXIF metadata and file records locally; it does not upload
  anything except the password used to unlock the album.
