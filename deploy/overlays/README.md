# Deploy Overlays

Place Compose/K8s overlay files here to customize the RAGFlow baseline
without modifying upstream files.

## Usage

```bash
# Start with enterprise overlay
docker compose \
  -f ragflow/docker/docker-compose.yml \
  -f deploy/overlays/docker-compose.enterprise.yml \
  up -d
```
