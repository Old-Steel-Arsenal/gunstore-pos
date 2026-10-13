#!/usr/bin/env bash
# Extension agent — the host-side half of a sideload or an upgrade (see docs/self-hosting.md#extensions).
#
# The Extensions page stages a package inside a worker container (code on the
# apps-ext volume, entry in sites/extensions.json, replay proven there) and
# leaves sites/.ext-request.json. Containers cannot restart each other, so this
# script, run by a systemd timer every 20 s (ext-agent.timer), does the rest:
#
#   1. read the request (no file → exit 0, nothing to do). This probe runs every
#      20 s and is almost always empty, so it is plain `docker ps` + `docker exec`
#      on the backend container, not the compose wrapper (which parses six compose
#      files per call); up.sh is only used once there is work
#   2. restart every app container (each replays the manifest on start): workers,
#      then backend, then frontend (nginx resolves the backend IP at its own start
#      and links the assets — auto_deploy.sh restarts it for the same reason)
#   3. wait for backend's replay marker, then finish on the site, in backend:
#        install: ffl_core.extensions.sideload.finish_install
#        upgrade: sideload.begin_upgrade → bench migrate → sideload.finish_upgrade(id, ok, message)
#      (the finish functions write .ext-request.done and remove the request)
#
# Only one agent runs at a time (flock). A step that fails — a restart, the replay
# wait, begin_upgrade, a migrate — is reported and the request file removed, so the
# timer does not loop: an upgrade is still finished through finish_upgrade(id, 0,
# why) while backend can run it (run-5a K4; the card turns Failed with its backup
# path), and anything else — an install, a backend that cannot run it — is written
# into .ext-request.done by the agent itself, where the page reads it. Such a run
# exits 1.
#
#   UP      — the compose wrapper (default: ./deploy/prod/up.sh next to this file's repo root)
#   DOCKER  — the docker CLI for the idle probe; COMPOSE_PROJECT_NAME picks the stack, as in up.sh
#   BENCH   — bench path inside the containers
#   LOCK    — flock file
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UP="${UP:-$HERE/up.sh}"
DOCKER="${DOCKER:-docker}"
PROJECT="${COMPOSE_PROJECT_NAME:-gunstore}"
BENCH="${BENCH:-/home/frappe/frappe-bench}"
LOCK="${LOCK:-/tmp/ext-agent.lock}"
REQUEST="$BENCH/sites/.ext-request.json"
DONE="$BENCH/sites/.ext-request.done"
REPLAY_WAIT="${REPLAY_WAIT:-120}"          # × 5 s = 10 minutes
SERVICES="${SERVICES:-websocket queue-short queue-long scheduler}"
# Opt-in services (compose profiles, e.g. queue-catalog for the local catalog) join
# the restart only when running: restarting a service whose profile is off fails.
OPTIONAL_SERVICES="${OPTIONAL_SERVICES:-queue-catalog}"

log() { echo "ext-agent: $*"; }
exec 9>"$LOCK"
if command -v flock >/dev/null 2>&1; then
	flock -n 9 || { log "another agent run is in progress"; exit 0; }
fi

# oneoff=False: the service's container, never a one-off `up.sh run backend` of the same project
backend="$("$DOCKER" ps -q --filter "label=com.docker.compose.project=$PROJECT" --filter label=com.docker.compose.service=backend --filter label=com.docker.compose.oneoff=False 2>/dev/null | head -n 1 || true)"
[ -n "$backend" ] || exit 0
req="$("$DOCKER" exec "$backend" cat "$REQUEST" 2>/dev/null || true)"
[ -n "$req" ] || exit 0

# The done file has one writer shape (ffl_core.extensions.sideload.mark_done); the
# agent's own failures are serialised on the host with json.dumps — never by string
# interpolation — and piped into the container.
# The request goes with it only when it is <app>'s ("" = the refused request itself), never another
# extension's (sideload._withdraw_request): a migrate that outlives the 30-minute sweep frees the
# pipeline, and the next extension's job writes its own request meanwhile.
write_done() {   # write_done <app> <message>
	python3 -c 'import json, sys, datetime; print(json.dumps({"app": sys.argv[1], "ok": False, "message": sys.argv[2][:500], "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}, indent=2))' "$1" "$2" \
		| "$UP" exec -T backend sh -c "cat > '$DONE'; [ -z '$1' ] || $(its_request "$1") && rm -f '$REQUEST'" || true
}
its_request() {   # its_request <app>: the shell test, run in backend, that the request file is <app>'s (ids are [a-z0-9_])
	printf "grep -Eqs '\"app\": *\"%s\"' '%s'" "$1" "$REQUEST"
}

parsed="$(python3 - "$req" 2>&1 <<'PY'
import json, re, sys
try:
	d = json.loads(sys.argv[1])
except ValueError as e:
	sys.exit(f"bad request file: {e}")
app, site, action = d.get("app", ""), d.get("site", ""), d.get("action", "")
if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", app) or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", site) or action not in ("install", "upgrade"):
	sys.exit(f"request refused: app={app!r} site={site!r} action={action!r}")
print(app); print(site); print(action)
PY
)" || { log "$parsed"; write_done "" "agent refused the request file: $parsed"; exit 1; }
app="$(sed -n 1p <<<"$parsed")"; site="$(sed -n 2p <<<"$parsed")"; action="$(sed -n 3p <<<"$parsed")"

# `bench execute` eval()s --args (frappe/commands/utils.py), so the literal is built by
# json.dumps on the host from strings and ints only — JSON true/false would be a NameError
# there and the whole literal would arrive as one string argument. Its verdict is the exit
# status alone: an exception inside the function comes back as "name '…' is not defined"
# (exit 1), and a function that returns {"ok": false} has already recorded why.
execute() {   # execute <sideload function> <args literal>
	"$UP" exec -T backend bench --site "$site" execute "ffl_core.extensions.sideload.$1" --args "$2" 2>&1
}
id_args="$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$app")"
# ["<id>", 1|0, "<message>"] for finish_upgrade; the message is the output on stdin cut the way
# run-5a K5 fixes it: the last non-empty line first (a traceback's exception, which the card shows),
# then up to 4 lines before it. ensure_ascii=False: an escaped astral character reaches frappe's eval
# as two lone surrogates the database write cannot encode. Bytes that are not UTF-8 become "?".
finish_args() {   # finish_args <1|0> < output
	PYTHONUTF8=1 python3 -c 'import json, sys
lines = [line.rstrip() for line in sys.stdin.read().encode("utf-8", "replace").decode().splitlines() if line.strip()]
print(json.dumps([sys.argv[1], int(sys.argv[2]), "\n".join(lines[-1:] + lines[-5:-1])[:1000]], ensure_ascii=False))' "$app" "$1"
}

give_up() {   # give_up <why>: the agent writes the done file itself (and removes the request), exit 1
	log "$*"
	write_done "$app" "$*"
	exit 1
}
# A step of the agent's own failed. An upgrade is still recorded on the site while backend can run
# finish_upgrade(id, 0, why): the card says Failed with its backup, the done file is written there and
# the request removed — this app's: another extension's request, written once the 30-minute
# sweep freed the pipeline, is not a sign it was not recorded. Otherwise — an install, or no
# finish_upgrade — the agent writes it.
fail() {   # fail <why>
	if [ "$action" = upgrade ]; then
		if execute finish_upgrade "$(finish_args 0 <<<"$*")" >/dev/null && ! "$UP" exec -T backend sh -c "$(its_request "$app")" 2>/dev/null; then
			log "$*"
			exit 1
		fi
		give_up "$* (finish_upgrade could not record it on the site either)"
	fi
	give_up "$*"
}

running="$("$UP" ps --services --status running 2>/dev/null || true)"
for svc in $OPTIONAL_SERVICES; do
	if printf '%s\n' "$running" | grep -qx "$svc"; then SERVICES="$SERVICES $svc"; fi
done
log "$action requested for '$app' on $site — restarting $SERVICES, then backend, then frontend"
"$UP" restart $SERVICES >/dev/null 2>&1 || fail "restart of $SERVICES failed"
"$UP" restart backend >/dev/null 2>&1 || fail "restart of backend failed"
n=0
until "$UP" exec -T backend test -f "$BENCH/.ext-replay.done" 2>/dev/null; do
	n=$((n + 1)); [ "$n" -lt "$REPLAY_WAIT" ] || fail "backend never finished its extension replay ($((REPLAY_WAIT * 5)) s) — see: up.sh logs backend"
	sleep 5
done
"$UP" restart frontend >/dev/null 2>&1 || fail "restart of frontend failed"

status=0
if [ "$action" = install ]; then
	log "installing '$app' on $site"
	out="$(execute finish_install "$id_args")" || fail "finish_install failed (bench execute exit $?): $(tail -n 3 <<<"$out" | tr '\n' ' ')"
else
	log "upgrading '$app' on $site"
	out="$(execute begin_upgrade "$id_args")" || fail "begin_upgrade failed (bench execute exit $?): $(tail -n 3 <<<"$out" | tr '\n' ' ')"
	if out="$("$UP" exec -T backend bench --site "$site" migrate 2>&1)"; then
		# the core's after_migrate recorded upgrade.migrate = succeeded: a failed finish below is no reason to roll back
		ok=1; out=""; outcome="succeeded — the database is updated, only finishing the upgrade was not recorded (docs/self-hosting.md#extensions)"
	else
		ok=0; outcome=failed; status=1
		log "migrate failed: $(tail -n 5 <<<"$out" | tr '\n' ' ')"
	fi
	out="$(execute finish_upgrade "$(finish_args "$ok" <<<"$out")")" || give_up "finish_upgrade failed (bench execute exit $?; migrate $outcome): $(tail -n 3 <<<"$out" | tr '\n' ' ')"
fi
log "$(tail -n 1 <<<"$out")"
# The finish functions remove the request; one of this app's left behind would restart the store every 20 s.
if "$UP" exec -T backend sh -c "$(its_request "$app")" 2>/dev/null; then
	give_up "the $action of '$app' finished without clearing the request — see: up.sh logs backend"
fi
exit "$status"
