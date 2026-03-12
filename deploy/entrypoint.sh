#!/bin/bash
# Create oscrc for osc CLI from environment variables
# osc-mcp passes creds via CLI args, but osc CLI needs oscrc for checkout/commit/build
mkdir -p /root/.config/osc
cat > /root/.config/osc/oscrc <<EOF
[general]
apiurl = ${OBS_API_URL:-https://api.opensuse.org}

[${OBS_API_URL:-https://api.opensuse.org}]
user = ${OBS_USER}
pass = ${OBS_PASS}
EOF
chmod 600 /root/.config/osc/oscrc

exec /opt/osc-mcp/osc-mcp \
  --http 0.0.0.0:8666 \
  --workdir /tmp/mcp-workdir \
  --api "${OBS_API_URL:-https://api.opensuse.org}" \
  --user "${OBS_USER}" \
  --password "${OBS_PASS}" \
  -d
