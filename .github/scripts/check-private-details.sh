#!/usr/bin/env bash
# Fails if a tracked text file contains details that belong to someone's own
# network or identity rather than this public repo: private (LAN) IP addresses
# other than the documentation examples (192.168.1.x), or email addresses other
# than no-reply ones. Run from the repo root; the PR checks run it on every PR.
set -euo pipefail

ip='\b(10\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])|192\.168)\.[0-9]{1,3}\.[0-9]{1,3}\b'
email='[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'

found=0
while IFS= read -r -d '' f; do
    grep -Iq . "$f" 2>/dev/null || continue   # skip binary and empty files
    hits="$(
        grep -nEo "$ip" "$f" | grep -vE ':192\.168\.1\.[0-9]{1,3}$' || true
        grep -nEo "$email" "$f" | grep -viE 'noreply|example\.(com|org|net)|\.(service|socket|target|timer|mount|path)$' || true
    )"
    if [ -n "$hits" ]; then
        printf '%s\n' "$hits" | sed "s|^|$f:|"
        found=1
    fi
done < <(git ls-files -z)

if [ "$found" = 1 ]; then
    echo "Private details found above. Use an example instead (192.168.1.x, someone@example.com)."
    exit 1
fi
echo "No private IPs or personal email addresses."
