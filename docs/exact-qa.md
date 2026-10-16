# 固定问答与统一思维内容协议

## 路由与匹配

继续使用 `POST /api/agent/chat/stream`。ChatService 在会话归属校验、敏感词检查之后，
读取正常问答系统提示词、启用 Skill 和启动普通智能体之前，执行固定 QA 匹配。
`exact_qa.match_mode` 支持 `model`（默认）和 `exact`，两条路径共用原有 QA 回放流程。

模型模式：

1. Agent 调用 `GET /internal/v1/qa/candidates`，读取全部已启用 QA 的当前发布问题、qaId 和 versionId。
2. 一次把本轮用户原始问题和全部候选问题交给模型；模型复用现有 `llm` 连接配置，温度为 0，不绑定工具。
   候选 ID 为 `qa-{qaId}-v{versionId}`。不向匹配模型发送答案、思维或引用，也不发送历史、选区或板块信息。
3. 模型只能返回 `{"status":"matched","candidate_id":"qa-1-v2"}`，或返回 no_match/ambiguous 且 candidate_id 为 null。
   问题应语义等价，不能仅因主题相关就选中；依赖缺失上下文、存在歧义时不匹配。
4. 选中后调用 `GET /internal/v1/qa/{qaId}/versions/{versionId}`，仅当 QA 仍启用且此版本仍是当前发布版本时返回完整快照。
   Agent 再校验快照的 QA ID、版本 ID、问题与候选一致，不要求用户问题与候选原文一致。
5. 命中后原样回放已发布答案、思维和引用。原始用户提问仍按原文保存。

无候选、不匹配、歧义、模型超时/失败、非法 JSON、非法 ID、工具调用、候选接口故障，以及 QA 停用/删除/换版均转入正常问答。
匹配模型输出只用于路由，不作为正文或思维发送前端。异常日志只记录异常类型。
不引入缓存、向量检索、预筛选或分批匹配；候选超出模型上下文导致调用失败时同样转普通问答，不截断候选。

精确模式：继续使用 `POST /internal/v1/qa/exact-match`，请求体为 `{"question":"原始问题"}`。
空格、换行、大小写、标点和全半角均参与匹配，SHA-256 只用于定位，返回前严格比较原文。
未命中继续现有 Agent 流程；超时、非成功 HTTP、无效响应和问题不一致仍返回 `EXACT_QA_LOOKUP_FAILED`，保持原行为。

当前问答库为全局库。问题最长 20000 个字符，超出查询限制直接进入普通问答。
敏感词拒绝和会话归属错误不会触发任何 QA 匹配。

## 数据与发布

dh-knowledge 的 `exact_qa` 保存草稿，`exact_qa_version` 保存不可变发布版本。
发布在问答行锁下生成递增版本，再原子切换线上指针，多实例并发发布不重复分配版本。
编辑草稿不修改发布内容。停用只改变启用状态，保留历史。已发布的问题不可改名，
需要新建问答并停用旧问题。历史版本可以通过管理页面恢复到草稿，再发布为新版本。

管理端路径 `/exact-qa`。接口位于 `/knowledge-api/admin/exact-qa`：

- GET 列表、GET `/{id}` 详情。
- POST 新建、PUT `/{id}` 修改草稿。
- POST `/{id}/publish` 发布、POST `/{id}/disable` 停用。
- GET `/{id}/versions` 获取不可变历史版本。

管理接口复用管理员 JWT 与内部 Token 校验；内部匹配复用服务间 Token。
固定引用保存来源、原文和稳定 citation_id，不允许携带旧会话 buffer_id。
正文中的每个 `[[cite:N]]` 必须存在对应来源。用户端保留固定引用的原始编号、
顺序及切片，普通模型回复继续使用原有引用归并规则。

## 统一 reasoning v1

固定问答与普通助手消息使用相同结构。消息 API 字段名为 `reasoning`，
数据库字段名为 `reasoning_json`。正文仍为消息 `content`；引用仍为 `sources`。

```json
{
  "schema_version": 1,
  "blocks": [
    {
      "id": "reasoning-1",
      "type": "reasoning",
      "round_number": 1,
      "status": "completed",
      "content": "原始思维文本，保留换行与空白。"
    },
    {
      "id": "call-1",
      "type": "process",
      "round_number": 1,
      "data": {
        "agent_id": "agent-loop",
        "step_id": "call-1",
        "parent_id": null,
        "stage": "tool",
        "kind": "tool",
        "status": "success",
        "severity": "info",
        "title": "算法工具箱：东溟知识库检索",
        "summary": "检索完成",
        "sequence": 2,
        "recoverable": true,
        "retryable": false,
        "details": {}
      }
    }
  ]
}
```

`blocks` 数组表示发生顺序，块 ID 唯一，round_number 为正整数。
reasoning 块的 status 为 streaming、completed、failed、cancelled。
process 块沿用现有过程事件字段。固定版本不接受未结束的块；固定 process 块需要
与块 ID 一致的 step_id、正整数 sequence，以及 success、failed 或 cancelled 状态。
无思维内容时 blocks 为空。正文和思维文本不做 trim 或 emoji 过滤；固定分支不执行
CitationService.finalize，完整来源按保存内容返回。

模型适配器保留 Chat Completions `reasoning_content`，以及文本块形式的 reasoning/thinking。
默认 `llm.reasoning_effort: none` 保持现有配置；实际未返回思维内容的模型不会产生
reasoning 文本块，工具执行过程仍会持久化。不从正文或审计摘要补造思维内容，
也不把用于展示的 reasoning 自动加入后续模型输入。

## 流式协议与恢复

- `reasoning_delta`：data 包含 block_id、round_number、content（原始增量）。
- `reasoning_block`：data.block 为完整块，按 ID 替换增量草稿，保留结束状态及附加字段。
- `process`：现有工具过程更新，按 step_id 更新相同过程块。
- `sources`：固定答案在正文前发送完整来源及 answer_origin。
- `token`：原始正文增量。
- `done`：完整 answer、reasoning、sources，固定答案附带 answer_origin=exact_qa 和 exact_qa 元数据。

所有事件使用现有 Run 事件表和 SSE 编码器。固定答案的块顺序和内容保持一致，
文本按配置重新分块，不复用旧会话的事件 ID 或运行 ID。固定输出不调用追问生成模型。

助手消息额外保存 answer_origin、exact_qa（qa_id、version_id、version_no、checksum）、
reasoning_event_sequence。历史加载返回 reasoning_event_id，正文继续使用 stream_event_id。
客户端分别判断两个快照的位置，防止思维快照较新时重放同一增量。
连接断开只关闭订阅，输出任务仍在后台执行；用户取消复用当前 Run 取消流程。
完成、取消及失败会保存思维块结束状态和已输出内容。

旧消息 reasoning_json 为空时返回空结构；已有历史正文与引用不变。
旧过程事件仍可通过原 Run 事件接口查询，本次不对旧消息回填思维文本。

## 配置和升级

`exact_qa.enabled` 默认 YAML 配置为 true；可以设置
`NEW_DH_EXACT_QA__ENABLED=false` 禁用精确查询。
`exact_qa.match_mode` 默认 `model`，可用 `NEW_DH_EXACT_QA__MATCH_MODE=exact` 切回原精确匹配。
`exact_qa.match_timeout` 默认 30 秒，使用 `NEW_DH_EXACT_QA__MATCH_TIMEOUT` 调整匹配模型调用超时。
知识服务 HTTP 超时仍独立使用 `exact_qa.timeout`。模型匹配增加一次非流式模型调用的首字延迟；本次无需新依赖或数据库迁移。
`exact_qa.timeout` 默认 5 秒，`exact_qa.chunk_chars` 默认 80。
`exact_qa.chars_per_second` 默认 40 字符/秒，可用
`NEW_DH_EXACT_QA__CHARS_PER_SECOND=40` 配置为其他正有限数值。
正文按此速率输出；按约 100 毫秒的粒度分块，单块不超过 chunk_chars。
每次分块发送前异步等待对应字符数/速率的时间，数据库与网络延迟可能降低实际显示速度。
复用 dictionaries.base_url、TLS 设置及 internal_request.token，无新增依赖。

建议先更新 dh-knowledge（启动执行 Flyway V5），再更新 new_dh_agent（启动执行 Alembic 0019），
最后更新管理前端；用户端 dh-frontend 使用旧版即可。0019 可以在 MySQL 部分 DDL 已完成时安全重试。
Gateway 与 dh-auth 无需新增路由或权限。数据库迁移应在发布前备份并验证；本次源码修改
不会自动连接生产环境或执行生产迁移。

## 旧版用户前端兼容

正文使用原 token/done 事件，引用仍由 done.sources 返回。
固定问答和普通模型问答的 reasoning 文本块额外投影为 process 事件：
agent_id=reasoning，step_id=reasoning:<block_id>，title 标记轮次，summary 为累计原文。
同一 step_id 持续覆盖 summary，结束时将状态改为 success、failed 或 skipped。
工具步骤继续发送原 process 事件，固定答案保留思维与工具块的原始顺序。
投影事件标记 display_only=true；它们存入 Run 事件日志用于断线重放，
但不写入 reasoning 快照，避免与原始 reasoning 块重复。原 reasoning_delta、
reasoning_block 和 done.reasoning 仍然保留。

此适配无需修改 dh-frontend。旧版组件会折叠文本空白，引用可能归并或重新编号；
旧版历史加载不会恢复过程面板，且发送问题前会去除首尾空白。后端仍按收到的问题
严格匹配并保存原始答案、思维及引用。普通模型问答保持模型实际输出速度。

## 固定 QA 的思考展示配置

旧版 dh-frontend 无需修改，继续通过 process.title、summary 和 running/success 等状态展示。
仅固定 QA 回放读取以下全局设置（同名前缀环境变量 `NEW_DH_EXACT_QA__`）：

- `thinking_title`：默认 `第 {round_number} 轮思考`，仅替换 `{round_number}` 占位符。
- `thinking_summary`：可选；未配置时显示原始思维正文。
- `thinking_min_duration_ms` / `thinking_max_duration_ms`：默认 2000 / 5000，整数毫秒，范围 0～300000。

单条 QA 在现有 reasoning JSON 中可增加可选 presentation 元数据，例如：

```json
{
  "schema_version": 1,
  "blocks": [{"id":"r1","type":"reasoning","round_number":1,"status":"completed","content":"原始思维文本"}],
  "presentation": {"rounds": [{"round_number":1,"title":"正在查询东溟知识库","summary":"正在查询QA库","min_duration_ms":2000,"max_duration_ms":5000}]}
}
```

每轮覆盖优先于全局设置；未提供或 null 表示继承，显式空字符串表示显示空文案。
界面留空会移除覆盖。时长上下限必须同时设置或同时继承，最短不得大于最长；相等为固定时长，0/0 可关闭模拟等待。
配置轮次必须对应 reasoning 文本块且不可重复。标题最多 200 字符，展示文字最多 20000 字符。

每轮只抽取一次目标时长，同轮多个块共享计时。先发送 running，短展示文案立即显示；
原始思维文本未被文案覆盖时，其增量在目标时间内输出。计时结束后发送块原有结束状态，再输出答案。
目标时间为回放调度时间，不包含前面的实际 QA 查询，持久化和网络开销可能使实际展示更长。
正文仍默认 40 字符/秒，普通模型回答不采用模拟计时。
无思维块时不添加思维或等待。取消会终止异步等待，断线重放复用已经记录的 process 事件，不重新抽取时间。

presentation 随现有 QA 草稿/发布版本及助手消息的 reasoning_json 保存，原始 blocks.content 不被替换。
reasoning_delta 可附带 presentation 元数据，事件重建时保留；投影 process 仍不混入原始思维块。
配置修改需重新发布后生效。管理端支持逐轮设置；Excel 使用原有“完整思维JSON”列导入配置，保留旧格式兼容。
本功能不新增表、列或迁移。
