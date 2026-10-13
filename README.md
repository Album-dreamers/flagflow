# FlagFlow

[![Test](https://github.com/Album-dreamers/flagflow/actions/workflows/test.yaml/badge.svg)](https://github.com/Album-dreamers/flagflow/actions/workflows/test.yaml)

功能开关与灰度发布服务。基于开源项目 [evo-company/featureflags](https://github.com/evo-company/featureflags)（FastAPI + PostgreSQL，已发布到 PyPI，v2.0.0 于 2026-07 发布）及其 Python SDK [featureflags-py](https://github.com/evo-company/featureflags-py) 改造：

- 配置改动实时推送到客户端（SSE + PostgreSQL LISTEN/NOTIFY），在数据库重启、服务重启、多实例部署的情况下都不丢更新；
- 修改开关改为单事务，加乐观锁，修复了"并发修改被静默丢弃却报保存成功"的问题；
- 用 locust 压测了推送的扩散延迟和单实例容量，压测中发现并修复了两个扩展性问题。

| 目录 | 来源 | 起点 |
|---|---|---|
| `server/` | evo-company/featureflags | v2.0.0（`7294474`） |
| `sdk/` | evo-company/featureflags-py | 0.7.0（`2f36373`） |

两个仓库的完整提交历史都保留在本仓库里。起点之后由 Album-dreamers 提交的才是本项目的改动，可以用 `git log --author=Album-dreamers` 查看。

## 架构

```mermaid
flowchart LR
    UI["管理后台 (React)"] -- GraphQL --> WEB[web 服务]
    WEB -- "一个事务：改规则、版本号 +1、<br/>变更日志、NOTIFY" --> PG[(PostgreSQL)]
    PG -- "LISTEN（提交后才投递）" --> HUB["http 服务 × N<br/>每实例一个 ProjectVersionHub"]
    HUB -- "SSE：只推版本号" --> SDK["业务服务 + SDK"]
    SDK -- "/flags/sync 回拉配置" --> HUB
```

- 写入端：保存开关时，规则、项目版本号、变更日志和 `pg_notify` 在同一个事务里，事务回滚就不会发出通知。
- 推送：每个 http 实例用一条连接 `LISTEN`，按项目唤醒本实例上的 SSE 连接。事件只带版本号，客户端再用 `/flags/sync` 拉配置（通知 + 回拉）。
- 不丢更新：
  1. 客户端连上时带着自己的版本号，如果落后，服务端立即补发；
  2. 监听连接断开后指数退避重连，重连后先按表里的版本补对一遍；
  3. 每 30 秒再对一次，同时用 `SELECT 1` 探测半开连接；
  4. 原有的轮询保留作兜底。

## 改了什么

**1. 灰度放量的哈希里没有开关名**（`sdk/`，提交 `7d1c659`）

原来是 `md5(变量名 + 值) % 100`，所有用同一变量的开关都选中同一批用户，两个各放 50% 的开关有 49.84% 的用户重合，而且最细只能放 1%（0.5% 时一个用户都选不中）。改为 GrowthBook 的 gbhash v2，以开关名为种子、分 10000 个桶，并用 GrowthBook 的 v2 测试向量校验。

**2. 修改开关不在一个事务里**（`server/`，`153e775`）

aiopg 默认自动提交，改规则、版本号加一、写变更日志是分开提交的。测试复现出三种情况：

- 版本号加一失败：规则改了，客户端却永远同步不到；
- 写日志失败：审计记录缺失；
- 两个管理员基于同一个旧页面修改：后保存的人的条件被唯一约束和 `ON CONFLICT DO NOTHING` 静默丢弃，接口却返回成功，日志里还多一条虚假记录。

改为单事务，并给 `flag`、`value` 表加行级 `version` 列做乐观锁；拿旧版本保存会被拒绝。管理后台已经在浏览器里验证过（两个标签页修改同一个开关）。

**3. 轮询改为推送**（`server/` `c22d429`，`sdk/` `cc9ca1f`）

原来 SDK 只能轮询 `/sync`，并且只要版本号不同就下发整个项目的配置。现在服务端新增 `GET /flags/stream`（SSE），SDK 加了 `HttpxManager(stream=True)`：断线后指数退避加 jitter 重连，重连时带上当前版本号，原有轮询保留作兜底。删除开关不加版本号这个上游 bug 也一起修了，否则客户端会一直执行已删除开关的规则。

**4. SDK 的健壮性**（`sdk/` `03f5676`、`376d4ed`）

- 只接受更大的版本号。原来用 `!=` 比较，迟到的旧响应会把配置回滚。
- 本地快照：每次版本前进后原子写入（同目录临时文件 + `fsync` + `os.replace`）；启动时连不上服务端，就用快照里的真实规则，而不是代码里写死的默认值。

**5. 压测中发现的两个扩展性问题**（`server/` `cfdd533`、`1194bc3`）

- 每条新的 SSE 连接都查一次数据库：1 万个连接涌入时打满了 10 个连接的连接池，56% 的客户端一次推送都没收到。改为由 Hub 在内存里维护版本号，同一项目的并发查询合并成一次；只在监听连接正常时信任内存里的值，重连时清空。
- 回拉惊群：所有客户端在同一时刻回拉同一个版本，每次都要重新组装整个项目，1000 个客户端时一轮回拉要 35 秒。改为按（项目，版本）缓存组装结果，同时到达的请求共用一次计算（async-lru）。

**6. 浏览器验证时发现的问题**

- 未登录时打开管理后台是白屏（`9161e50`）：上游 `Base.jsx` 在部分 Hook 之前就提前 return 了（React #300），从 2024 年起一直存在。
- 首页返回 500（`84006dc`）：这是我升级 Starlette 1.x 时引入的回归。原有测试没有覆盖首页，所以全部通过却没发现；已补上测试。

## 用到的现成组件

| 用途 | 组件 |
|---|---|
| SSE 响应、保活、关停时结束流 | [sse-starlette](https://github.com/sysid/sse-starlette)（BSD-3） |
| 多实例转发 | PostgreSQL LISTEN/NOTIFY（通过 aiopg） |
| 放量哈希 | GrowthBook gbhash v2（MIT，移植） |
| 按版本缓存、合并并发请求 | [async-lru](https://github.com/aio-libs/async-lru)（MIT） |
| SDK 解析 SSE | [httpx-sse](https://github.com/florimondmanca/httpx-sse)（MIT） |
| 压测 | [locust](https://locust.io) |

比较过但没有采用的候选（都下载源码核实过）：

| 候选 | 没采用的原因 |
|---|---|
| FastAPI 自带的 `fastapi.sse` | 服务关停时要等超时，然后每条连接打一段异常堆栈 |
| aiopg-listen | 没有保活，连接半开时永远收不到通知；2023 年后没再发版 |
| asyncpg-listen | 要多引一个数据库驱动；和 aiopg-listen 一样，都不通知"已重连"，丢失的通知要等空闲窗口才补 |
| launchdarkly-eventsource | 异步客户端在文档里标为 experimental；要求 Python ≥ 3.10，而 SDK 支持 3.9 |
| Unleash FileCache（fcache） | 临时文件建在系统临时目录，跨文件系统时退化成非原子的复制 |
| atomicwrites | 已停止维护，作者建议直接用 `os.replace` |

## 压测结果

测试条件：

- 阿里云 4 vCPU / 7 GB，压测端、服务端、PostgreSQL 在**同一台机器**上；
- 服务端是单个 uvicorn 进程（uvloop），Python 3.11；
- 项目有 20 个开关；
- 延迟从版本提交那一刻算到客户端收到为止，包含压测端解析流的开销。

脚本和用法见 [`server/loadtest/`](server/loadtest/README.md)。

**单实例推送扩散**（客户端只保持 SSE 连接）

| SSE 连接数 | 送达 | 提交 → 收到事件 P50 / P99 | 服务进程内存 |
|---|---|---|---|
| 1,000 | 100% | 135 / 484 ms | 156 MB |
| 2,000 | 100% | 323 / 843 ms | 214 MB |
| 5,000 | 100% | 1.42 / 1.94 s | 380 MB |
| 10,000（修复前） | 37.9%，只有 4,416 个客户端收到过推送 | — | 525 MB |
| 10,000（修复后） | **100%**（10 万次投递） | 3.03 / 4.16 s | 656 MB |

**收到推送后回拉配置**（1,000 个客户端，每 10 秒发布一次）

| | 缓存前 | 缓存后 |
|---|---|---|
| 追上最新版本的客户端 | 0 / 1,000 | **1,000 / 1,000** |
| 提交 → 配置生效 P99 | 35.2 s | **4.77 s** |
| 提交 → 收到事件 P99 | 19.3 s | **0.68 s** |

**故障测试**

- PostgreSQL 重启：数据库刚恢复、Hub 还没重连上时提交一个版本，它的 NOTIFY 无人接收，但仍然靠重连后的补对送达了全部 300 个客户端（P50 0.95 s）。全程 2,100 次投递全部送达，回拉零失败。
- 两个 http 实例各连 500 个客户端，只写入一次：5,000 次投递全部送达，P99 0.80 s。
- 服务重启：SDK 带 jitter 重连，重连成功时立即拿到停机期间提交的版本。

## 运行

```bash
# 服务端测试：沿用上游的 lets + Docker，PostgreSQL 13 跑在容器里（CI 也是这样跑的）
cd server && lets test

# SDK 测试
cd sdk && pip install -e ".[httpx,aiohttp,requests]" "pytest~=7.1" "pytest-asyncio~=0.18" "faker==18.13"
python -m pytest featureflags_client/tests
```

压测需要先配好服务端的环境变量和 locust，步骤见 [`server/loadtest/README.md`](server/loadtest/README.md)。服务端的其他用法见 [`server/README.rst`](server/README.rst)。

## 已知限制

**大量客户端同时启动时，`/flags/load` 会失败。** 1000 个客户端以每秒 50 个的速度启动，每个都先调一次 `/flags/load`，单个服务进程下的结果：

| 连接池 `postgres.pool_maxsize` | `/flags/load` 失败率 | 追上最新版本的客户端 |
|---|---|---|
| 10（默认值） | 88.9% | 157 / 1000 |
| 40 | 48.0% | 520 / 1000 |

把连接池做成可配置能缓解，但解决不了。根源在于每次 `/flags/load` 都要逐个开关去查询，并更新同一批开关行的 `reported_timestamp`。客户端同时启动时，这些写入在同一批行上互相排队，每个请求占着连接的时间被拉长，于是在连接池上排队超时。连接池调到 40 后，`/flags/load` 的平均耗时反而从 11.6 s 升到 22.0 s。

要根治，需要批量写入上报时间并限制写入频率。这会改变上游"每次加载都刷新上报时间"的行为，暂时没有做。

## 许可证

MIT，保留原作者的版权声明，见 [`server/LICENSE`](server/LICENSE) 和 [`sdk/LICENSE`](sdk/LICENSE)。
