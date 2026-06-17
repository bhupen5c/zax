#!/usr/bin/env bash
# Zax full smoke test: every API endpoint + security hardening checks.
# Usage: ./scripts/smoke_test.sh   (server must be running on :8777)
set -u
BASE=http://127.0.0.1:8777
PASS=0; FAIL=0

check() { # name, expected, actual
  if [ "$2" = "$3" ]; then PASS=$((PASS+1)); echo "  ✓ $1";
  else FAIL=$((FAIL+1)); echo "  ✗ $1  (expected $2, got $3)"; fi
}
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
json() { curl -s "$@"; }

echo "== core =="
check "GET /                          200" 200 "$(code $BASE/)"
check "GET /static/app.js             200" 200 "$(code $BASE/static/app.js)"
check "GET /api/status                200" 200 "$(code $BASE/api/status)"
check "GET /api/greeting              200" 200 "$(code $BASE/api/greeting)"
check "GET /api/messages              200" 200 "$(code $BASE/api/messages)"
check "GET /api/feed                  200" 200 "$(code "$BASE/api/feed?after=0")"

echo "== chat =="
check "POST /api/chat                 200" 200 "$(code -X POST $BASE/api/chat -H 'Content-Type: application/json' -d '{"message":"status report"}')"
check "POST /api/chat empty msg       422" 422 "$(code -X POST $BASE/api/chat -H 'Content-Type: application/json' -d '{"message":""}')"

echo "== org =="
check "GET /api/agents                200" 200 "$(code $BASE/api/agents)"
HIRE=$(json -X POST $BASE/api/hire -H 'Content-Type: application/json' -d '{"role":"smoke testing"}')
AID=$(echo "$HIRE" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
check "POST /api/hire                 has id" "yes" "$([ -n "$AID" ] && echo yes)"
check "POST fire bad id               404" 404 "$(code -X POST $BASE/api/agents/nonexistent/fire -H 'Content-Type: application/json' -d '{"reason":"x"}')"
check "POST fire real agent           200" 200 "$(code -X POST $BASE/api/agents/$AID/fire -H 'Content-Type: application/json' -d '{"reason":"smoke test cleanup"}')"
check "POST fire same agent again     404" 404 "$(code -X POST $BASE/api/agents/$AID/fire -H 'Content-Type: application/json' -d '{"reason":"x"}')"

echo "== tasks =="
check "GET /api/tasks                 200" 200 "$(code $BASE/api/tasks)"
check "POST /api/tasks                200" 200 "$(code -X POST $BASE/api/tasks -H 'Content-Type: application/json' -d '{"title":"smoke task","priority":2}')"
check "POST task bad priority         422" 422 "$(code -X POST $BASE/api/tasks -H 'Content-Type: application/json' -d '{"title":"x","priority":9}')"
check "POST task no title             422" 422 "$(code -X POST $BASE/api/tasks -H 'Content-Type: application/json' -d '{"priority":1}')"

echo "== routines =="
RID=$(json -X POST $BASE/api/routines -H 'Content-Type: application/json' -d '{"name":"smoke routine","description":"x","interval_minutes":60}' | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
check "POST /api/routines             has id" "yes" "$([ -n "$RID" ] && echo yes)"
check "POST routine interval<5        422" 422 "$(code -X POST $BASE/api/routines -H 'Content-Type: application/json' -d '{"name":"x","interval_minutes":1}')"
check "DELETE /api/routines/{id}      200" 200 "$(code -X DELETE $BASE/api/routines/$RID)"

echo "== memory & learning =="
check "GET /api/memory                200" 200 "$(code $BASE/api/memory)"
check "GET /api/memory?q=...          200" 200 "$(code "$BASE/api/memory?q=research&kind=skill")"
check "GET /api/learning/status       200" 200 "$(code $BASE/api/learning/status)"
check "POST /api/learning/reflect     200" 200 "$(code -X POST $BASE/api/learning/reflect -H 'Content-Type: application/json' -d '{}')"
check "DELETE /api/memory/999999      200" 200 "$(code -X DELETE $BASE/api/memory/999999)"

echo "== knowledge graph (graphify) =="
check "GET /api/graph                200" 200 "$(code $BASE/api/graph)"
check "GET /api/graph/stats          200" 200 "$(code $BASE/api/graph/stats)"
check "graph engine available        true" true "$(json $BASE/api/graph/stats | python3 -c "import json,sys; print(str(json.load(sys.stdin)['available']).lower())")"
check "graph node_link shape         ok" ok "$(json $BASE/api/graph | python3 -c "import json,sys; d=json.load(sys.stdin); print('ok' if 'nodes' in d and 'links' in d else 'no')")"
check "GET /api/graph/query          200" 200 "$(code "$BASE/api/graph/query?q=Skyforge")"
check "graph query empty             400" 400 "$(code "$BASE/api/graph/query?q=")"
check "DELETE missing node           200" 200 "$(code -X DELETE $BASE/api/graph/node/no-such-node)"

echo "== providers =="
check "GET /api/providers             200" 200 "$(code $BASE/api/providers)"
check "POST select unknown            404" 404 "$(code -X POST $BASE/api/providers/select -H 'Content-Type: application/json' -d '{"provider":"nope"}')"
check "POST select mock               200" 200 "$(code -X POST $BASE/api/providers/select -H 'Content-Type: application/json' -d '{"provider":"mock"}')"
check "POST configure model           200" 200 "$(code -X POST $BASE/api/providers/configure -H 'Content-Type: application/json' -d '{"provider":"groq","model":"llama-3.3-70b-versatile"}')"
check "POST test mock ok=true         true" true "$(json -X POST $BASE/api/providers/test -H 'Content-Type: application/json' -d '{"provider":"mock"}' | python3 -c "import json,sys; print(str(json.load(sys.stdin)['ok']).lower())")"
check "POST select claude-cli back    200" 200 "$(code -X POST $BASE/api/providers/select -H 'Content-Type: application/json' -d '{"provider":"claude-cli"}')"

echo "== voice =="
check "POST /api/voice/speak          200" 200 "$(code -X POST $BASE/api/voice/speak -H 'Content-Type: application/json' -d '{"text":"audit check"}')"
check "POST speak empty               422" 422 "$(code -X POST $BASE/api/voice/speak -H 'Content-Type: application/json' -d '{"text":""}')"

echo "== skills =="
check "GET /api/skills              200" 200 "$(code $BASE/api/skills)"
check "skills catalog has packs     ok" ok "$(json $BASE/api/skills | python3 -c "import json,sys; d=json.load(sys.stdin); n=sum(len(v) for v in d.values()); print('ok' if n>=16 else 'no')")"
check "POST hire skill              200" 200 "$(code -X POST $BASE/api/skills/hire -H 'Content-Type: application/json' -d '{"skill":"designer"}')"
check "POST hire unknown skill      404" 404 "$(code -X POST $BASE/api/skills/hire -H 'Content-Type: application/json' -d '{"skill":"nope"}')"

echo "== chat sessions =="
check "GET /api/sessions            200" 200 "$(code $BASE/api/sessions)"
SID=$(json -X POST $BASE/api/sessions -H 'Content-Type: application/json' -d '{}' | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
check "POST /api/sessions           has id" "yes" "$([ -n "$SID" ] && echo yes)"
CHATBODY=$(python3 -c "import json,sys; print(json.dumps({'message':'hi','session_id':sys.argv[1]}))" "$SID")
check "POST chat to session         200" 200 "$(code -X POST $BASE/api/chat -H 'Content-Type: application/json' -d "$CHATBODY")"
check "GET messages?session         200" 200 "$(code "$BASE/api/messages?session=$SID")"
check "POST rename session          200" 200 "$(code -X POST $BASE/api/sessions/$SID/rename -H 'Content-Type: application/json' -d '{"title":"renamed"}')"
check "DELETE session               200" 200 "$(code -X DELETE $BASE/api/sessions/$SID)"
check "DELETE main session blocked  400" 400 "$(code -X DELETE $BASE/api/sessions/main)"

echo "== telegram =="
check "GET /api/telegram            200" 200 "$(code $BASE/api/telegram)"
check "POST bad token rejected      400" 400 "$(code -X POST $BASE/api/telegram/connect -H 'Content-Type: application/json' -d '{"token":"000000:invalidinvalidinvalidinvalid"}')"
check "POST notify toggle           200" 200 "$(code -X POST $BASE/api/telegram/notify -H 'Content-Type: application/json' -d '{"enabled":true}')"

echo "== security hardening =="
check "POST text/plain blocked        415" 415 "$(code -X POST $BASE/api/chat -H 'Content-Type: text/plain' -d '{"message":"csrf"}')"
check "POST no content-type blocked   415" 415 "$(code -X POST $BASE/api/chat -d '{"message":"csrf"}' -H 'Content-Type:')"
check "Bad Host header blocked        400" 400 "$(code $BASE/api/status -H 'Host: evil.example.com')"
check "GET with good host ok          200" 200 "$(code $BASE/api/status -H 'Host: localhost:8777')"

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
