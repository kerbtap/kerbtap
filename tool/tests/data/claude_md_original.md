# Acme Pricing Service

Rust + Postgres service that prices quotes for the broker network.

## Architecture

Axum HTTP layer, a pricing core with no IO, and a Postgres store behind a repository trait.
The pricing core is pure so it can be property-tested; nothing else is.

## Commands

- `just test` runs unit and property tests.
- `just migrate` applies sqlx migrations.

## Conventions

Errors bubble as `anyhow::Error` at the edges and concrete types in the core.
No unwrap outside tests.
