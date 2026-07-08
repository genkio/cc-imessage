#!/usr/bin/env bash
# Create the self-signed code-signing identity that `make build` signs with.
# Signing gives a stable designated requirement (identifier + cert leaf) instead
# of a per-build cdhash, so macOS Full Disk Access / Automation grants survive
# brew upgrades rather than resetting on every rebuild. Idempotent.
#
# Losing this identity (new machine, keychain wipe) means the next release has a
# different cert -> one more manual re-grant, then stable again.
set -euo pipefail

ID="cc-imessage-selfsign"
if security find-identity -v 2>/dev/null | grep -q "$ID"; then
  echo "identity '$ID' already present"
  exit 0
fi

OSSL="$(command -v /opt/homebrew/opt/openssl@3/bin/openssl openssl | head -1)"
tmp="$(mktemp -d)"
cat > "$tmp/cnf" <<'EOF'
[req]
distinguished_name=dn
x509_extensions=v3
prompt=no
[dn]
CN=cc-imessage-selfsign
[v3]
keyUsage=critical,digitalSignature
extendedKeyUsage=critical,codeSigning
basicConstraints=critical,CA:FALSE
EOF

"$OSSL" req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
  -keyout "$tmp/key" -out "$tmp/crt" -config "$tmp/cnf"
# -legacy + a non-empty password: macOS `security` rejects an OpenSSL 3 p12 otherwise
"$OSSL" pkcs12 -export -legacy -out "$tmp/p12" -inkey "$tmp/key" -in "$tmp/crt" -passout pass:ccimg
security import "$tmp/p12" -k "$HOME/Library/Keychains/login.keychain-db" \
  -P ccimg -T /usr/bin/codesign -A
rm -rf "$tmp"
echo "created signing identity '$ID'"
