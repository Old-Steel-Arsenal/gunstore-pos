#!/usr/bin/env bash
# ext-agent.sh against a fake compose wrapper and a fake docker CLI — no docker, seconds.
#   deploy/prod/tests/test_ext_agent.sh
# The shims record every call and play the container side: the request file, the
# replay marker, bench migrate, and the sideload functions — `bench execute` is
# emulated the way frappe runs it (eval of --args, the whole string as one argument
# on a NameError), so a literal JSON true/false or a pasted message would show here.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT="$HERE/../ext-agent.sh"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok()  { pass=$((pass + 1)); echo "  ✔ $*"; }
bad() { fail=$((fail + 1)); echo "  ✘ $*"; }
step() { echo; echo "== $*"; }

# --- the shims: $T/up.sh (compose wrapper) and $T/docker (idle probe) ---------------
mkdir -p "$T/bench/sites"
cat > "$T/up.sh" <<'SH'
#!/usr/bin/env bash
# fake up.sh: logs calls, serves files from $FAKE_BENCH
echo "$*" >> "$FAKE_LOG"
case "$1 $2 $3 $4" in
	"ps --services --status running") printf '%s\n' ${FAKE_RUNNING:-} ;;
	"exec -T backend cat")      cat "${5/#\/home\/frappe\/frappe-bench/$FAKE_BENCH}" ;;
	"exec -T backend test")     test "$5" "${6/#\/home\/frappe\/frappe-bench/$FAKE_BENCH}" ;;
	"exec -T backend sh")       shift 4; c="${*//\/home\/frappe\/frappe-bench/$FAKE_BENCH}"; sh -c "${c#-c }" ;;
	"exec -T backend bench")
		if [ "$7" = migrate ]; then
			# FAKE_MIGRATE_WRITES_REQUEST=<app>: the migrate outlives the 30-minute sweep, which consumes this request and
			# frees the pipeline; <app>'s staging job writes its own request meanwhile
			[ -z "${FAKE_MIGRATE_WRITES_REQUEST:-}" ] || printf '{"action": "install", "app": "%s", "site": "pos.example.com"}' "$FAKE_MIGRATE_WRITES_REQUEST" > "$FAKE_BENCH/sites/.ext-request.json"
			[ -z "${FAKE_MIGRATE_FAILS:-}" ] || { printf '%s\n' "${FAKE_BOOM:-migrate boom}"; exit 1; }
			echo "Migrating $6"; exit 0
		fi
		method="${8##*.}"
		case " ${FAKE_EXEC_FAILS:-} " in *" $method "*) echo "bench: ${FAKE_BOOM:-boom}"; exit 1 ;; esac
		python3 - "$method" "${10}" <<'PY'
import json, os, sys
method, raw = sys.argv[1], sys.argv[2]
try:
	args = eval(raw)                     # frappe/commands/utils.py: fn_args = eval(args)
except NameError:
	args = [raw]
bench = os.environ["FAKE_BENCH"]
with open(f"{bench}/{method}.args", "w") as f:
	json.dump(args, f)
if method == "finish_upgrade":
	str(args[2]).encode("utf-8")         # set_state's database write: a lone surrogate raises here, exit 1
if method in ("finish_install", "finish_upgrade") and not os.environ.get("FAKE_KEEP_REQUEST"):
	ok = bool(args[1]) if method == "finish_upgrade" else True           # mark_done(app, ok, …)
	with open(f"{bench}/sites/.ext-request.done", "w") as f:
		json.dump({"app": args[0], "ok": ok, "message": method, "finished_at": "now"}, f)
	try:                                                                  # _withdraw_request: its own (or unreadable) only
		mine = json.load(open(f"{bench}/sites/.ext-request.json")).get("app") in (None, args[0])
	except (OSError, ValueError):
		mine = True
	if mine and os.path.exists(f"{bench}/sites/.ext-request.json"):
		os.remove(f"{bench}/sites/.ext-request.json")
print(os.environ.get("FAKE_EXEC_TEXT") or json.dumps({"ok": True}))
PY
		;;
	"restart"*)
		# FAKE_RESTART_WRITES_REQUEST=<app>: the agent's run started late, the 30-minute sweep consumed this upgrade's request
		# and freed the pipeline, and <app>'s staging job wrote its own request meanwhile
		[ -z "${FAKE_RESTART_WRITES_REQUEST:-}" ] || printf '{"action": "install", "app": "%s", "site": "pos.example.com"}' "$FAKE_RESTART_WRITES_REQUEST" > "$FAKE_BENCH/sites/.ext-request.json"
		if [ -n "${FAKE_RESTART_FAILS:-}" ] && [[ "$*" == *"$FAKE_RESTART_FAILS"* ]]; then exit 1; fi
		[ -n "${FAKE_NO_REPLAY:-}" ] || touch "$FAKE_BENCH/.ext-replay.done" ;;
	"logs"*)                    echo "fake logs" ;;
esac
SH
cat > "$T/docker" <<'SH'
#!/usr/bin/env bash
# fake docker: two backend containers of project $FAKE_PROJECT — a one-off `up.sh run backend`
# (newest, so `ps` lists it first; it has no request file) and the service's 3f2a9c. `ps` prints
# the ones every --filter label=… matches; `exec` runs in $FAKE_BENCH, for 3f2a9c only.
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
	ps)
		for c in "9e0ff1 True" "3f2a9c False"; do
			labels=" com.docker.compose.project=${FAKE_PROJECT:-gunstore} com.docker.compose.service=backend com.docker.compose.oneoff=${c#* } "
			match=1
			for a in "$@"; do case "$a" in label=*) [[ "$labels" == *" ${a#label=} "* ]] || match=0 ;; esac; done
			[ "$match" = 0 ] || echo "${c%% *}"
		done ;;
	exec) [ "$2" = 3f2a9c ] || exit 1; shift 2; "$1" "${2/#\/home\/frappe\/frappe-bench/$FAKE_BENCH}" ;;
esac
SH
chmod +x "$T/up.sh" "$T/docker"
export FAKE_BENCH="$T/bench" FAKE_LOG="$T/calls.log" FAKE_DOCKER_LOG="$T/docker.log"
unset COMPOSE_PROJECT_NAME             # the agent picks the stack from it; each case sets it explicitly
run_agent() { : > "$FAKE_LOG"; : > "$FAKE_DOCKER_LOG"; rm -f "$T"/bench/*.args; UP="$T/up.sh" DOCKER="$T/docker" LOCK="$T/lock" REPLAY_WAIT="${REPLAY_WAIT:-2}" bash "$AGENT"; }
request() { printf '{"action":"%s","app":"%s","site":"pos.example.com","requested_at":"x","requested_by":"y"}' "$1" "${2:-acme_layaway}" > "$T/bench/sites/.ext-request.json"; }
done_json() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d[sys.argv[2]])' "$T/bench/sites/.ext-request.done" "$1"; }
line_of() { { grep -n -m1 -- "$1" "$FAKE_LOG" || true; } | cut -d: -f1; }
# args_are <method> <python expression over `a`, the arguments frappe's eval produced> — expressions are
# written in this file only (eval of test code, as the shim's frappe emulation above)
args_are() { python3 -c 'import json, sys; a = json.load(open(sys.argv[1])); assert eval(sys.argv[2]), a' "$T/bench/$1.args" "$2" 2>/dev/null; }

step "no request → nothing happens, and the idle probe never touches the compose wrapper"
rc=0; out="$(run_agent)" || rc=$?
[ "$rc" = 0 ] && [ -z "$out" ] && ok "silent exit 0" || bad "rc=$rc out=$out"
[ ! -s "$FAKE_LOG" ] && ok "no up.sh call at all" || bad "compose wrapper called while idle: $(cat "$FAKE_LOG")"
grep -q '^ps ' "$FAKE_DOCKER_LOG" && grep -q '^exec 3f2a9c cat /home/frappe/frappe-bench/sites/.ext-request.json' "$FAKE_DOCKER_LOG" && ok "docker ps + docker exec" || bad "docker calls: $(cat "$FAKE_DOCKER_LOG")"

step "the probe takes the service's backend, never a one-off 'up.sh run backend' container of the project (listed first)"
request install
rc=0; out="$(run_agent)" || rc=$?
grep -q -- '--filter label=com.docker.compose.oneoff=False' "$FAKE_DOCKER_LOG" && ok "docker ps filters oneoff=False" || bad "ps call: $(grep '^ps' "$FAKE_DOCKER_LOG")"
[ "$rc" = 0 ] && grep -q '^exec 3f2a9c cat' "$FAKE_DOCKER_LOG" && ! grep -q '^exec 9e0ff1' "$FAKE_DOCKER_LOG" && grep -q '^restart backend' "$FAKE_LOG" && ok "request read from the service container and acted on" || bad "rc=$rc docker: $(cat "$FAKE_DOCKER_LOG")"

step "no backend container of this project (stack down, or COMPOSE_PROJECT_NAME elsewhere) → exit 0 quietly"
request install
rc=0; out="$(FAKE_PROJECT=gunstore-demo run_agent)" || rc=$?
[ "$rc" = 0 ] && [ ! -s "$FAKE_LOG" ] && ok "nothing done" || bad "rc=$rc calls: $(cat "$FAKE_LOG")"
rc=0; out="$(FAKE_PROJECT=gunstore-demo COMPOSE_PROJECT_NAME=gunstore-demo run_agent)" || rc=$?
[ "$rc" = 0 ] && grep -q '^restart backend' "$FAKE_LOG" && ok "COMPOSE_PROJECT_NAME selects the stack, as up.sh does" || bad "rc=$rc calls: $(cat "$FAKE_LOG")"

step "a valid install request → workers restart, backend last, replay awaited, finish_install run, request gone"
request install; rm -f "$T/bench/.ext-replay.done"
rc=0; out="$(run_agent)" || rc=$?
[ "$rc" = 0 ] && ok "exit 0" || bad "rc=$rc: $out"
[ "$(grep -c '^restart' "$FAKE_LOG" || true)" = 3 ] && ok "three restart batches (workers, backend, frontend)" || bad "restart calls: $(grep restart "$FAKE_LOG")"
[ "$(sed -n '/^restart/p' "$FAKE_LOG" | tr '\n' '|')" = "restart websocket queue-short queue-long scheduler|restart backend|restart frontend|" ] && ok "workers → backend → frontend" || bad "order: $(grep restart "$FAKE_LOG")"
grep -q 'bench --site pos.example.com execute ffl_core.extensions.sideload.finish_install --args \["acme_layaway"\]$' "$FAKE_LOG" && ok "finish_install for the app, on the requested site" || bad "no finish_install: $(cat "$FAKE_LOG")"
[ "$(cat "$T/bench/finish_install.args")" = '["acme_layaway"]' ] && ok "frappe's eval sees one argument, the id" || bad "args: $(cat "$T/bench/finish_install.args")"
[ ! -f "$T/bench/sites/.ext-request.json" ] && [ -f "$T/bench/sites/.ext-request.done" ] && ok "request consumed, done written" || bad "markers wrong"
grep -q migrate "$FAKE_LOG" && bad "install ran migrate" || ok "install does not migrate"

step "an opt-in service (queue-catalog, compose profile) is restarted with the workers only when it runs"
request install; rm -f "$T/bench/.ext-replay.done"
rc=0; out="$(FAKE_RUNNING="backend queue-catalog" run_agent)" || rc=$?
[ "$rc" = 0 ] && [ "$(sed -n '/^restart/p' "$FAKE_LOG" | head -1)" = "restart websocket queue-short queue-long scheduler queue-catalog" ] && ok "running queue-catalog joins the worker batch" || bad "rc=$rc order: $(grep restart "$FAKE_LOG")"
grep -q '^ps --services --status running$' "$FAKE_LOG" && ok "the running list comes from up.sh ps" || bad "no ps: $(cat "$FAKE_LOG")"

step "a refused request (bad app id) → done says so, request removed, exit 1"
request install ../evil
out="$(run_agent 2>&1)" && bad "should exit non-zero" || ok "exit non-zero"
grep -q "refused.*app='../evil'" "$T/bench/sites/.ext-request.done" && python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$T/bench/sites/.ext-request.done" && ok "done carries the refusal (valid JSON)" || bad "done: $(cat "$T/bench/sites/.ext-request.done")"
[ ! -f "$T/bench/sites/.ext-request.json" ] && ok "request removed (no loop)" || bad "request still there"
{ grep -q restart "$FAKE_LOG" && bad "restarted on a refused request"; } || ok "no restart"

step "an unknown action → refused like a bad id: no restart, done says so, request removed"
request uninstall
out="$(run_agent 2>&1)" && bad "should exit non-zero" || ok "exit non-zero"
grep -q "refused.*action='uninstall'" "$T/bench/sites/.ext-request.done" && ok "done names the action" || bad "done: $(cat "$T/bench/sites/.ext-request.done")"
[ ! -f "$T/bench/sites/.ext-request.json" ] && ok "request removed" || bad "request still there"
{ grep -q restart "$FAKE_LOG" && bad "restarted on an unknown action"; } || ok "no restart"

step "install: replay never finishes → done says so, exit 1"
request install; rm -f "$T/bench/.ext-replay.done"
out="$(FAKE_NO_REPLAY=1 REPLAY_WAIT=1 run_agent 2>&1)" && bad "should exit non-zero" || ok "exit non-zero"
grep -q "never finished" "$T/bench/sites/.ext-request.done" && ok "done names the replay wait" || bad "done: $(cat "$T/bench/sites/.ext-request.done")"

step "install: a restart fails → done says which, request consumed, nothing run on the site, exit 1"
request install
out="$(FAKE_RESTART_FAILS=backend run_agent 2>&1)" && bad "should exit non-zero" || ok "exit non-zero"
grep -q 'restart of backend failed' "$T/bench/sites/.ext-request.done" && [ ! -f "$T/bench/sites/.ext-request.json" ] && ok "done names the restart, request removed" || bad "done: $(cat "$T/bench/sites/.ext-request.done")"
{ grep -q 'execute\|restart frontend' "$FAKE_LOG" && bad "carried on after the failed restart: $(cat "$FAKE_LOG")"; } || ok "no frontend restart, no finish_install"

step "finish_install fails → done says so (finish_install itself writes it in real life; here the shim exits 1)"
request install
out="$(FAKE_EXEC_FAILS=finish_install run_agent 2>&1)" && bad "should exit non-zero" || ok "exit non-zero"
grep -q "finish_install failed (bench execute exit 1)" "$T/bench/sites/.ext-request.done" && ok "done carries the reason" || bad "done: $(cat "$T/bench/sites/.ext-request.done")"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["app"]=="acme_layaway" and d["ok"] is False and d["finished_at"]' "$T/bench/sites/.ext-request.done" && ok "done is mark_done-shaped JSON (app, ok, message, finished_at)" || bad "done shape"

step "a message with quotes and backslashes survives (no string interpolation into code)"
request install
nasty=$'it\'s \\ broken "here" \'\'\' done'
out="$(FAKE_EXEC_FAILS=finish_install FAKE_BOOM="$nasty" run_agent 2>&1)" || true
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert "broken" in d["message"]' "$T/bench/sites/.ext-request.done" && ok "still valid JSON with the message intact" || bad "done not JSON: $(cat "$T/bench/sites/.ext-request.done")"

step "upgrade → same restarts, replay awaited, then begin_upgrade → migrate → finish_upgrade(id, 1, \"\")"
request upgrade; rm -f "$T/bench/.ext-replay.done"
rc=0; out="$(run_agent 2>&1)" || rc=$?
[ "$rc" = 0 ] && ok "exit 0" || bad "rc=$rc: $out"
[ "$(sed -n '/^restart/p' "$FAKE_LOG" | tr '\n' '|')" = "restart websocket queue-short queue-long scheduler|restart backend|restart frontend|" ] && ok "restart order as install" || bad "order: $(grep restart "$FAKE_LOG")"
fe="$(line_of '^restart frontend')"; wait_line="$(line_of 'test -f /home/frappe/frappe-bench/.ext-replay.done')"; be="$(line_of 'sideload.begin_upgrade')"; mg="$(line_of 'bench --site pos.example.com migrate')"; fu="$(line_of 'sideload.finish_upgrade')"
[ -n "$wait_line" ] && [ -n "$fe" ] && [ -n "$be" ] && [ -n "$mg" ] && [ -n "$fu" ] && [ "$wait_line" -lt "$be" ] && [ "$fe" -lt "$be" ] && [ "$be" -lt "$mg" ] && [ "$mg" -lt "$fu" ] && ok "replay marker → begin_upgrade → migrate → finish_upgrade" || bad "sequence: $(cat "$FAKE_LOG")"
[ "$(cat "$T/bench/begin_upgrade.args")" = '["acme_layaway"]' ] && ok "begin_upgrade gets the id" || bad "begin args: $(cat "$T/bench/begin_upgrade.args" 2>/dev/null)"
[ "$(cat "$T/bench/finish_upgrade.args")" = '["acme_layaway", 1, ""]' ] && ok "finish_upgrade gets (id, 1, \"\") after frappe's eval" || bad "finish args: $(cat "$T/bench/finish_upgrade.args" 2>/dev/null)"
[ ! -f "$T/bench/sites/.ext-request.json" ] && [ "$(done_json ok)" = True ] && ok "request consumed, done ok" || bad "markers wrong"

step "upgrade whose migrate fails with quotes, backslashes and newlines → finish_upgrade(id, 0, exception line first, then the 4 lines before it), exit 1"
request upgrade
nasty=$'Migrating pos.example.com\nExecuting ext_hello.patches.v0_2.remember_greeting in pos.example.com\n\nTraceback (most recent call last):\n  File "patch.py", line 3, in execute\n    frappe.db.sql("update tabX set osa_x = \'\\\\\'")\npymysql.err.OperationalError: (1054, "Unknown column \'osa_x\' in \'field list\'") \\ C:\\path \'\'\' """\n\n'
out="$(FAKE_MIGRATE_FAILS=1 FAKE_BOOM="$nasty" run_agent 2>&1)" && bad "should exit non-zero" || ok "exit non-zero"
python3 - "$T/bench/finish_upgrade.args" <<'PY' && ok "frappe's eval yields [id, 0, the exception line, then the 4 non-empty lines before it, verbatim]" || bad "finish args: $(cat "$T/bench/finish_upgrade.args" 2>/dev/null)"
import json, sys
args = json.load(open(sys.argv[1]))
assert args[:2] == ["acme_layaway", 0], args
assert args[2].split("\n") == [
	'pymysql.err.OperationalError: (1054, "Unknown column \'osa_x\' in \'field list\'") \\ C:\\path \'\'\' """',
	"Executing ext_hello.patches.v0_2.remember_greeting in pos.example.com",
	"Traceback (most recent call last):",
	'  File "patch.py", line 3, in execute',
	'    frappe.db.sql("update tabX set osa_x = \'\\\\\'")',
], args[2]
PY
[ ! -f "$T/bench/sites/.ext-request.json" ] && [ "$(done_json ok)" = False ] && ok "done written (not ok), request consumed" || bad "markers: $(cat "$T/bench/sites/.ext-request.done")"

step "a migrate failure with an astral character and a non-UTF-8 byte → finish_upgrade stores it (no lone surrogates after frappe's eval)"
request upgrade
out="$(FAKE_MIGRATE_FAILS=1 FAKE_BOOM=$'ValueError: bad name \xf0\x9f\x98\x80 in row \xff 7' run_agent 2>&1)" && bad "should exit non-zero" || ok "exit non-zero"
args_are finish_upgrade 'a == ["acme_layaway", 0, "ValueError: bad name \U0001F600 in row ? 7"]' && ok "one code point for the emoji, ? for the byte, encodable" || bad "finish args: $(cat "$T/bench/finish_upgrade.args" 2>/dev/null)"
[ "$(done_json message)" = finish_upgrade ] && ok "finish_upgrade itself recorded it (the agent did not have to)" || bad "done: $(cat "$T/bench/sites/.ext-request.done")"

step "upgrade: begin_upgrade cannot run → no migrate; finish_upgrade(id, 0, why) records it on the site, request removed, exit 1"
request upgrade
out="$(FAKE_EXEC_FAILS=begin_upgrade run_agent 2>&1)" && bad "should exit non-zero" || ok "exit non-zero"
grep -q migrate "$FAKE_LOG" && bad "migrated without begin_upgrade" || ok "no migrate"
args_are finish_upgrade 'a[:2] == ["acme_layaway", 0] and a[2].startswith("begin_upgrade failed (bench execute exit 1)")' && ok "finish_upgrade(id, 0, \"begin_upgrade failed …\")" || bad "finish args: $(cat "$T/bench/finish_upgrade.args" 2>/dev/null)"
[ "$(done_json message)" = finish_upgrade ] && [ "$(done_json ok)" = False ] && [ ! -f "$T/bench/sites/.ext-request.json" ] && ok "done from finish_upgrade (not ok), request removed" || bad "done: $(cat "$T/bench/sites/.ext-request.done")"

step "upgrade: begin_upgrade and finish_upgrade both cannot run → the agent writes done itself, naming both (K4 fallback)"
request upgrade
out="$(FAKE_EXEC_FAILS='begin_upgrade finish_upgrade' run_agent 2>&1)" && bad "should exit non-zero" || ok "exit non-zero"
grep -q 'begin_upgrade failed (bench execute exit 1).*finish_upgrade could not record it' "$T/bench/sites/.ext-request.done" && [ ! -f "$T/bench/sites/.ext-request.json" ] && ok "done names both, request removed" || bad "done: $(cat "$T/bench/sites/.ext-request.done")"

step "upgrade: a restart fails → finish_upgrade(id, 0, \"restart of … failed\") before anything else runs on the site, request removed, exit 1"
request upgrade
out="$(FAKE_RESTART_FAILS=queue-long run_agent 2>&1)" && bad "should exit non-zero" || ok "exit non-zero"
args_are finish_upgrade 'a == ["acme_layaway", 0, "restart of websocket queue-short queue-long scheduler failed"]' && ok "finish_upgrade(id, 0, the failed restart)" || bad "finish args: $(cat "$T/bench/finish_upgrade.args" 2>/dev/null)"
{ grep -q 'begin_upgrade\|migrate\|restart backend' "$FAKE_LOG" && bad "carried on: $(cat "$FAKE_LOG")"; } || ok "no backend restart, begin_upgrade or migrate"
[ "$(done_json message)" = finish_upgrade ] && [ ! -f "$T/bench/sites/.ext-request.json" ] && ok "done from finish_upgrade, request removed" || bad "done: $(cat "$T/bench/sites/.ext-request.done")"

step "upgrade: replay never finishes → finish_upgrade(id, 0, \"backend never finished …\"), no begin_upgrade / migrate, exit 1"
request upgrade; rm -f "$T/bench/.ext-replay.done"
out="$(FAKE_NO_REPLAY=1 REPLAY_WAIT=1 run_agent 2>&1)" && bad "should exit non-zero" || ok "exit non-zero"
args_are finish_upgrade 'a[:2] == ["acme_layaway", 0] and a[2].startswith("backend never finished its extension replay (5 s)")' && ok "finish_upgrade(id, 0, the replay wait)" || bad "finish args: $(cat "$T/bench/finish_upgrade.args" 2>/dev/null)"
{ grep -q 'begin_upgrade\|migrate' "$FAKE_LOG" && bad "carried on: $(cat "$FAKE_LOG")"; } || ok "no begin_upgrade, no migrate"
[ "$(done_json message)" = finish_upgrade ] && [ ! -f "$T/bench/sites/.ext-request.json" ] && ok "done from finish_upgrade, request removed" || bad "done: $(cat "$T/bench/sites/.ext-request.done")"

step "finish_upgrade (after the migrate) cannot run → the agent writes done itself (names the migrate outcome), asks only once, request removed, exit 1"
request upgrade
out="$(FAKE_EXEC_FAILS=finish_upgrade run_agent 2>&1)" && bad "should exit non-zero" || ok "exit non-zero"
done_json message | grep -q "^finish_upgrade failed (bench execute exit 1; migrate succeeded — the database is updated, only finishing the upgrade was not recorded (docs/self-hosting.md#extensions)): " && [ ! -f "$T/bench/sites/.ext-request.json" ] && ok "done carries the reason, request removed" || bad "done: $(cat "$T/bench/sites/.ext-request.done")"
[ "$(grep -c 'sideload.finish_upgrade' "$FAKE_LOG")" = 1 ] && ok "finish_upgrade tried once" || bad "finish_upgrade calls: $(grep -c 'sideload.finish_upgrade' "$FAKE_LOG")"

step "the verdict is bench's exit status, never its text (bench execute reports a raising function as \"name '…' is not defined\", exit 1)"
request upgrade
rc=0; out="$(FAKE_EXEC_TEXT="NameError: name 'ffl_core' is not defined" run_agent 2>&1)" || rc=$?
[ "$rc" = 0 ] && grep -q 'bench --site pos.example.com migrate' "$FAKE_LOG" && grep -q 'sideload.finish_upgrade' "$FAKE_LOG" && ok "exit 0 with error-looking text → carried on (migrate, finish_upgrade)" || bad "rc=$rc calls: $(cat "$FAKE_LOG")"
request upgrade
out="$(FAKE_EXEC_FAILS=begin_upgrade FAKE_BOOM='{"ok": true}' run_agent 2>&1)" && bad "exit 1 with ok-looking text should fail" || ok "exit 1 with {\"ok\": true} text → failure"
grep -q migrate "$FAKE_LOG" && bad "migrated after a failed begin_upgrade" || ok "no migrate"

step "a finish step that returns without clearing the request → the agent clears it (no restart every 20 s)"
request upgrade
out="$(FAKE_KEEP_REQUEST=1 run_agent 2>&1)" && bad "should exit non-zero" || ok "exit non-zero"
[ ! -f "$T/bench/sites/.ext-request.json" ] && grep -q "without clearing the request" "$T/bench/sites/.ext-request.done" && ok "request removed, done says why" || bad "request left: $(ls "$T/bench/sites")"

step "a migrate that outlives the 30-minute sweep: another extension's request, written meanwhile, is left for the next run — the finish's done kept, exit 0"
request upgrade
rc=0; out="$(FAKE_MIGRATE_WRITES_REQUEST=acme_other run_agent 2>&1)" || rc=$?
[ "$rc" = 0 ] && ok "exit 0: this upgrade finished" || bad "rc=$rc: $out"
grep -q '"app": "acme_other"' "$T/bench/sites/.ext-request.json" 2>/dev/null && ok "acme_other's request is still there" || bad "request: $(cat "$T/bench/sites/.ext-request.json" 2>/dev/null || echo removed)"
[ "$(done_json app)" = acme_layaway ] && [ "$(done_json message)" = finish_upgrade ] && [ "$(done_json ok)" = True ] && ok "done is finish_upgrade's, not overwritten by the agent" || bad "done: $(cat "$T/bench/sites/.ext-request.done")"

step "…and when its finish_upgrade cannot run there: the agent writes done for this app, the other extension's request stays"
request upgrade
out="$(FAKE_MIGRATE_WRITES_REQUEST=acme_other FAKE_EXEC_FAILS=finish_upgrade run_agent 2>&1)" && bad "should exit non-zero" || ok "exit non-zero"
grep -q '"app": "acme_other"' "$T/bench/sites/.ext-request.json" 2>/dev/null && ok "acme_other's request is still there" || bad "request: $(cat "$T/bench/sites/.ext-request.json" 2>/dev/null || echo removed)"
[ "$(done_json app)" = acme_layaway ] && done_json message | grep -q '^finish_upgrade failed (bench execute exit 1' && ok "done names this app's failure" || bad "done: $(cat "$T/bench/sites/.ext-request.done")"

step "an agent step fails after the sweep freed the pipeline and another extension wrote its request: finish_upgrade recorded it, so the agent keeps its done and exits 1 (review-pipeline r1 nit, ext-agent.sh fail())"
request upgrade
out="$(FAKE_RESTART_WRITES_REQUEST=acme_other FAKE_RESTART_FAILS=backend run_agent 2>&1)" && bad "should exit non-zero" || ok "exit non-zero"
args_are finish_upgrade 'a == ["acme_layaway", 0, "restart of backend failed"]' && ok "finish_upgrade(id, 0, the failed restart)" || bad "finish args: $(cat "$T/bench/finish_upgrade.args" 2>/dev/null)"
[ "$(done_json app)" = acme_layaway ] && [ "$(done_json message)" = finish_upgrade ] && ok "done is finish_upgrade's, not overwritten with \"could not record it on the site either\"" || bad "done: $(cat "$T/bench/sites/.ext-request.done")"
grep -q '"app": "acme_other"' "$T/bench/sites/.ext-request.json" 2>/dev/null && ok "acme_other's request is still there" || bad "request: $(cat "$T/bench/sites/.ext-request.json" 2>/dev/null || echo removed)"

step "…while a finish_upgrade that leaves this app's own request behind is not taken as recorded: the agent writes done and removes that request (no restart every 20 s)"
request upgrade
out="$(FAKE_KEEP_REQUEST=1 FAKE_RESTART_FAILS=backend run_agent 2>&1)" && bad "should exit non-zero" || ok "exit non-zero"
[ ! -f "$T/bench/sites/.ext-request.json" ] && grep -q 'restart of backend failed (finish_upgrade could not record it on the site either)' "$T/bench/sites/.ext-request.done" && ok "request removed, done says finish_upgrade did not record it" || bad "request: $(cat "$T/bench/sites/.ext-request.json" 2>/dev/null || echo removed) done: $(cat "$T/bench/sites/.ext-request.done")"

echo; echo "ext-agent: $pass passed, $fail failed"
[ "$fail" = 0 ]
