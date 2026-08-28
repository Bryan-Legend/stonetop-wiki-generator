#!/usr/bin/env bash
# End-to-end check of the sync worker against `wrangler dev --local`.
#
#   cd sync-worker && npx wrangler dev --local --port 8788   # in one shell
#   bash test/worker.sh                                      # in another
#
# Campaign creation is rate limited to 5/hour per address, and the check for
# that spends the whole allowance — so it only runs when asked for:
#   SYNC_TEST_RATELIMIT=1 bash test/worker.sh
set -u
# Defaults to the local `wrangler dev`. Point it at the deployed Worker to
# check a fresh deploy: SYNC_ENDPOINT=https://sync.stonetop-wiki.workers.dev
B=${SYNC_ENDPOINT:-http://127.0.0.1:8788}
pass=0; fail=0
ok()  { echo "  PASS  $1"; pass=$((pass+1)); }
bad() { echo "  FAIL  $1"; echo "        $2"; fail=$((fail+1)); }
chk() { # name expected actual
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected [$2] got [$3]"; fi
}

echo "== create campaign =="
CREATE=$(curl -s -X POST "$B/v1/campaigns")
CID=$(echo "$CREATE" | python -c 'import sys,json;print(json.load(sys.stdin)["campaign"])')
PTOK=$(echo "$CREATE" | python -c 'import sys,json;print(json.load(sys.stdin)["player_token"])')
GTOK=$(echo "$CREATE" | python -c 'import sys,json;print(json.load(sys.stdin)["gm_token"])')
[ -n "$CID" ] && ok "campaign created: $CID" || bad "campaign created" "$CREATE"
case "$CID" in stonetop-????????????????) ok "campaign id shape (64 unguessable bits)" ;; *) bad "campaign id shape" "$CID" ;; esac
[ "${#GTOK}" -ge 32 ] && ok "gm token is long (${#GTOK} chars)" || bad "gm token length" "${#GTOK}"
[ "$GTOK" != "$PTOK" ] && ok "tokens differ" || bad "tokens differ" "same"

echo "== auth =="
chk "bad token is 404" "404" "$(curl -s -o /dev/null -w '%{http_code}' -H "authorization: Bearer nope" "$B/v1/state/$CID")"
chk "no such campaign is 404" "404" "$(curl -s -o /dev/null -w '%{http_code}' -H "authorization: Bearer $GTOK" "$B/v1/state/stonetop-deadbeefdeadbeef")"
chk "no token is 404" "404" "$(curl -s -o /dev/null -w '%{http_code}' "$B/v1/state/$CID")"

echo "== empty campaign =="
chk "fresh pull is 304" "304" "$(curl -s -o /dev/null -w '%{http_code}' -H "authorization: Bearer $PTOK" "$B/v1/state/$CID?since=0")"

echo "== player writes a shared store =="
P1=$(curl -s -X POST "$B/v1/state/$CID" -H "authorization: Bearer $PTOK" -H 'content-type: application/json' \
  -d '{"patches":[{"store":"stonetop-wiki-checks","set":{"marshedge#fire-2":true},"del":[]}]}')
chk "player patch applied" "1" "$(echo "$P1" | python -c 'import sys,json;print(json.load(sys.stdin)["applied"])')"
CUR1=$(echo "$P1" | python -c 'import sys,json;print(json.load(sys.stdin)["cursor"])')
chk "cursor advanced" "1" "$CUR1"

echo "== a second player, concurrent, different key =="
P2=$(curl -s -X POST "$B/v1/state/$CID" -H "authorization: Bearer $PTOK" -H 'content-type: application/json' \
  -d '{"patches":[{"store":"stonetop-wiki-checks","set":{"stonetop#granary-1":true},"del":[]}]}')
GET=$(curl -s -H "authorization: Bearer $PTOK" "$B/v1/state/$CID?since=0")
BOTH=$(echo "$GET" | python -c '
import sys,json
rows = json.load(sys.stdin)["rows"]
print(",".join(sorted(r["k"] for r in rows)))')
chk "both edits survive" "marshedge#fire-2,stonetop#granary-1" "$BOTH"

echo "== gm scope =="
GHP=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$B/v1/state/$CID" -H "authorization: Bearer $PTOK" -H 'content-type: application/json' \
  -d '{"patches":[{"store":"underfalls-hp","set":{"ogre":3},"del":[]}]}')
chk "player writing gm store is 403" "403" "$GHP"
GHP2=$(curl -s -X POST "$B/v1/state/$CID" -H "authorization: Bearer $GTOK" -H 'content-type: application/json' \
  -d '{"patches":[{"store":"underfalls-hp","set":{"ogre":3},"del":[]}]}')
chk "gm writing gm store applies" "1" "$(echo "$GHP2" | python -c 'import sys,json;print(json.load(sys.stdin)["applied"])')"

PLAYER_SEES=$(curl -s -H "authorization: Bearer $PTOK" "$B/v1/state/$CID?since=0" | python -c '
import sys,json
print(",".join(sorted({r["store"] for r in json.load(sys.stdin)["rows"]})))')
chk "player never sees gm rows" "stonetop-wiki-checks" "$PLAYER_SEES"
GM_SEES=$(curl -s -H "authorization: Bearer $GTOK" "$B/v1/state/$CID?since=0" | python -c '
import sys,json
print(",".join(sorted({r["store"] for r in json.load(sys.stdin)["rows"]})))')
chk "gm sees both scopes" "stonetop-wiki-checks,underfalls-hp" "$GM_SEES"

echo "== incremental pull =="
CUR=$(curl -s -H "authorization: Bearer $GTOK" "$B/v1/state/$CID?since=0" | python -c 'import sys,json;print(json.load(sys.stdin)["cursor"])')
chk "nothing new after catching up" "304" "$(curl -s -o /dev/null -w '%{http_code}' -H "authorization: Bearer $GTOK" "$B/v1/state/$CID?since=$CUR")"
curl -s -o /dev/null -X POST "$B/v1/state/$CID" -H "authorization: Bearer $GTOK" -H 'content-type: application/json' \
  -d '{"patches":[{"store":"stonetop-wiki-map-pins","set":{"campaign":[{"x":0.5,"y":0.5,"color":"#e2534a","label":"here"}]},"del":[]}]}'
DELTA=$(curl -s -H "authorization: Bearer $GTOK" "$B/v1/state/$CID?since=$CUR" | python -c '
import sys,json
rows = json.load(sys.stdin)["rows"]
print(len(rows), rows[0]["store"] if rows else "")')
chk "only the new row comes back" "1 stonetop-wiki-map-pins" "$DELTA"

echo "== etag =="
ET=$(curl -s -D - -o /dev/null -H "authorization: Bearer $GTOK" "$B/v1/state/$CID?since=0" | tr -d '\r' | grep -i '^etag:' | cut -d' ' -f2-)
[ -n "$ET" ] && ok "etag returned: $ET" || bad "etag returned" "none"
chk "If-None-Match gives 304" "304" "$(curl -s -o /dev/null -w '%{http_code}' -H "authorization: Bearer $GTOK" -H "if-none-match: $ET" "$B/v1/state/$CID?since=0")"

echo "== tombstones =="
curl -s -o /dev/null -X POST "$B/v1/state/$CID" -H "authorization: Bearer $PTOK" -H 'content-type: application/json' \
  -d '{"patches":[{"store":"stonetop-wiki-checks","set":{},"del":["marshedge#fire-2"]}]}'
TOMB=$(curl -s -H "authorization: Bearer $PTOK" "$B/v1/state/$CID?since=0" | python -c '
import sys,json
rows = json.load(sys.stdin)["rows"]
print(next((("null" if r["v"] is None else r["v"]) for r in rows if r["k"]=="marshedge#fire-2"), "missing"))')
chk "deleted key comes back as a tombstone" "null" "$TOMB"

echo "== reset =="
chk "player cannot reset" "403" "$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$B/v1/state/$CID/stonetop-wiki-checks" -H "authorization: Bearer $PTOK")"
RES=$(curl -s -X DELETE "$B/v1/state/$CID/underfalls-hp" -H "authorization: Bearer $GTOK")
chk "gm reset clears the store" "1" "$(echo "$RES" | python -c 'import sys,json;print(json.load(sys.stdin)["cleared"])')"

echo "== validation =="
# The dice-sound setting is a real key this browser holds and must never sync.
chk "a store outside the registry is refused" "400" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$B/v1/state/$CID" -H "authorization: Bearer $GTOK" -H 'content-type: application/json' -d '{"patches":[{"store":"stonetop-wiki-sound","set":{"a":1}}]}')"
# ...but the notes store, which carries answers and playbook boxes, is in it.
chk "the notes store is accepted" "200" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$B/v1/state/$CID" -H "authorization: Bearer $GTOK" -H 'content-type: application/json' -d '{"patches":[{"store":"stonetop-wiki-notes","set":{"marshedge#h1:q0":"asked"}}]}')"
chk "patches must be an array" "400" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$B/v1/state/$CID" -H "authorization: Bearer $GTOK" -H 'content-type: application/json' -d '{"patches":"nope"}')"
BIG=$(python -c 'print("{\"patches\":[{\"store\":\"stonetop-wiki-checks\",\"set\":{" + ",".join("\"k%d\":true" % i for i in range(600)) + "}}]}")')
chk "too many keys refused" "413" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$B/v1/state/$CID" -H "authorization: Bearer $GTOK" -H 'content-type: application/json' -d "$BIG")"

echo "== cors =="
chk "preflight from the hosted wiki" "204" "$(curl -s -o /dev/null -w '%{http_code}' -X OPTIONS "$B/v1/state/$CID" -H 'origin: https://stonetop-wiki.github.io' -H 'access-control-request-method: GET')"
chk "preflight from a stranger" "403" "$(curl -s -o /dev/null -w '%{http_code}' -X OPTIONS "$B/v1/state/$CID" -H 'origin: https://evil.example' -H 'access-control-request-method: GET')"
ACAO=$(curl -s -D - -o /dev/null -H "authorization: Bearer $GTOK" -H 'origin: null' "$B/v1/state/$CID?since=0" | tr -d '\r' | grep -i '^access-control-allow-origin:' | cut -d' ' -f2-)
chk "gm allowed from a file:// page" "null" "$ACAO"
chk "player refused from a file:// page" "403" "$(curl -s -o /dev/null -w '%{http_code}' -H "authorization: Bearer $PTOK" -H 'origin: null' "$B/v1/state/$CID?since=0")"
chk "stranger origin refused" "403" "$(curl -s -o /dev/null -w '%{http_code}' -H "authorization: Bearer $GTOK" -H 'origin: https://evil.example' "$B/v1/state/$CID?since=0")"

echo "== body cap =="
HUGE=$(mktemp)
python -c 'import sys;sys.stdout.write("{\"patches\":[{\"store\":\"stonetop-wiki-checks\",\"set\":{\"k\":\"" + "x"*70000 + "\"}}]}")' > "$HUGE"
chk "oversized body refused" "413" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$B/v1/state/$CID" -H "authorization: Bearer $GTOK" -H 'content-type: application/json' --data-binary @"$HUGE")"
rm -f "$HUGE"

echo "== rate limit on the one open endpoint =="
# Tripping this costs the address its hourly allowance, and the counter now
# lives in a Durable Object that expires on its own rather than a table we can
# truncate — so it is opt-in.
if [ "${SYNC_TEST_RATELIMIT:-0}" = "1" ]; then
  code=""
  for i in 1 2 3 4 5 6; do
    code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$B/v1/campaigns")
    [ "$code" = "429" ] && break
  done
  chk "campaign creation is rate limited" "429" "$code"
else
  echo "  skip  set SYNC_TEST_RATELIMIT=1 to spend this address's hourly allowance"
fi

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
