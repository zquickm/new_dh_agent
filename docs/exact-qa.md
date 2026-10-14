# 固定问答与统一思维内容协议

## 路由与匹配

继续使用 `POST /api/agent/chat/stream`。ChatService 在会话归属校验、敏感词检查之后，
读取系统提示词、启用 Skill 和启动模型之前，调用 dh-knowledge 的
`POST /internal/v1/qa/exact-match`，请求体为 `{"question":"原始问题"}`。

当前问答库为全局库。匹配仅使用本轮原始问题，不使用历史、选区、板块、同义词或模型改写。
空格、换行、大小写、标点和全半角均参与匹配。SHA-256 只用于数据库候选定位，
返回前还会严格比较原文。问题最长 20000 个字符，超出查询限制直接进入普通问答。

未命中返回成功封装中的 `data: null`，继续现有 Agent 流程。超时、非成功 HTTP、
无效响应和问题不一致返回 `EXACT_QA_LOOKUP_FAILED`，保存失败 Run，不调用模型。
敏感词拒绝和会话归属错误不会触发精确匹配。

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
`exact_qa.timeout` 默认 5 秒，`exact_qa.chunk_chars` 默认 80。
复用 dictionaries.base_url、TLS 设置及 internal_request.token，无新增依赖。

建议先更新 dh-knowledge（启动执行 Flyway V5），再更新 new_dh_agent（启动执行 Alembic 0019），
最后更新两个前端。0019 可以在 MySQL 部分 DDL 已完成时安全重试。
Gateway 与 dh-auth 无需新增路由或权限。数据库迁移应在发布前备份并验证；本次源码修改
不会自动连接生产环境或执行生产迁移。
