#!/bin/sh
set -e

# Default to using the Traefik-routed API domain if provided; else keep localhost
API_BASE="${API_BASE:-http://localhost:8000}"

# Replace hardcoded backend origin in all HTML files at runtime
# Handle both http and https localhost URLs, and also the production domain
for f in $(find /usr/share/nginx/html -type f -name "*.html"); do
  # Replace http://localhost:8000 with API_BASE
  sed -i "s|http://localhost:8000|${API_BASE}|g" "$f" || true
  # Replace https://localhost:8000 with API_BASE (in case someone used https locally)
  sed -i "s|https://localhost:8000|${API_BASE}|g" "$f" || true
  # Replace https://api.gridbotbrawl.com with API_BASE (fallback for any missed hardcoded URLs)
  sed -i "s|https://api.gridbotbrawl.com|${API_BASE}|g" "$f" || true
  # Replace http://api.gridbotbrawl.com with API_BASE (fallback for any missed hardcoded URLs)
  sed -i "s|http://api.gridbotbrawl.com|${API_BASE}|g" "$f" || true
  # Fix trailing slash issues: replace /teams? with /teams/? to avoid 307 redirects
  sed -i "s|/teams?|/teams/?|g" "$f" || true
  sed -i "s|/leaderboard?|/leaderboard/?|g" "$f" || true
  sed -i "s|/metadata?|/metadata/?|g" "$f" || true
  sed -i "s|/simulate?|/simulate/?|g" "$f" || true
  sed -i "s|/matches?|/matches/?|g" "$f" || true
done

exec nginx -g 'daemon off;'

