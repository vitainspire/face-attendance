#!/bin/bash
set -uo pipefail

ENV_FILE=/etc/webapp.env

/usr/local/bin/cloudflared tunnel --url http://localhost:8000 --no-autoupdate 2>&1 | while IFS= read -r line; do
    echo "$line"
    if [[ "$line" == *"trycloudflare.com"* ]]; then
        url=$(echo "$line" | grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' | head -1)
        if [[ -n "$url" ]]; then
            current=$(grep '^PUBLIC_BASE_URL=' "$ENV_FILE" | cut -d= -f2-)
            if [[ "$current" != "$url" ]]; then
                sed -i "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=$url|" "$ENV_FILE"
                systemctl restart webapp.service
                echo "SYNCED PUBLIC_BASE_URL -> $url"
            fi
        fi
    fi
done
