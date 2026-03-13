#!/bin/bash
# ============================================================================
# Packaging Agent — Quick Setup
# Interactive setup for local development and testing.
# Creates .env with credentials, verifies dependencies, and runs a test.
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
CONFIG_FILE="$SCRIPT_DIR/config.json"
OSC_MCP_BIN="$SCRIPT_DIR/deploy/osc-mcp"
BACKUP_TAR="$SCRIPT_DIR/../backups/osc-mcp/osc-mcp-backup.tar.gz"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${CYAN}[*]${NC} $1"; }
ok()    { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
err()   { echo -e "${RED}[-]${NC} $1"; }

banner() {
    echo ""
    echo -e "${BOLD}  ┌──────────────────────────────────────────┐${NC}"
    echo -e "${BOLD}  │   openSUSE Packaging Agent — Setup       │${NC}"
    echo -e "${BOLD}  │   AI-powered OBS package maintenance     │${NC}"
    echo -e "${BOLD}  └──────────────────────────────────────────┘${NC}"
    echo ""
}

# ── Step 0: Banner ──────────────────────────────────────────────────────────
banner

# ── Step 1: Check Python ────────────────────────────────────────────────────
info "Checking Python..."
if ! command -v python3 &>/dev/null; then
    err "Python 3 not found. Install it first."
    exit 1
fi
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
ok "Python $PY_VER found"

# ── Step 2: Check/Install Python deps ──────────────────────────────────────
info "Checking Python dependencies..."
MISSING=0
for pkg in fastmcp mcp httpx anyio; do
    if ! python3 -c "import $pkg" 2>/dev/null; then
        MISSING=1
        break
    fi
done

if [ $MISSING -eq 1 ]; then
    echo ""
    echo -e "  Required packages: ${BOLD}fastmcp mcp httpx httpx-sse anyio${NC}"
    read -p "  Install them now? (pip install -r requirements.txt) [Y/n] " -r INSTALL
    if [[ "$INSTALL" =~ ^[Nn] ]]; then
        warn "Skipping. Install manually: pip install -r $SCRIPT_DIR/requirements.txt"
    else
        pip install -r "$SCRIPT_DIR/requirements.txt" --quiet
        ok "Python dependencies installed"
    fi
else
    ok "All Python dependencies present"
fi

# ── Step 3: Check osc-mcp binary ───────────────────────────────────────────
info "Checking osc-mcp binary..."
if [ -f "$OSC_MCP_BIN" ]; then
    ok "osc-mcp binary found at deploy/osc-mcp"
else
    warn "osc-mcp binary not found at deploy/osc-mcp"
    # Try building from source if Go is available
    if command -v go &>/dev/null; then
        echo ""
        read -p "  Go is available. Build osc-mcp from source? [Y/n] " -r BUILD_GO
        if [[ ! "$BUILD_GO" =~ ^[Nn] ]]; then
            info "Building osc-mcp..."
            (cd "$SCRIPT_DIR/deploy" && go build -o osc-mcp . 2>&1)
            if [ -f "$OSC_MCP_BIN" ]; then
                chmod +x "$OSC_MCP_BIN"
                ok "osc-mcp built successfully"
            else
                warn "Build failed. Try: cd deploy && go build -o osc-mcp ."
            fi
        fi
    elif [ -f "$BACKUP_TAR" ]; then
        echo ""
        read -p "  Extract from backup ($BACKUP_TAR)? [Y/n] " -r EXTRACT
        if [[ ! "$EXTRACT" =~ ^[Nn] ]]; then
            tar -xzf "$BACKUP_TAR" -C "$SCRIPT_DIR/deploy/" --strip-components=1 osc-mcp 2>/dev/null \
                || tar -xzf "$BACKUP_TAR" -C /tmp/ && cp /tmp/osc-mcp "$OSC_MCP_BIN" 2>/dev/null \
                || true
            if [ -f "$OSC_MCP_BIN" ]; then
                chmod +x "$OSC_MCP_BIN"
                ok "osc-mcp extracted"
            else
                warn "Could not extract osc-mcp. You'll need Docker Compose mode."
            fi
        fi
    else
        warn "Go is not installed and no binary found."
        echo "  Options:"
        echo "    1. Install Go (zypper install go) and re-run setup"
        echo "    2. Build manually: cd deploy && go build -o osc-mcp ."
        echo "    3. Use Docker Compose: docker compose up"
    fi
fi

# ── Step 4: Credentials ────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  Credentials Setup${NC}"
echo -e "  Stored in ${BOLD}.env${NC} (gitignored, never committed)"
echo ""

# Load existing values if .env exists
EXISTING_OPENAI_KEY=""
EXISTING_OBS_USER=""
EXISTING_OBS_PASS=""
EXISTING_OBS_API=""
EXISTING_OBS_PROJECT=""

if [ -f "$ENV_FILE" ]; then
    source <(grep -v '^\s*#' "$ENV_FILE" | grep '=' || true)
    EXISTING_OPENAI_KEY="${OPENAI_API_KEY:-}"
    EXISTING_OBS_USER="${OBS_USER:-}"
    EXISTING_OBS_PASS="${OBS_PASS:-}"
    EXISTING_OBS_API="${OBS_API_URL:-}"
    EXISTING_OBS_PROJECT="${OBS_PROJECT:-}"
    info "Existing .env found. Press Enter to keep current values."
    echo ""
fi

# Prompt helper: show masked current value
prompt_secret() {
    local prompt="$1"
    local current="$2"
    local varname="$3"
    if [ -n "$current" ]; then
        local masked="${current:0:4}...${current: -4}"
        read -p "  $prompt [$masked]: " -r VALUE
        if [ -z "$VALUE" ]; then
            eval "$varname=\"$current\""
        else
            eval "$varname=\"$VALUE\""
        fi
    else
        read -p "  $prompt: " -r VALUE
        eval "$varname=\"$VALUE\""
    fi
}

prompt_value() {
    local prompt="$1"
    local current="$2"
    local default="$3"
    local varname="$4"
    local show="${current:-$default}"
    read -p "  $prompt [$show]: " -r VALUE
    if [ -z "$VALUE" ]; then
        eval "$varname=\"$show\""
    else
        eval "$varname=\"$VALUE\""
    fi
}

echo -e "  ${BOLD}1. OpenAI API Key${NC} (for GPT-4o spec updates and review)"
prompt_secret "OPENAI_API_KEY" "$EXISTING_OPENAI_KEY" "NEW_OPENAI_KEY"
echo ""

echo -e "  ${BOLD}2. Open Build Service Credentials${NC}"
echo "  Register at https://build.opensuse.org if you don't have an account."
prompt_value "OBS_USER" "$EXISTING_OBS_USER" "" "NEW_OBS_USER"
prompt_secret "OBS_PASS" "$EXISTING_OBS_PASS" "NEW_OBS_PASS"
echo ""

echo -e "  ${BOLD}3. OBS Settings${NC}"
prompt_value "OBS_API_URL" "$EXISTING_OBS_API" "https://api.opensuse.org" "NEW_OBS_API"
prompt_value "OBS_PROJECT" "$EXISTING_OBS_PROJECT" "devel:languages:python" "NEW_OBS_PROJECT"
echo ""

# Write .env
cat > "$ENV_FILE" <<EOF
# Generated by setup.sh — $(date)
# This file is gitignored. Never commit credentials.

OPENAI_API_KEY=$NEW_OPENAI_KEY
OBS_USER=$NEW_OBS_USER
OBS_PASS=$NEW_OBS_PASS
OBS_API_URL=$NEW_OBS_API
OBS_PROJECT=$NEW_OBS_PROJECT
EOF
chmod 600 "$ENV_FILE"
ok "Credentials saved to .env (mode 600)"

# Also write config.json for CLI usage (same precedence: env > config.json)
cat > "$CONFIG_FILE" <<EOF
{
    "openai_api_key": "$NEW_OPENAI_KEY",
    "obs_user": "$NEW_OBS_USER",
    "obs_pass": "$NEW_OBS_PASS",
    "obs_api_url": "$NEW_OBS_API",
    "obs_project": "$NEW_OBS_PROJECT"
}
EOF
chmod 600 "$CONFIG_FILE"
ok "Config saved to config.json (mode 600)"

# ── Step 5: Verify credentials ─────────────────────────────────────────────
echo ""
info "Verifying credentials..."

# Test OBS API
OBS_OK=0
if [ -n "$NEW_OBS_USER" ] && [ -n "$NEW_OBS_PASS" ]; then
    HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' \
        -u "$NEW_OBS_USER:$NEW_OBS_PASS" \
        "$NEW_OBS_API/person/$NEW_OBS_USER" </dev/null 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
        ok "OBS API: authenticated"
        OBS_OK=1
    else
        err "OBS API: HTTP $HTTP_CODE (check username/password)"
    fi
else
    warn "OBS credentials not provided — skipping verification"
fi

# Test OpenAI API
OPENAI_OK=0
if [ -n "$NEW_OPENAI_KEY" ]; then
    HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer $NEW_OPENAI_KEY" \
        "https://api.openai.com/v1/models" </dev/null 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
        ok "OpenAI API: authenticated"
        OPENAI_OK=1
    else
        err "OpenAI API: HTTP $HTTP_CODE (check API key)"
    fi
else
    warn "OpenAI key not provided — skipping verification"
fi

# ── Step 6: Choose run mode ────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  How do you want to run osc-mcp?${NC}"
echo ""
echo "  1) Local binary    — fastest, needs osc-mcp in deploy/"
echo "  2) Docker Compose  — easiest, needs Docker"
echo "  3) Skip            — I'll set it up myself"
echo ""
read -p "  Choice [1/2/3]: " -r MODE

OSC_MCP_PID=""
case "$MODE" in
    1)
        if [ -f "$OSC_MCP_BIN" ]; then
            ok "Local mode selected. osc-mcp binary ready."
            echo ""
            read -p "  Start osc-mcp now? [Y/n] " -r START_MCP
            if [[ ! "$START_MCP" =~ ^[Nn] ]]; then
                mkdir -p /tmp/mcp-workdir
                info "Starting osc-mcp on port 8666..."
                "$OSC_MCP_BIN" \
                    --http 0.0.0.0:8666 \
                    --workdir /tmp/mcp-workdir \
                    --api "$NEW_OBS_API" \
                    --user "$NEW_OBS_USER" \
                    --password "$NEW_OBS_PASS" -d &
                OSC_MCP_PID=$!
                sleep 2
                if kill -0 "$OSC_MCP_PID" 2>/dev/null; then
                    ok "osc-mcp running (PID $OSC_MCP_PID)"
                else
                    err "osc-mcp failed to start. Check credentials and try manually:"
                    echo "    $OSC_MCP_BIN --http 0.0.0.0:8666 --workdir /tmp/mcp-workdir \\"
                    echo "      --api $NEW_OBS_API --user $NEW_OBS_USER --password \$OBS_PASS -d"
                    OSC_MCP_PID=""
                fi
            else
                echo ""
                echo -e "  ${BOLD}Start osc-mcp manually:${NC}"
                echo "    $OSC_MCP_BIN --http 0.0.0.0:8666 --workdir /tmp/mcp-workdir \\"
                echo "      --api $NEW_OBS_API --user $NEW_OBS_USER --password \$OBS_PASS -d"
            fi
        else
            err "osc-mcp binary not found. Use Docker Compose instead (option 2)."
        fi
        ;;
    2)
        if command -v docker &>/dev/null; then
            ok "Docker Compose mode selected."
            echo ""
            read -p "  Start Docker Compose now? [Y/n] " -r START_DC
            if [[ ! "$START_DC" =~ ^[Nn] ]]; then
                info "Starting docker compose..."
                docker compose -f "$SCRIPT_DIR/docker-compose.yml" up -d
                if [ $? -eq 0 ]; then
                    ok "Docker containers running"
                else
                    err "Docker Compose failed. Check docker-compose.yml and try manually."
                fi
            else
                echo ""
                echo -e "  ${BOLD}Start manually:${NC}"
                echo "    cd $SCRIPT_DIR"
                echo "    docker compose up -d"
            fi
        else
            err "Docker not found. Install Docker first."
        fi
        ;;
    *)
        ok "Manual setup. See README.md for details."
        ;;
esac

# ── Step 7: Quick test ─────────────────────────────────────────────────────
echo ""
read -p "  Run a quick test upgrade now? [y/N] " -r RUN_TEST

if [[ "$RUN_TEST" =~ ^[Yy] ]]; then
    echo ""
    echo -e "  ${BOLD}Pick a test package:${NC}"
    echo "  1) python-ConfigArgParse 1.7.5  (small, fast)"
    echo "  2) python-PyJWT 2.11.0          (popular, medium)"
    echo "  3) python-Faker 40.8.0          (large, 2MB tarball)"
    echo "  4) Custom"
    echo ""
    read -p "  Choice [1]: " -r PKG_CHOICE

    case "$PKG_CHOICE" in
        2) TEST_PKG="python-PyJWT"; TEST_VER="2.11.0" ;;
        3) TEST_PKG="python-Faker"; TEST_VER="40.8.0" ;;
        4)
            read -p "  Package name: " -r TEST_PKG
            read -p "  Target version: " -r TEST_VER
            ;;
        *) TEST_PKG="python-ConfigArgParse"; TEST_VER="1.7.5" ;;
    esac

    read -p "  OBS project [devel:languages:python]: " -r TEST_PROJECT
    TEST_PROJECT="${TEST_PROJECT:-devel:languages:python}"

    echo ""
    info "Running: upgrade $TEST_PKG $TEST_VER --live --project $TEST_PROJECT"
    echo ""

    # Determine how to run it
    if [ "$MODE" = "2" ] && command -v docker &>/dev/null; then
        docker compose -f "$SCRIPT_DIR/docker-compose.yml" exec packaging-agent \
            python3 -m packaging_agent upgrade "$TEST_PKG" "$TEST_VER" \
            --live --project "$TEST_PROJECT"
    else
        cd "$SCRIPT_DIR"
        export OPENAI_API_KEY="$NEW_OPENAI_KEY"
        export OBS_USER="$NEW_OBS_USER"
        export OBS_PASS="$NEW_OBS_PASS"
        export OBS_API_URL="$NEW_OBS_API"
        export OBS_PROJECT="$TEST_PROJECT"
        python3 -m packaging_agent upgrade "$TEST_PKG" "$TEST_VER" \
            --live --project "$TEST_PROJECT"
    fi
fi

# ── Done ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  Setup complete!${NC}"
echo ""
if [ -n "$OSC_MCP_PID" ] && kill -0 "$OSC_MCP_PID" 2>/dev/null; then
    echo -e "  osc-mcp is running (PID $OSC_MCP_PID) — ready for --live upgrades"
    echo "  To stop it: kill $OSC_MCP_PID"
    echo ""
fi
echo "  Quick reference:"
echo "    source .env"
echo "    python3 -m packaging_agent upgrade <package> <version> --live"
echo "    python3 -m packaging_agent analyze <package>"
echo "    python3 -m packaging_agent ask \"how to package a Python wheel?\""
echo ""
echo "  Docs: README.md | Architecture: docs/ARCHITECTURE.md"
echo ""
