# Upstream notices

This component links to official `anki==26.5`, copyright Ankitects Pty Ltd and
contributors, licensed under AGPL-3.0-or-later.

- Source: https://github.com/ankitects/anki
- Release source commit: `e64c6b1aee3e8d668fb8bbe084beada8e070d985`
- Package: https://pypi.org/project/anki/26.5/

Web application remains separate MIT component. Boundary is SQLite tables
`sync_jobs`, `user_credentials`, `anki_accounts`, `anki_note_links`, and
`worker_heartbeat`; web process never imports or links to this worker or Anki.
