#!/bin/sh
set -e

# Default to using the Traefik-routed API domain if provided; else keep localhost
API_BASE="${API_BASE:-http://localhost:8000}"

# Replace hardcoded backend origin in all HTML files at runtime
for f in $(find /usr/share/nginx/html -type f -name "*.html"); do
  sed -i "s|http://localhost:8000|${API_BASE}|g" "$f" || true
done

exec nginx -g 'daemon off;'

