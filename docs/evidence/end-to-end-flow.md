# 端到端链路实测（Issue → 审批 → PR）

重跑：`make demo-flow`（ScriptedLLM，不花钱）· `LLM=deepseek make demo-flow`（真模型）
生成于 2026-09-11 18:38

> 上游仓库是**本地的** `fixtures/sample_repo`（预先 clone 进 `.repos/` 缓存）。
> 走的是和真实 GitHub **完全相同的代码路径** —— `RepoCache.ensure()` 照样
> fetch + reset + clean，只是省掉联网那几秒。真连 github.com 的证据见 `make clone-check`。
>
> 没配 `GITHUB_TOKEN` 时发布器是 `DryRunPublisher`：状态照样走到 `published`，
> 但 **`pr_url` 是空的** —— 空的 pr_url 就是「这次没真发 PR」的标记。

```

=== 准备：缓存目标仓库 + 起服务
  ✓ 上游就绪，缓存已预热：.repos/kayou/demo-repo
  ✓ 服务就绪 :8077  (provider=scripted, sandbox=docker)

=== 1) webhook 验签 · 幂等 · 授权边界
  ✓ 签名对不上的请求 → 401
  ✓ 没打 repopilot 标签（忽略但 2xx，否则 GitHub 会一直重投） → 200
  ✓ 合法投递 → 202
  ✓ 同一个 delivery-id 重投 → 200
  ✓ 重投被判重，没有开出第二个 run

=== 2) worker 领取 → clone → 容器沙箱里跑测试
  ✓ 走了 RepoCache（clone/fetch）
  ✓ 测试跑在容器里（sandbox.docker）
  ✓ Agent 跑完，停在审批闸门（没有直接 published） → pending_approval

=== 3) 审批闸门 + 审计
  ✓ ★ci-bot 批准自己开的 run → 403
  ✓ 人批准（请求体里冒充 the CTO） → 200
  ✓ ★审批流水记的是 "kayou"，不是请求体里的 "the CTO"

=== 4) 发布
  ✓ 终态 → published
  ✓ pr_url 为空 = 走的 DryRunPublisher，没配 token 就不假装发了 PR

=== 完成，用时 4 秒
全部通过 ✓
```
