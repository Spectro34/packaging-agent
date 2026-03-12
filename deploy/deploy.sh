#!/bin/bash
##
## Deploy osc-mcp + packaging-agent MCP servers to the local RKE2 K8s cluster
##
## Usage:
##   ./deploy.sh                    # Full deploy (build + import + apply)
##   ./deploy.sh build              # Build images only
##   ./deploy.sh apply              # Apply K8s manifests only
##
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROD_DIR="$(dirname "$SCRIPT_DIR")"
BACKUP_DIR="$(dirname "$PROD_DIR")/backups/osc-mcp"

NAMESPACE="suse-private-ai"
CTR="sudo /var/lib/rancher/rke2/bin/ctr --address /run/k3s/containerd/containerd.sock -n k8s.io"

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

log() { echo -e "${BLUE}[deploy]${NC} $1"; }
ok()  { echo -e "${GREEN}[  ok  ]${NC} $1"; }
err() { echo -e "${RED}[error]${NC} $1"; }

# ─── Build Container Images ─────────────────────────────────────────────────
build_images() {
    log "Building container images..."

    # 1. packaging-agent image
    log "Building packaging-agent image..."
    cd "$PROD_DIR"
    docker build --no-cache -t localhost/packaging-agent:latest -f deploy/Dockerfile.packaging-agent .
    ok "packaging-agent image built"

    # 2. osc-mcp image
    log "Building osc-mcp image..."
    TMPDIR=$(mktemp -d)
    tar -xzf "$BACKUP_DIR/osc-mcp-backup.tar.gz" -C "$TMPDIR" 2>/dev/null

    # Find the binary
    OSC_MCP_BIN=$(find "$TMPDIR" -name "osc-mcp" -type f -executable | head -1)
    if [ -z "$OSC_MCP_BIN" ]; then
        # Try any file named osc-mcp that's not .go
        OSC_MCP_BIN=$(find "$TMPDIR" -name "osc-mcp" -type f ! -name "*.go" | head -1)
    fi
    if [ -z "$OSC_MCP_BIN" ]; then
        err "osc-mcp binary not found in backup. Contents:"
        find "$TMPDIR" -type f | head -20
        rm -rf "$TMPDIR"
        return 1
    fi

    cp "$OSC_MCP_BIN" "$SCRIPT_DIR/osc-mcp"
    chmod +x "$SCRIPT_DIR/osc-mcp"
    cd "$SCRIPT_DIR"
    docker build --no-cache -t localhost/osc-mcp:latest -f Dockerfile.osc-mcp .
    rm -f "$SCRIPT_DIR/osc-mcp"
    rm -rf "$TMPDIR"
    ok "osc-mcp image built"
}

# ─── Import Images into RKE2 containerd ─────────────────────────────────────
import_images() {
    log "Importing images into RKE2 containerd..."

    for img in packaging-agent osc-mcp; do
        log "Exporting $img..."
        docker save "localhost/$img:latest" -o "/tmp/$img.tar"

        log "Importing $img into containerd..."
        $CTR images import "/tmp/$img.tar"
        rm -f "/tmp/$img.tar"

        # docker save creates docker.io/library/$img:latest but K8s expects
        # localhost/$img:latest — re-tag to match the K8s manifest references
        DOCKER_TAG="docker.io/library/$img:latest"
        LOCAL_TAG="localhost/$img:latest"
        if $CTR images ls -q | grep -q "^${DOCKER_TAG}$"; then
            log "Re-tagging $DOCKER_TAG -> $LOCAL_TAG"
            $CTR images tag "$DOCKER_TAG" "$LOCAL_TAG" 2>/dev/null || true
        fi

        ok "$img imported"
    done

    log "Verifying images in containerd..."
    $CTR images ls | grep -E "packaging-agent|osc-mcp" || true
}

# ─── Apply K8s Manifests ────────────────────────────────────────────────────
apply_manifests() {
    log "Applying K8s manifests..."
    /usr/local/bin/kubectl apply -f "$SCRIPT_DIR/k8s-mcp-servers.yaml"
    ok "K8s manifests applied"

    # Restart pods to pick up new images
    log "Restarting deployments to pick up new images..."
    /usr/local/bin/kubectl -n $NAMESPACE rollout restart deployment/osc-mcp 2>/dev/null || true

    # Wait for deployments
    log "Waiting for osc-mcp..."
    /usr/local/bin/kubectl -n $NAMESPACE rollout status deployment/osc-mcp --timeout=180s || true

    # Status
    log "Pod status:"
    /usr/local/bin/kubectl get pods -n $NAMESPACE -l 'app in (osc-mcp, packaging-agent)' -o wide

    log "Service status:"
    /usr/local/bin/kubectl get svc -n $NAMESPACE -l 'app in (osc-mcp, packaging-agent)'
}

# ─── Main ────────────────────────────────────────────────────────────────────
CMD="${1:-all}"

case "$CMD" in
    build)
        build_images
        ;;
    import)
        import_images
        ;;
    apply)
        apply_manifests
        ;;
    all)
        build_images
        import_images
        apply_manifests
        ;;
    status)
        /usr/local/bin/kubectl get pods -n $NAMESPACE -l 'app in (osc-mcp, packaging-agent)' -o wide
        /usr/local/bin/kubectl get svc -n $NAMESPACE -l 'app in (osc-mcp, packaging-agent)'
        ;;
    logs)
        SVC="${2:-packaging-agent}"
        /usr/local/bin/kubectl logs -n $NAMESPACE -l "app=$SVC" --tail=50
        ;;
    *)
        echo "Usage: $0 {build|import|apply|all|status|logs [svc]}"
        exit 1
        ;;
esac

ok "Done!"
