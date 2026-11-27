---
paths:
  - "Dockerfile*"
  - "docker-compose*"
  - "infra/docker/**"
  - "**/Dockerfile"
---

# Docker rules

## Volume mounts — verify read vs write before `:ro`

Before mounting any volume as `:ro`, verify the service NEVER writes to that path. Check for:
- `conn.commit()` calls (SQLite/database writes)
- `init_db()` or schema-creation logic (creates the DB file)
- File creation, log writes, cache writes

When in doubt, mount `:rw` and add a comment explaining why.

**Why:** A container mounted a data volume as `:ro`, but the service writes to a `log` table in its SQLite database and `init_db()` creates the DB file on first run. The read-only mount caused a crash loop on the production server.

## Delegation

All Docker work is delegated to the `docker-expert` agent (per memory `feedback_docker_to_expert`). Do not write or modify Dockerfiles / compose / docker-related shell directly without dispatching docker-expert first.
