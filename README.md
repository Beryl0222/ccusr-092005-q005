# 文化项目拨付与成效追踪后端

面向省文化产业推进办公室的文化扶持资金后端：规则发布与换版、围绕阶段承诺的申报与
证据提交、业务/财务双审后形成可支付额度、联合申报重复成果的归属协商、失败支付重试，
以及面向年度审计的拨付结果下钻。

**核心立场：事件是唯一事实来源。** 所有写操作都只向仅追加（append-only）事件日志追加
事件，状态由事件流确定性重放得到。资金退回、项目合并、地区变更、跨年结转从不修改旧数据，
而以连续事件保留原公示口径；每条事件带全局序号、前驱哈希与内容哈希，任何事后删改都会在
哈希链校验中暴露。

## 领域与不变量

| 关注点 | 落地方式 |
| --- | --- |
| 支持规则带明确生效日期 | `RulePublished` 事件保留每一版全文；按日期取“当日有效且已生效”的最高版本 |
| 政策换版 | 版本号只能递增；旧版事件永不修改。**财务审核按审核当日有效规则测算，锁定时把规则全文快照钉进 `StageLocked`，故换版只影响尚未锁定的阶段** |
| 阶段承诺 | 申报时固定阶段承诺（成效指标 + 拟拨金额），并钉住申报当日规则版本；提交后计划表不会被覆盖 |
| 三类证据 | 合同 / 服务记录 / 公众反馈；只有被采纳的三类证据齐备才能进入业务审核 |
| 成果去重 | 对成果“身份要素”（不含口径各异的传播量）算指纹 `achievement_fingerprint`；跨申报建指纹索引 |
| 联合申报重复成果 | 不同主体同指纹 → 自动开 `Dispute`，双方阶段进入争议态、证据回到“归属待定”，审核被阻断；可记录多轮协商，由主管部门裁决 |
| 归属裁决 | 对每条候选证据落 `EvidenceAdopted(adopted, credited_share)`；同一成果分成之和恒为 1，裁决必须覆盖全部候选证据；第三个主体并入既有未决争议 |
| 双审形成可支付额度 | 指派的业务审核者（核验证据齐备与成效达标）与财务审核者（按当日有效规则、阶段上限、去重分成测算额度）**都**通过后才能锁定；两岗不得为同一人 |
| 已锁定保护 | 阶段锁定后证据不可改；已随锁定成果申报的指纹不能被再次申报 |
| 支付重试 | 支付尝试带幂等键；同键重放回放首次事件、不重复扣款；失败后可以新键重试，成功即终态 |
| 连续事件 | `ProjectRegionChanged / ProjectMerged / ProjectCarriedOver / ProjectFundsReturned(+PaymentRefunded)` 全部追加，原值留在 history 链 |
| 敏感合同 | 合同正文进独立密件保管库（默认内存 / 可选落盘，文件权限 600），事件流只存 `secret_id`；仅该阶段**实际指派**的业务/财务审核者可取正文，每次访问（含拒绝）写 `SensitiveAccessLogged` |
| 不唯流量 | 成效投影按规则维度（服务人次、满意度、重复参与率、传播量……）汇总，sum 指标按归属分成加权不重复计数；只有流量一维时给出警示 |
| 审计下钻 | 从任一支付单可看到：支付过程、当时有效规则快照、采用证据、去重决定与协商记录、审批链（业务→财务→锁定）、项目连续事件、敏感访问日志 |

## 代码结构

```
cultural_fund/
  eventstore.py   仅追加事件存储：哈希链、乐观锁、幂等键、跨流原子 commit、内存/JSONL 后端
  domain.py       事件类型常量、聚合状态与 fold 规则
  repository.py   由事件重建聚合的只读仓储 + 跨申报成果指纹索引
  vault.py        敏感合同密件保管库（正文不进事件流）
  services.py     应用服务：命令校验→追加事件；地区差距/多维成效/审计下钻投影
  payments.py     支付网关端口与可配置失败次数的模拟网关
  ingest.py       录入 fixtures/seed.json（规则、项目、里程碑）
  auth.py         Bearer 令牌与角色（主管部门/申报主体/业务/财务/审计）
  api.py          stdlib http.server 的 JSON API（零第三方运行时依赖）
  server.py       启动入口（--demo 录入资料与角色）
  clock.py        可注入时钟（测试确定性）
tests/            pytest 测试（44 个，含 HTTP 端到端闭环）
fixtures/seed.json 现有两地区项目、里程碑与两版规则资料
```

## 运行

```bash
pip install -r requirements.txt          # 仅 pytest；运行时只用标准库
python3 -m pytest -q                     # 44 passed
python3 -m cultural_fund.server --demo --port 8080
```

`--demo` 会录入 `fixtures/seed.json`（两版规则：2026-01-01 与 2026-07-01 生效）并打印
各角色令牌。生产持久化可把 `InMemoryEventStore` 换成 `JsonlEventStore(path)`
（逐行追加、重启重放），密件库换成 `JsonFileSecretVault` 或 KMS 信封加密实现。

## HTTP 接口（节选）

鉴权头：`Authorization: Bearer <token>`。写操作返回本次产生的事件（含 seq/hash）。

- `POST /api/v1/rules` 发布规则版本（authority）
- `POST /api/v1/projects`、`POST /projects/{id}/region-change`、`/projects/merge`、`/projects/{id}/carryover`、`/projects/{id}/refund`
- `POST /api/v1/applications` 申报（钉住当日有效规则）
- `POST /applications/{id}/stages/{sid}/reviewers|evidence|business-review|finance-review|lock`
- `GET  /api/v1/disputes`、`POST /disputes/{id}/negotiations|resolve`
- `POST /payments/{id}/simulate-failure`、`POST /payments/{id}/attempt`（body 带 `idempotency_key`）
- `POST /applications/{id}/evidence/{eid}/reveal` 敏感合同阅取（仅实际审核者）
- `GET  /api/v1/reports/regions` 地区受益-承诺差距；`GET /reports/effectiveness` 多维成效
- `GET  /api/v1/audit/payments/{pid}` 拨付结果下钻（auditor/authority）
- `GET  /api/v1/events`、`POST /api/v1/events/verify` 事件导出与哈希链校验

## 一次闭环（已由 tests/test_api.py 端到端覆盖）

1. 主管部门发布 v1 规则（2026-01-01 生效）；城区 A、县域 B 登记项目并申报同一阶段承诺；
2. A、B 就同一场“联合云剧场”提交服务记录 → 指纹命中，自动进入归属协商，双方都无法过审；
3. 双方记录协商意见，主管部门裁决五五分成（分成和=1，覆盖全部候选证据）；
4. 业务审（证据齐备、加权后成效达标）→ 财务审（v1：120000×0.8，去重折算 0.5）→ 锁定，
   锁定事件钉住 v1 规则快照；
5. 支付前两次失败、第三次成功；同幂等键重放不产生新事件、不重复扣款；
6. 期间发布 7 月生效的 v2：已锁定阶段仍按 v1 公示，未锁定阶段审核自动适用 v2；
7. 审计从支付单下钻，看到采用证据、分成去重决定、协商记录、业务/财务/锁定审批链、
   锁定时有效规则全文与支付尝试明细；`/events/verify` 确认日志未被篡改。

## 原有资料

`project_data.load_seed()` 与 `python3 -m unittest discover -s tests` 的种子结构检查保持可用；
`fixtures/seed.json` 在原 `program/milestone` 记录基础上补充了两地区项目、里程碑承诺与两版规则。
