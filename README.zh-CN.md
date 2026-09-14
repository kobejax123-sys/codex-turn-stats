# codex-turn-stats

[English](./README.md) | 简体中文

一个 Codex 插件（plugin），在每轮对话结束后输出一行统计信息：模型生成速度、
输出 token 数、缓存命中率、工具耗时，以及该轮的预估费用。

```text
↳ Hook · 148 tps · 965 out (71% reasoning) · CacheHit 90% · Tools 7 3.2s · $0.0592
```

每一段都可以单独关闭，详见[配置](#配置)。

## 特性

- 每轮结束输出一行，不占用对话内容
- 六项指标可独立开关
- 费用按请求逐条累加，能正确处理长上下文的分段计价
- 通过插件市场分发，不改动用户的 `~/.codex/hooks.json`

## 环境要求

- 支持插件的 Codex（`plugins` 特性默认已开启）
- `PATH` 中有 `python3`

## 快速开始

### 安装

```sh
codex plugin marketplace add kobejax123-sys/codex-turn-stats
codex plugin add turn-stats@codex-turn-stats
```

`marketplace add` 也接受完整 URL（`https://github.com/kobejax123-sys/codex-turn-stats`）
或本地路径；要锁定版本，可以加 `--ref <分支|标签>`。

### 信任 hook

插件声明的 hook 在获批之前不会执行。安装后首次启动 Codex 会出现一个确认界面：

```text
Hooks need review
1 hook is new or changed.
Hooks can run outside the sandbox after you trust them.

  1. Review hooks
  2. Trust all and continue
  3. Continue without trusting
```

选择 **Trust all and continue**。如果选了 *Continue without trusting*，
插件会保持安装状态，但不会有任何输出。

这次确认会把 `trusted_hash` 写入配置的 `[hooks.state]`。哈希只覆盖
`hooks/hooks.json` 里的 hook **声明**，不含 Python 脚本 —— 所以改动
`hooks.json`（或插件升级导致它变化）会再次询问，而脚本本身可以随意修改。

## 各段含义

| 段 | 含义 |
|---|---|
| `148 tps` | 估算的输出速度：输出 token 数除以 transcript 中记录的生成项时长 |
| `965 out` | 该轮的输出 token 总数，跨请求累加 |
| `(71% reasoning)` | 输出 token 中思考 token 的占比 |
| `CacheHit 90%` | 缓存命中的输入 token 占总输入 token 的比例 |
| `Tools 7 3.2s` | 该轮的工具调用次数，以及花在工具上的总时间 |
| `$0.0592` | 该轮的预估 API 费用 |

## 配置

所有开关默认都是 `true`，因此完全没有配置文件时，每一段都会显示。要关掉其中
某几段，把本仓库的 [`turn_stats.json`](./turn_stats.json) 复制到插件数据目录
再改：

```sh
mkdir -p "$CODEX_HOME/plugins/data/turn-stats-codex-turn-stats"
cp turn_stats.json "$CODEX_HOME/plugins/data/turn-stats-codex-turn-stats/"
```

`$CODEX_HOME` 通常是 `~/.codex`。该目录不会自动创建，上面的 `mkdir` 会处理。
这个位置能在插件升级后保留；而插件自身所在的目录每次版本更新都会被整个替换。
所以要改就改这份副本，不要改插件目录里的文件。

```json
{
  "tps": true,
  "output_tokens": true,
  "reasoning_share": true,
  "cache_hit": true,
  "tools": true,
  "cost": true,
  "fast_multiplier": 2.0,
  "model_rates": {
    "gpt-5.6-luna": {
      "input": 0.2,
      "cached_input": 0.02,
      "cache_write": 0.25,
      "output": 1.2
    }
  }
}
```

| 开关 | 控制 | 示例 |
|---|---|---|
| `tps` | 生成速度 | `148 tps` |
| `output_tokens` | 输出 token 数 | `965 out` |
| `reasoning_share` | 输出 token 中思考 token 的占比 | `(71% reasoning)` |
| `cache_hit` | 缓存命中率 | `CacheHit 90%` |
| `tools` | 工具调用次数与耗时 | `Tools 7 3.2s` |
| `cost` | 预估 API 费用 | `$0.0592` |
| `fast_multiplier` | Fast 服务层级下的费用倍数 | `2.0` |
| `model_rates` | 每百万 token 的模型费率；可覆盖或扩展内置费率 | `{ "gpt-5.5": { ... } }` |

说明：

- `tps` 是基于 transcript 时间戳的估算值，不是模型服务商直接报告的速度。生成窗口不足
  500 ms 时不显示 `tps`，窗口太短，其中的速率没有参考价值。
- 当输出中出现 `tps↑` 时，表示至少一个生成项缺少可用的开始或完成时间戳，估算值可能偏高。
- 关闭 `output_tokens` 时，`reasoning_share` 会独立显示为 `reasoning 71%`，
  开关不会失效。
- `fast_multiplier` 在该轮使用 Fast 服务层级时用于放大费用。
- `model_rates` 使用 `input`、`cached_input`、`cache_write`、`output` 四项美元/百万
  token 费率。模型键按不区分大小写的子串匹配，匹配到的键中较长者优先。本仓库模板
  已包含内置费率；要新增或覆盖费率，请修改数据目录中的副本。新增模型必须提供四项
  费率，已有模型则可以只覆盖发生变化的字段。

## 计算方式

hook 在每轮结束时触发一次，事件为 `Stop`，读取 Codex 传入的会话记录
（transcript）。所有数值都取自记录中已有的内容，不做任何推测性测量。

- **`tps`** —— 估算的输出速度，即输出 token 数除以各个生成项从开始到完成的时间跨度。
  工具调用和等待批准的时间不计入，因此它反映的是估算的模型生成速度，而非墙钟速度。
  若任一生成项缺少可用的时间戳，则在速率后追加 `↑`。若该轮没有任何流式生成，则省略
  速率，而不是报 0。
- **`CacheHit`** —— 该轮的 `cached_input_tokens / input_tokens`。
- **`cost`** —— 按请求逐条累加，因为长上下文档位是按请求而非按轮判定的。输入
  超过 272,000 token 的请求会整体重新计价：输入侧费率翻倍，输出费率上浮一半。
  若某一轮中途跨过阈值，则前后分别按各自费率计费 —— 这一点用轮次总量累加是
  表达不出来的。

价格单位为美元/百万 token，按模型 ID 匹配：

| 模型 | 输入 | 缓存输入 | 缓存写入 | 输出 |
|---|---|---|---|---|
| `gpt-5.6-sol` | $4.00 | $0.40 | $5.00 | $20.00 |
| `gpt-5.6-terra` | $2.00 | $0.20 | $2.50 | $12.00 |
| `gpt-5.6-luna` | $0.20 | $0.02 | $0.25 | $1.20 |
| `gpt-6-astra` | $10.00 | $1.00 | $12.50 | $50.00 |

**这是估算值，不是账单金额。** 价格来自第三方整理，而非第一方接口，会随官方
调价而变化，也没有考虑你账号上的任何折扣、赠金或企业协议。要按自己的费率计算，
请修改数据目录副本 `turn_stats.json` 中的 `model_rates`，不要编辑插件脚本。没有
匹配到费率的模型会完全不显示费用，而不是给出一个猜测值。

## 已知限制

- **重开会话后这一行会消失。** hook 输出被有意设计为临时的 —— Codex 把
  `HookCompleted` 归类为不可持久化事件，不会写入 rollout，因此无法回放。它只
  在当次会话的滚动缓冲中可见。
- **没有流式生成的轮次不显示速率。** 这类项会作为单个数据块一次性到达，时间
  窗口只有几毫秒，说明不了速度。
- **首次请求就出现 `CacheHit` 是正常的。** 它表示静态前缀（系统提示词加工具
  定义）在后端已经是热的。缓存按块计量，所以数值是量化的。

## 卸载

```sh
codex plugin remove turn-stats --marketplace codex-turn-stats
codex plugin marketplace remove codex-turn-stats
```

插件删除后，留在 `[hooks.state]` 中的 `trusted_hash` 条目不会再生效，可以从
配置里删掉。

## 许可证

[MIT](./LICENSE)
