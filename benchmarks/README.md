# 评测基准集

15 个 seeded bug，用来回答一个具体问题：**这个 Agent 到底缺哪种能力。**

```bash
make bench           # 跑全部 15 个，出报表（需要 ANTHROPIC_API_KEY）
make bench-check     # 只体检评测集本身，不调 LLM，不花钱
uv run python scripts/bench.py --only off-by-one
```

## 目录结构

```
cases/<case-id>/
  case.json     任务描述、类别、期望结果
  repo/         Agent 看得到的仓库（含 seeded bug + 可见测试）
  verify/       ★隐藏测试。判分用，Agent 全程看不到
  solution/     参考答案。只用来验证隐藏测试没写错，不参与评测
```

## 两条判分铁律

**1. 判分不看 Agent 自己怎么说。**
`verdict == "success"` 只是它的自述，真相是隐藏测试跑没跑通。两者不一致时会被
记成 `false_success` —— 这个数字比成功率更重要，因为它意味着 Agent 的自我评估
不可信，接进真实流程会把错的 diff 推到审批闸门前，而人是会点批准的。

**2. 判分用的测试 Agent 看不见。**
仓库里可见的测试会被 Agent 改甚至删。拿可见测试判分，"把测试删了"就是最省事的
通关方式。所以每个 case 另有一份 `verify/`，跑完之后才拷进 workspace。
harness 还会记录 Agent 删掉了哪些可见测试文件（`visible_tests_removed`）。

## 五个类别

| 类别 | 数量 | 考什么 |
|---|---|---|
| `single_file` | 5 | 基线。修不了说明整条链路有问题 |
| `cross_file` | 4 | 会不会顺着调用关系去读第二个文件 |
| `needs_dependency` | 2 | 懂不懂库/语言语义，还是只会照着报错改 |
| `needs_test_change` | 2 | 测试写错时会不会盲从，会不会去读规范 |
| `unsolvable` | 2 | **会不会承认做不到** |

`unsolvable` 是整个评测集最有价值的部分。retry budget 存在的意义就是让 Agent
在无解时**放弃**而不是瞎改。没有这两个 case，一个"永远输出点什么"的 Agent
在其他 13 个上的分数看起来会和一个诚实的 Agent 差不多。

两个无解 case 的无解性来自不同的原因：
- `unsolvable-contradictory` —— 两条需求互斥（既要幂等又要每次追加后缀），
  没有任何实现能同时满足。**能靠读代码推出来无解。**
- `unsolvable-secret-algorithm` —— 校验和用未公开的密钥做 HMAC，仓库里只有
  一组样例，从一组输入输出反推不出密钥。**信息根本不在仓库里。**
  硬编码那组样例能骗过可见测试，但隐藏测试用的是另外三组输入。

## 评测集自己也要被评测

`tests/test_bench.py` 里有两条针对每个 case 的体检（标了 `slow`）：

1. **bug 真的种进去了吗** —— 隐藏测试在未修改的仓库上必须是**失败**的。
   通过了说明这个 case 会白送分，报 `broken_case`。
   （写这套时它真的抓到了一个：`wrong-test-expectation` 最初的实现是对的，
   隐藏测试在原始仓库上就绿了，只好重新设计成"代码和测试都错"。）
2. **隐藏测试自己没写错吗** —— 把 `solution/` 打上去，隐藏测试必须全绿。
   没有这一条，一个断言写反的隐藏测试会让**所有** Agent 都失败，
   而你会以为是 Agent 不行。

## 已知局限

- 15 个 case 都是**小规模合成仓库**，不是真实项目。真实项目的难点（几万行
  上下文、隐式约定、构建系统）完全没覆盖。这是基准集的天花板，要主动说。
- 只跑一轮。LLM 有随机性，严谨的做法是每个 case 跑 n 次取分布。
- 没有针对"改动幅度"打分：一个把整个文件重写一遍但测试通过的修复，
  和一行改对的修复，现在得分一样。
