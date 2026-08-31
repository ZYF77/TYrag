# ADR: Preserve the RAGFlow runtime service template in the image

- Status: Accepted
- Date: 2026-08-31
- Scope: `ragflow/Dockerfile`

## Context

RAGFlow's entrypoint removes `conf/service_conf.yaml` and regenerates it from
`conf/service_conf.yaml.template`. The production image copied the non-empty
Docker template first, then copied `conf/`, whose empty placeholder overwrote
it. A clean release image therefore restarted before serving HTTP.

## Decision

Copy the complete `conf/` directory first, then copy the Docker runtime
template and fail the image build if the resulting template is empty. This
keeps environment substitution in the existing entrypoint and changes no
runtime service configuration.

## Alternatives rejected

- Keep the bundled `service_conf.yaml`: bypasses the existing environment
  substitution path and can preserve stale values.
- Add a runtime volume or deployment-specific file: hides an image build bug
  and risks replacing the server's existing configuration.

## Verification and rollback

The release build must pass the non-empty-template check and a one-shot image
marker check before packaging. Rollback is the previous immutable image tag;
the Dockerfile change has no database or public API effect.
