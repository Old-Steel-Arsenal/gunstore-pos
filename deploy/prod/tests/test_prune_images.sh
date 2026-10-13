#!/usr/bin/env bash
# auto_deploy.sh's prune_images (and its stack_images) against a fake compose wrapper and a fake docker — no docker, no ssh.
#   deploy/prod/tests/test_prune_images.sh
# The functions are cut out of the script and run with a host that holds the release now running,
# the one it replaced, older ones, an image whose tag moved (<none>), MCP builds and an image of an
# unrelated repository.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../auto_deploy.sh"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok()  { pass=$((pass + 1)); echo "  ✔ $*"; }
bad() { fail=$((fail + 1)); echo "  ✘ $*"; }

sed -n '/^stack_images() {$/,/^}$/p; /^prune_images() {$/,/^}$/p' "$SCRIPT" > "$T/fn.sh"
[ "$(grep -c '() {$' "$T/fn.sh")" = 2 ] || { echo "stack_images / prune_images not found in auto_deploy.sh"; exit 1; }

# the POS stack after the deploy: the new release and a database image
cat > "$T/up.sh" <<'SH'
#!/usr/bin/env bash
case "$*" in "config --images") printf 'reg/pos:v3\nmariadb:11.8\n' ;; esac
SH
chmod +x "$T/up.sh"
# fake docker: three containers (POS v3, mariadb, MCP new); `images <repo>` lists that repo
cat > "$T/docker" <<'SH'
#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
case "$*" in
	"ps -aq") printf 'c1\nc2\nc3\n' ;;
	"inspect --format {{.Image}} c1 c2 c3") printf 'sha256:pos3\nsha256:maria\nsha256:mcpnew\n' ;;
	compose*"config --images") [ -n "${FAKE_NO_MCP-}" ] || echo 'reg/pos-mcp:sha-new' ;;
	*" reg/pos") printf 'reg/pos:v3 sha256:pos3\nreg/pos:v2 sha256:pos2\nreg/pos:v1 sha256:pos1\nreg/pos:main-abc sha256:posm\n'
		[ -n "${FAKE_NO_UNTAGGED-}" ] || echo 'reg/pos:<none> sha256:posx' ;;
	*" mariadb") printf 'mariadb:11.8 sha256:maria\nmariadb:10.6 sha256:maria0\n' ;;
	*" reg/pos-mcp") printf 'reg/pos-mcp:sha-new sha256:mcpnew\nreg/pos-mcp:sha-old sha256:mcpold\nreg/pos-mcp:sha-older sha256:mcpolder\n' ;;
	*" wordpress") echo 'wordpress:cli sha256:wp' ;;
	rmi*) [ -z "${FAKE_RMI_FAILS-}" ] || exit 1 ;;
esac
SH
chmod +x "$T/docker"

# run <images the stacks ran before the deploy>; prune_images is called as auto_deploy.sh calls it
run() {
	: > "$T/log"
	PATH="$T:$PATH" FAKE_LOG="$T/log" HERE="$T" UP="$T/up.sh" KEEP_IMAGES="$1" \
		bash -c 'set -euo pipefail; source "$0"; prune_images || { echo "(prune_images failed)"; exit 3; }' "$T/fn.sh" > "$T/out" 2>&1 || true
	grep '^docker rmi' "$T/log" | tr ' ' '\n' | grep -v -e '^docker$' -e '^rmi$' | sort > "$T/removed" || true
}
removed() { grep -qxF "$1" "$T/removed"; }

echo "== a deploy from v2 to v3"
run "$(printf 'reg/pos:v2\nmariadb:11.8\nreg/pos-mcp:sha-old')"
removed reg/pos:v1 && removed reg/pos:main-abc && ok "older POS images removed" || bad "removed: $(cat "$T/removed")"
removed sha256:posx && ! grep -q '<none>' "$T/removed" && ok "an image whose tag moved goes by its ID" || bad "removed: $(cat "$T/removed")"
removed mariadb:10.6 && ok "an unused image of a stack service removed" || bad "removed: $(cat "$T/removed")"
removed reg/pos-mcp:sha-older && ok "an older MCP build removed" || bad "removed: $(cat "$T/removed")"
! removed reg/pos:v2 && ! removed reg/pos-mcp:sha-old && ok "what the stacks ran before is kept (rollback)" || bad "removed: $(cat "$T/removed")"
! removed reg/pos:v3 && ! removed mariadb:11.8 && ! removed reg/pos-mcp:sha-new && ok "images in use kept" || bad "removed: $(cat "$T/removed")"
! grep -q wordpress "$T/log" && ok "other repositories never looked at" || bad "log: $(cat "$T/log")"
[ "$(wc -l < "$T/removed" | tr -d ' ')" = 5 ] && grep -q 'removing 5 old image' "$T/out" && ok "says how many" || bad "out: $(cat "$T/out")"

echo "== nothing old on the host"
FAKE_NO_UNTAGGED=1 run "$(printf 'reg/pos:v1\nreg/pos:v2\nreg/pos:main-abc\nmariadb:10.6\nreg/pos-mcp:sha-old\nreg/pos-mcp:sha-older')"
[ ! -s "$T/removed" ] && grep -q 'no old images' "$T/out" && ok "no rmi, says so" || bad "removed: $(cat "$T/removed"); out: $(cat "$T/out")"

echo "== no MCP stack on the host"
FAKE_NO_MCP=1 run 'reg/pos:v2'
removed reg/pos:v1 && ! grep -q 'reg/pos-mcp' "$T/log" && ok "POS pruned, MCP repository not listed" || bad "log: $(cat "$T/log")"

echo "== a failing rmi fails the function (auto_deploy turns that into a warning)"
FAKE_RMI_FAILS=1 run 'reg/pos:v2'
grep -q 'prune_images failed' "$T/out" && ok "non-zero exit" || bad "out: $(cat "$T/out")"
unset FAKE_RMI_FAILS

echo; echo "prune-images: $pass passed, $fail failed"
[ "$fail" = 0 ]
