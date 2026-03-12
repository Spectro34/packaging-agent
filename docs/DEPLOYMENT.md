# Deployment Guide

## Prerequisites

- **Kubernetes cluster**: RKE2 (tested), K3s, or any K8s distribution
- **Namespace**: `suse-private-ai` (or modify the manifests)
- **Docker**: For building container images
- **n8n**: Running in the same cluster (for the AI workflow)
- **Credentials**: OBS account, OpenAI API key, Slack bot token (optional)

## Building Container Images

### osc-mcp Image

The osc-mcp image is based on openSUSE Tumbleweed and includes `osc`, the full RPM build toolchain, and the pre-compiled osc-mcp Go binary.

```bash
cd agent-factory/production/deploy

# The osc-mcp binary must be pre-compiled and placed in the deploy/ directory
# (Go cross-compilation or build on a Linux host)
docker build -f Dockerfile.osc-mcp -t localhost/osc-mcp:latest .
```

Included packages: `osc`, `build`, `obs-service-download_files`, `obs-service-go_modules`, `obs-service-format_spec_file`, `obs-service-tar_scm`, `obs-service-set_version`, `obs-service-recompress`, `obs-service-obs_scm`, `rpm-build`, `rpmdevtools`, `rpmlint`, `git`, `patch`.

The image uses `entrypoint.sh` which auto-creates `/root/.config/osc/oscrc` from `OBS_USER`, `OBS_PASS`, and `OBS_API_URL` environment variables before launching osc-mcp. This ensures the `osc` CLI (used internally by osc-mcp for commits and builds) has valid credentials.

### packaging-agent Image

The packaging-agent image is a slim Python 3.13 container with only the MCP client libraries.

```bash
cd agent-factory/production/deploy

# Build from the production/ directory (needs access to packaging_agent/)
docker build -f Dockerfile.packaging-agent -t localhost/packaging-agent:latest ../
```

Python dependencies: `fastmcp>=2.13`, `mcp>=1.20`, `httpx`, `httpx-sse`, `anyio`.

Note: `osc` is **not** installed in the packaging-agent container. All OBS operations are delegated to osc-mcp via MCP.

### Importing to RKE2 containerd

RKE2 uses containerd, not Docker, so images must be imported:

```bash
# Save from Docker
docker save localhost/osc-mcp:latest | gzip > osc-mcp.tar.gz
docker save localhost/packaging-agent:latest | gzip > packaging-agent.tar.gz

# Copy to the RKE2 node
scp osc-mcp.tar.gz packaging-agent.tar.gz <user>@<node>:/tmp/

# Import on the node (as root)
ssh <user>@<node>
sudo ctr -n k8s.io images import /tmp/osc-mcp.tar.gz
sudo ctr -n k8s.io images import /tmp/packaging-agent.tar.gz
```

Both Deployment specs use `imagePullPolicy: Never` to use locally imported images.

### Automated Deployment Script

The `deploy/deploy.sh` script automates the full build-import-deploy cycle:

```bash
cd agent-factory/production/deploy

./deploy.sh              # Full deploy: build + import + apply
./deploy.sh build        # Build container images only
./deploy.sh import       # Import images into RKE2 containerd only
./deploy.sh apply        # Apply K8s manifests only
./deploy.sh status       # Show pod and service status
./deploy.sh logs [svc]   # Tail logs for a service (default: packaging-agent)
```

The script:
1. Builds both images with `--no-cache` to ensure fresh builds
2. Extracts the osc-mcp binary from `backups/osc-mcp/osc-mcp-backup.tar.gz` and builds the osc-mcp image
3. Imports both images into RKE2 containerd using `ctr`, re-tagging from `docker.io/library/...` to `localhost/...` (required because `docker save` and `ctr import` use different naming conventions)
4. Applies `k8s-mcp-servers.yaml`, restarts the deployment, and waits for rollout

## Kubernetes Manifest Overview

The manifest `deploy/k8s-mcp-servers.yaml` contains:

### 1. Secret: `mcp-credentials`

Holds all sensitive configuration:

| Key | Description |
|-----|-------------|
| `OBS_USER` | OBS API username |
| `OBS_PASS` | OBS API password |
| `OBS_API_URL` | OBS API endpoint (default: `https://api.opensuse.org`) |
| `OBS_PROJECT` | Default OBS project to manage |
| `OPENAI_API_KEY` | OpenAI API key for GPT-4o |

**Before deploying**: Edit the Secret's `stringData` section with your actual credentials. Better yet, create the secret manually and remove it from the YAML:

```bash
kubectl -n suse-private-ai create secret generic mcp-credentials \
  --from-literal=OBS_USER="<your-obs-user>" \
  --from-literal=OBS_PASS="<your-obs-password>" \
  --from-literal=OBS_API_URL="https://api.opensuse.org" \
  --from-literal=OBS_PROJECT="systemsmanagement:ansible" \
  --from-literal=OPENAI_API_KEY="<your-openai-key>"
```

### 2. PersistentVolumeClaim: `mcp-workdir`

- **Size**: 10Gi
- **Access mode**: ReadWriteOnce
- **Mount path**: `/tmp/mcp-workdir` (both containers)
- **Purpose**: Shared filesystem for osc checkouts, source tarballs, and build artifacts

### 3. Deployment: `osc-mcp`

A single-replica Deployment with **two containers** (sidecar pattern):

**Container: osc-mcp**
- Image: `localhost/osc-mcp:latest`
- Port: 8666
- Privileged: true (required for `osc build`)
- Resources: 512Mi-8Gi RAM, 250m-4000m CPU
- Volumes: `mcp-workdir` PVC + `buildroot` emptyDir (20Gi)
- Command: `/opt/osc-mcp/entrypoint.sh` (auto-creates `oscrc` from env vars, then launches osc-mcp)
- Installed OBS services: `download_files`, `go_modules`, `format_spec_file`, `tar_scm`, `set_version`, `recompress`, `obs_scm` (needed for `_service` packages with `mode="manual"`)

**Container: packaging-agent**
- Image: `localhost/packaging-agent:latest`
- Port: 8667
- Privileged: false
- Resources: 256Mi-1Gi RAM, 100m-1000m CPU
- Volumes: `mcp-workdir` PVC (shared with osc-mcp)
- Env: `MCP_URL=http://localhost:8666/mcp` (connects to osc-mcp sidecar)

Both containers have TCP liveness and readiness probes on their respective ports.

### 4. Services

**Service: osc-mcp**
- Selector: `app: osc-mcp`
- Port: 8666
- Used by n8n to connect to osc-mcp tools directly

**Service: packaging-agent**
- Selector: `app: osc-mcp` (same pod!)
- Port: 8667
- Used by n8n to connect to packaging-agent tools

Both services are ClusterIP (internal only).

## Credential Management

**Never commit credentials to git.** The `k8s-mcp-servers.yaml` file in the repo should use placeholder values. Create the actual Secret via `kubectl create secret` or a secrets management tool.

Environment variables injected into the containers:

| Container | Variable | Source |
|-----------|----------|--------|
| osc-mcp | `OBS_USER` | Secret `mcp-credentials` |
| osc-mcp | `OBS_PASS` | Secret `mcp-credentials` |
| osc-mcp | `OBS_API_URL` | Secret `mcp-credentials` |
| packaging-agent | `MCP_URL` | Hardcoded: `http://localhost:8666/mcp` |
| packaging-agent | `OPENAI_API_KEY` | Secret `mcp-credentials` |
| packaging-agent | `OBS_USER` | Secret `mcp-credentials` |
| packaging-agent | `OBS_PASS` | Secret `mcp-credentials` |
| packaging-agent | `OBS_API_URL` | Secret `mcp-credentials` |
| packaging-agent | `OBS_PROJECT` | Secret `mcp-credentials` |

## n8n Workflow Setup

### Importing the Workflow

1. Open your n8n instance
2. Go to Workflows > Import from File
3. Import `deploy/n8n-package-maintainer.json`

### Workflow Nodes

```
Weekly Scan (cron: Mon 06:00)  -+
                                +--> Prepare Input --> Package Maintainer (AI Agent)
Manual Trigger (webhook: POST) -+          |                    |
                                           |         +----------+----------+
                                           |         |          |          |
                                           |    OpenAI     pkg-agent   osc-mcp
                                           |    GPT-4o     MCP Tools   MCP Tools
                                           |
                                           +--> Format Slack Message --> Send to Slack
```

### Configuring MCP Tool Nodes

Both MCP Client Tool nodes must use:
- **Type version**: 1.2 (not 1)
- **Server transport**: HTTP Streamable
- **Authentication**: None (internal cluster communication)
- **Timeout**: 600000ms (10 minutes, builds can be slow)

**Packaging Agent Tools node**:
- Endpoint URL: `http://packaging-agent:8667/mcp`

**osc-mcp Tools node**:
- Endpoint URL: `http://osc-mcp:8666/mcp`

### Configuring Credentials

Create two credential entries in n8n:
1. **OpenAI API**: Add your API key
2. **Slack API** (optional): Add your Slack bot token for notifications to the `#all-obsagent` channel

### Triggering the Workflow

**Via webhook (POST)**:
```bash
curl -X POST https://<n8n-url>/webhook/package-maintainer \
  -H "Content-Type: application/json" \
  -d '{"command": "upgrade", "package": "molecule", "version": "26.3.0"}'
```

Supported commands:
- `scan` -- scan all packages for CVEs, outdated versions, build failures
- `analyze` -- deep analysis of a single package (requires `package` field)
- `upgrade` -- upgrade a package to a new version (requires `package` and `version` fields)
- `check-updates` -- scan + auto-upgrade LOW/MEDIUM risk packages
- `ask` -- ask a free-text packaging question (pass the question in `package` field)

**Via cron**: The weekly scan runs every Monday at 06:00 UTC.

## Verifying the Deployment

```bash
# Check pod status
kubectl -n suse-private-ai get pods -l app=osc-mcp

# Check both containers are running
kubectl -n suse-private-ai describe pod -l app=osc-mcp

# Test osc-mcp health
kubectl -n suse-private-ai exec deploy/osc-mcp -c osc-mcp -- curl -s http://localhost:8666/mcp

# Test packaging-agent health
kubectl -n suse-private-ai exec deploy/osc-mcp -c packaging-agent -- curl -s http://localhost:8667/mcp

# Check logs
kubectl -n suse-private-ai logs deploy/osc-mcp -c osc-mcp
kubectl -n suse-private-ai logs deploy/osc-mcp -c packaging-agent
```

## Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| packaging-agent can't find checkout files | Separate pods (not sidecar) | Ensure both containers are in the same pod spec |
| osc-mcp crashes on startup | Missing credentials / keyring | Pass `--user` and `--password` CLI flags from env vars |
| `osc build` fails with permission error | Container not privileged | Set `securityContext.privileged: true` on osc-mcp container |
| PVC mount fails | ReadWriteOnce on wrong node | Sidecar pattern ensures same node; delete and recreate PVC if stuck |
| n8n MCP tool timeout | Build takes >10 min | Increase timeout in MCP Client Tool options |
| n8n `sseEndpoint` error | Wrong typeVersion | Use `typeVersion: 1.2` on MCP Client Tool nodes |
| Image not updating after rebuild | containerd tag mismatch | `docker save` creates `docker.io/library/...` but K8s expects `localhost/...`. Use `ctr images tag` to re-tag, or use `deploy.sh` which handles this automatically |
| `osc commit` fails with "install obs-service-format_spec_file" | Missing package in container | Ensure `obs-service-format_spec_file` is in `Dockerfile.osc-mcp` zypper install line |
| `osc` commands fail with "credentials not configured" | Missing `oscrc` file | The `entrypoint.sh` script auto-creates `/root/.config/osc/oscrc` from env vars. Ensure `OBS_USER`, `OBS_PASS`, `OBS_API_URL` are set in the Secret |
| Old tarballs remain on OBS after upgrade | `os.remove()` doesn't tell `osc` about deleted files | Fixed: pipeline writes `.osc/_to_be_deleted` to mark files for proper `osc rm` during commit |
| `_service` packages fail to update tarball | Services not installed in container | Ensure `obs-service-tar_scm`, `obs-service-set_version`, `obs-service-recompress`, `obs-service-go_modules`, `obs-service-obs_scm` are in the Dockerfile |
