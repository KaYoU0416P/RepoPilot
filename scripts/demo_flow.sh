#!/usr/bin/env bash
# 端到端演示：GitHub Issue → 验签入队 → clone → Agent 修复 → 审批闸门 → 发布。
#
#   ./scripts/demo_flow.sh            # 用 ScriptedLLM，不花钱，约 40 秒
#   LLM=deepseek ./scripts/demo_flow.sh   # 走真模型
#
# 面试现场就跑这个。它把六件事一次演完：
#   1. webhook 验签（改一个字节 → 401，且什么都不记账）
#   2. 幂等（同一个 delivery-id 重投 → duplicate，不会开出第二个 run）
#   3. 授权边界（没打 repopilot 标签 → 忽略，但仍返回 2xx）
#   4. clone 目标仓库 + 容器沙箱闸门
#   5. ★审批闸门：开 run 的那把 key 批不了自己开的 run（403）
#   6. ★审计：请求体里冒充 "the CTO"，流水里记的还是真实身份
#
# 关于「真实性」的两点说明，面试要主动讲：
#   - 上游仓库是**本地的** fixtures/sample_repo（预先 clone 进 .repos/ 缓存，
#     origin 指向本地路径）。走的是**和真实 GitHub 完全相同的代码路径** ——
#     RepoCache.ensure() 照样 fetch + reset + clean，只是省掉了联网那几秒。
#     真连 github.com 的证据另有 `make clone-check`。
#   - 没配 GITHUB_TOKEN 时发布器是 DryRunPublisher：状态照样走到 published，
#     但 **pr_url 是空的** —— 空的 pr_url 就是「这次没真发 PR」的标记。
set -euo pipefail

cd "$(dirname "$0")/.."
PORT=${PORT:-8077}
BASE="http://localhost:$PORT"
SECRET="demo-webhook-secret"
CI_KEY="rp_demo_ci_00000000000000"
HU_KEY="rp_demo_human_0000000000"
REPO="kayou/demo-repo"
STARTED=$(date +%s)
# ★每次跑用一批新的 delivery-id。幂等台账是**持久**的 —— 固定 id 的话第二次
# 跑整场演示都会被判重（202 变 200 duplicate）。这是幂等在正常工作，
# 但会让演示只能跑一次。**可重复演示的前提是每次都用新的幂等键。**
TAG=$(date +%s)

say() { printf '\n\033[1;36m=== %s\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[31m✗ %s\033[0m\n' "$*"; FAILED=1; }
FAILED=0
# 断言 HTTP 状态码：expect actual label
is()  { [ "$1" = "$2" ] && ok "$3 → $2" || bad "$3 → 期望 $1，实际 $2"; }

cleanup() { [ -n "${SRV:-}" ] && kill "$SRV" 2>/dev/null || true; }
trap cleanup EXIT

# ---------------------------------------------------------------- 准备
say "准备：缓存目标仓库 + 起服务"
make sync >/dev/null 2>&1
docker image inspect repopilot-sandbox:py312 >/dev/null 2>&1 \
  || { echo "缺镜像，先跑 make sandbox-image"; exit 1; }

# 把 fixtures/sample_repo 变成一个"上游"，再 clone 进缓存 —— 之后 RepoCache
# 走的就是完全正常的 fetch 路径。
UPSTREAM=$(mktemp -d)/upstream.git
rm -rf ".repos/$REPO"
mkdir -p "$(dirname "$UPSTREAM")" ".repos/$(dirname "$REPO")"
git init -q --bare "$UPSTREAM"
# 裸仓库的 HEAD 默认指向 master，而我们推的是 main。不改的话 clone 会警告
# "远程 HEAD 指向一个不存在的引用"，而且 RepoCache._refresh 读 origin/HEAD 会失败。
git -C "$UPSTREAM" symbolic-ref HEAD refs/heads/main
TMPW=$(mktemp -d)
cp -R fixtures/sample_repo/. "$TMPW/"
git -C "$TMPW" init -q -b main
git -C "$TMPW" add -A
git -C "$TMPW" -c user.email=d@d -c user.name=d commit -qm seed --no-gpg-sign
git -C "$TMPW" push -q "$UPSTREAM" HEAD:main
git clone -q "$UPSTREAM" ".repos/$REPO"
ok "上游就绪，缓存已预热：.repos/$REPO"

export REPOPILOT_LLM_PROVIDER="${LLM:-scripted}"
export REPOPILOT_SANDBOX=docker                 # ★clone 来的仓库必须容器沙箱
export REPOPILOT_GITHUB_WEBHOOK_SECRET="$SECRET"
export REPOPILOT_GITHUB_REPO_ALLOWLIST="[\"$REPO\"]"
export REPOPILOT_API_KEYS="[{\"key\":\"$CI_KEY\",\"name\":\"ci-bot\",\"scopes\":[\"run\"]},{\"key\":\"$HU_KEY\",\"name\":\"kayou\",\"scopes\":[\"run\",\"approve\"]}]"

make db-up >/dev/null 2>&1

# ★起服务之前先确认 import 得到。这台机器上 uv 写的 .pth 带 UF_HIDDEN，
# CPython 会静默跳过它 —— 症状是 uvicorn 直接 ModuleNotFoundError 退出，
# 而下面那个就绪循环如果只是"轮询完就往下走"，会**顶着一个死掉的服务报就绪**。
uv run --no-sync python -c "import repopilot" \
  || { echo "↑ import repopilot 失败。多半是 .pth 的 UF_HIDDEN，跑 make sync；见 docs/failures.md"; exit 1; }

uv run --no-sync uvicorn repopilot.api.app:app --port "$PORT" >/tmp/demo_flow.log 2>&1 &
SRV=$!
READY=0
for _ in $(seq 1 40); do
  curl -sf "$BASE/health" >/dev/null 2>&1 && { READY=1; break; }
  kill -0 "$SRV" 2>/dev/null || break     # 进程已经死了，别再等满 20 秒
  sleep 0.5
done
# ★就绪检查必须能失败。上一版这里只是 `... && break`，循环跑完照样往下走，
# 于是服务其实没起来却打印了"✓ 服务就绪"，后面所有断言拿到 000。
# **一个不会失败的检查比没有检查更糟** —— 它把故障伪装成了通过。
[ "$READY" = 1 ] || { echo "服务没起来，见 /tmp/demo_flow.log:"; tail -5 /tmp/demo_flow.log; exit 1; }
ok "服务就绪 :$PORT  (provider=$REPOPILOT_LLM_PROVIDER, sandbox=docker)"

# ---------------------------------------------------------------- webhook
post_issue() { # $1=delivery  $2=labels-json  $3=tamper?
  local body sig
  body=$(python3 -c "
import json,sys
print(json.dumps({'action':'opened','repository':{'full_name':'$REPO'},
 'issue':{'number':42,'title':'Fix divide() so dividing by zero raises ValueError',
          'body':'divide(1, 0) 现在返回 None，应该抛 ValueError','labels':$2}}))")
  sig=$(SECRET="$SECRET" BODY="$body" python3 -c "
import hmac,hashlib,os
print('sha256='+hmac.new(os.environ['SECRET'].encode(),os.environ['BODY'].encode(),hashlib.sha256).hexdigest())")
  [ "${3:-}" = "tamper" ] && body="${body}"  # 签名对不上改过的体
  curl -s -o /tmp/wh.json -w '%{http_code}' -X POST "$BASE/webhooks/github" \
    -H "X-Hub-Signature-256: $sig" -H "X-GitHub-Delivery: $1" \
    -H "X-GitHub-Event: issues" -H 'content-type: application/json' \
    --data-binary "${3:-$body}"
}

say "1) webhook 验签 · 幂等 · 授权边界"
is 401 "$(post_issue "$TAG-bad" '[{"name":"repopilot"}]' '{"action":"opened"}')" "签名对不上的请求"
is 200 "$(post_issue "$TAG-nolabel" '[{"name":"bug"}]')" "没打 repopilot 标签（忽略但 2xx，否则 GitHub 会一直重投）"
is 202 "$(post_issue "$TAG-1" '[{"name":"repopilot"}]')" "合法投递"
RID=$(python3 -c "import json;d=json.load(open('/tmp/wh.json'));print(d.get('run_id') or '')")
[ -n "$RID" ] || { echo "没拿到 run_id，webhook 响应："; cat /tmp/wh.json; exit 1; }
is 200 "$(post_issue "$TAG-1" '[{"name":"repopilot"}]')" "同一个 delivery-id 重投"
[ "$(python3 -c "import json;print(json.load(open('/tmp/wh.json'))['status'])")" = duplicate ] \
  && ok "重投被判重，没有开出第二个 run" || bad "幂等失效"

# ---------------------------------------------------------------- Agent
say "2) worker 领取 → clone → 容器沙箱里跑测试"
for _ in $(seq 1 120); do
  ST=$(curl -s "$BASE/runs/$RID" -H "Authorization: Bearer $CI_KEY" \
       | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')
  [ "$ST" = pending_approval ] || [ "$ST" = failed ] && break
  sleep 1
done
grep -q "repo.ensure\|clone\|fetch" /tmp/demo_flow.log && ok "走了 RepoCache（clone/fetch）"
grep -q "sandbox.docker" /tmp/demo_flow.log && ok "测试跑在容器里（sandbox.docker）"
is pending_approval "$ST" "Agent 跑完，停在审批闸门（没有直接 published）"

# ---------------------------------------------------------------- 审批
say "3) 审批闸门 + 审计"
CODE=$(curl -s -o /tmp/a.json -w '%{http_code}' -X POST "$BASE/runs/$RID/approval" \
  -H "Authorization: Bearer $CI_KEY" -H 'content-type: application/json' \
  -d '{"decision":"approved"}')
is 403 "$CODE" "★ci-bot 批准自己开的 run"

CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/runs/$RID/approval" \
  -H "Authorization: Bearer $HU_KEY" -H 'content-type: application/json' \
  -d '{"decision":"approved","decided_by":"the CTO","reason":"diff 看过了"}')
is 200 "$CODE" "人批准（请求体里冒充 the CTO）"

WHO=$(curl -s "$BASE/runs/$RID/approvals" -H "Authorization: Bearer $CI_KEY" \
      | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["decided_by"])')
[ "$WHO" = kayou ] && ok "★审批流水记的是 \"$WHO\"，不是请求体里的 \"the CTO\"" \
                  || bad "审计被伪造成了 $WHO"

# ---------------------------------------------------------------- 发布
say "4) 发布"
for _ in $(seq 1 60); do
  FIN=$(curl -s "$BASE/runs/$RID" -H "Authorization: Bearer $CI_KEY")
  ST=$(echo "$FIN" | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')
  [ "$ST" = published ] || [ "$ST" = failed ] && break
  sleep 1
done
is published "$ST" "终态"
PR=$(echo "$FIN" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("pr_url") or "")')
[ -z "$PR" ] && ok "pr_url 为空 = 走的 DryRunPublisher，没配 token 就不假装发了 PR" \
             || ok "PR: $PR"

say "完成，用时 $(( $(date +%s) - STARTED )) 秒"
[ "$FAILED" = 0 ] && echo "全部通过 ✓" || { echo "有断言未通过 ✗"; exit 1; }
