# New DH AgentLoop

固定问答、统一思维内容持久化与 SSE 协议见 [固定问答说明](docs/exact-qa.md)。

精简的 FastAPI + AgentLoop 流式对话后端，接口兼容现有 `dh-frontend`。

## 上下文 Token 预算

主模型使用 DeepSeek 官方 V4 tokenizer 在本地计算每轮输入。默认上下文窗口为
`131072`，预留最大输出 `8192` 和安全余量 `2048`，实际输入预算为 `120832`。
历史按完整问答轮裁剪，工具结果导致后续轮次超限时会受控截断；持久化的会话消息不删除。

相关配置位于 `llm.context_window_tokens`、`llm.max_output_tokens`、
`llm.token_safety_margin`、`llm.reasoning_effort` 和 `llm.tokenizer_path`。
当前 dsv4 Flash 默认显式使用非思考模式 `none`。流式接口新增
`event: context_usage`，`done.data.context_usage` 与会话详情的
`context_usage` 返回最终占用；Run 查询返回累计 Token 字段。

## 配置

- `config/application.yaml`：公共配置
- `config/application-dev.yaml`：开发环境覆盖
- `config/application-prod.yaml`：生产环境覆盖
- `APP_PROFILE`：选择 `dev` 或 `prod`，默认 `dev`
- `NEW_DH_...__...`：使用双下划线覆盖嵌套配置
- `${ENV_NAME:默认值}`：在 YAML 中读取环境变量，环境变量不存在时使用默认值

密钥不要写入 YAML。复制 `.env.example` 为 `.env`：

```dotenv
APP_PROFILE=dev
```

生产环境还必须配置独立的应用持久化库；它与 `tools.database` 指向的只读业务库
不是同一个连接：

```dotenv
DH_AGENT_DB_HOST=mysql
DH_AGENT_DB_PORT=3306
DH_AGENT_DB_NAME=dh_agent
DH_AGENT_DB_USERNAME=new_dh_agent
DH_AGENT_DB_PASSWORD=change-me
```

应用启动时先执行 `CREATE DATABASE IF NOT EXISTS`，再自动执行
`alembic upgrade head`。修改 ORM 实体后，应先执行
`alembic revision --autogenerate -m "变更说明"`，审核并提交生成的增量迁移；
不要修改已经发布的迁移文件。

所有 HTTP 请求必须携带配置项 `internal_request.header_name` 指定的内部
Header。默认名称为 `X-DH-Internal-Token`，Token 从 `DH_INTERNAL_TOKEN`
读取；生产环境必须设置随机值，并与 Nginx、Gateway、其他微服务保持一致。
缺少或不匹配时服务返回 HTTP 403。该校验使用纯 ASGI 中间件，不会缓冲 SSE
流式响应。

### 用户身份与对话隔离

请求链路为：前端携带 `Authorization: Bearer <JWT>` → Nginx → dh-gateway →
本服务。JWT 由网关验证，本服务不重复解析 JWT；在内部 Token 校验通过后，
对话接口还必须携带网关写入的以下 Header：

| Header | 要求 |
| --- | --- |
| `X-User-Id` | 当前登录用户的正整数 ID，范围为 Java Long 正数范围，不能重复传入 |
| `X-Auth-Source` | 必须为 `dh-gateway`，不能重复传入 |

创建会话和发起新对话时还会读取 Gateway 注入的姓名、单位、部门、业务角色和
`X-Profile-Version`，并把创建时的组织归属快照写入 `agent_conversation`。这些字段
当前不扩大访问范围；列表、详情、更新和续写仍严格使用 `X-User-Id` 隔离。

`X-Username`、`X-Token-Id` 不作为归属依据，因此修改用户名或重新登录不会改变
会话所属用户。请求体和查询参数中的 `user_id`、`userId` 不能覆盖网关身份。

- `POST /conversations`：为当前用户创建会话。
- `GET /conversations`：仅返回当前用户的会话，先按用户过滤再排序、限制条数。
- `GET /conversations/{id}`：仅允许查看自己的会话。
- `GET /conversations/{id}/runs/{run_id}`：查询本人会话中一轮 Agent 运行的
  状态、模型、耗时和失败原因。
- `GET /conversations/{id}/runs/{run_id}/tool-calls`：按执行顺序查询该轮工具
  调用的状态、耗时、脱敏参数、结果摘要和失败原因。
- `GET /conversations/{id}/runs/{run_id}/tool-calls/{tool_call_id}`：查询单次工具
  调用审计详情。
- `PUT /conversations/{id}/messages/{message_id}/feedback`：为本人会话中的已结束
  助手回复提交或更新点赞、点踩及可选评论；请求体使用 `feedbackType` 和
  `comment`。
- `DELETE /conversations/{id}/messages/{message_id}/feedback`：软撤回本人对该
  回复的反馈，重复撤回保持幂等。
- `POST /agent/chat/stream`：只允许续写自己的会话；未传 `conversation_id` 时，
  自动为当前用户创建会话，通过首个 `conversation` SSE 事件返回 ID。
- 历史消息、引用来源、消息写入和当前地图选区均按用户与会话 ID 隔离；选区只临时注入本轮模型输入，不写入消息正文。

以上是本服务路径；前端经过网关时使用 `/api/conversations`、
`/api/agent/chat/stream` 等带 `/api` 前缀的路径。
缺少或无效的用户身份返回 HTTP 401；他人的会话和不存在的会话统一返回 HTTP 404。
流式接口在发送 SSE 前检查会话归属，失败时返回普通 JSON 错误，不会先发送 HTTP 200。

前端无需也不应自行设置上述身份 Header，只需携带 JWT。前端保存并复用服务端返回的
`conversation_id`（兼容 `conversationId`）；`sessionId` 不是会话 ID 或用户身份。
`chat_history` 字段仍可传入，但服务端不采信其内容，模型历史只来自当前用户会话的
服务端记录。退出或切换账号时，前端仍应清空本地会话缓存并取消旧账号的流式请求，
防止页面残留之前已下载的内容。

安全前提：网关必须清除客户端伪造的身份 Header 并重新写入；服务端口应通过绑定地址、
防火墙等措施限制为仅允许网关访问。`X-Auth-Source` 本身不是密码，不能代替网络隔离
或内部 Token。生产环境不可使用默认内部 Token。

所有运行环境的会话都使用独立的 `dh_agent` MySQL 数据库持久化，启动时由 Alembic
自动执行尚未应用的版本化迁移；会话、消息、运行记录、工具调用审计、引用来源和地图
上下文均可跨重启及多实例共享。用户消息、助手占位消息和运行记录在 Agent 执行前通过
同一事务写入；运行
完成或失败时，助手消息和运行终态也通过同一事务更新。运行心跳超时后会自动将遗留的
`running/streaming` 状态恢复为 `interrupted/failed`。运行时不存在内存仓储回退：MySQL
配置错误或数据库不可用时，应用会直接启动失败。
工具调用在实际执行前先提交 `running` 审计记录；执行成功、失败或取消后立即写入终态。
参数和错误信息会脱敏并限长，数据库结果行、RAG 正文和观测值不写入审计表，仅保留摘要、
原始结果字节数与 SHA-256。工具失败不会自动将整轮运行标记为失败，Agent 仍可基于失败信息
继续生成降级回答。
单元测试使用 `tests/fakes` 下的测试夹具，不会进入生产运行路径。

迁移 `0012` 为助手消息增加内部 `model_messages` 字段，按完整工具轮次保存模型的
工具调用、参数和实际结果（包括 Skill 全文），后续提问按原顺序恢复。该字段不返回
聊天界面；历史消息数限制仍按可见问答计算，Token 预算包括工具内容，超限时按完整
历史问答组移除，避免拆散调用与结果。旧消息没有完整记录时沿用原有文本历史，无法
从审计摘要补回过去的工具全文。

当前大模型服务不需要鉴权时，`NEW_DH_LLM__API_KEY` 可以留空或省略；如果服务需要鉴权，再配置真实 Key：

```dotenv
NEW_DH_LLM__API_KEY=your-key
```

## 运行

生产环境基线为 CPython 3.9。请使用 3.9.x 创建虚拟环境，避免使用其他 Python 版本生成生产 wheelhouse。

```powershell
.\.venv\Scripts\python.exe main.py
```

流式接口为 `POST /agent/chat/stream`。生产环境的会话保存在 MySQL 中。
智能体最大模型轮次统一默认为 20（`react.max_rounds`），开发与生产环境继承公共配置；
第 20 轮保留给最终回答。同一模型轮次可以提交多个独立工具调用。

## RAG、数据库、观测、地图与专题分析工具

Agent 已注册九个业务工具（括号内为协议调用标识）：

- 东溟知识库检索（`rag_search`）：检索内部知识库并返回文档片段和来源。

  工具仅接受必填的 `query` 和可选的 `tags`（标签名称数组）。标签用于优先检索，
  标签名称按字符串数组透传，带标签片段不足 `recall_top_n` 时平台在授权范围内补召回其他片段，
  标签不是严格过滤条件。每次按场景配置顺序串行查询知识库，所有请求头共用
  `RAG_API_TOKEN`，各请求体的 `knowledge_ids` 仅包含一个库 ID。三个 ID 分别通过
  `RAG_POLICY_KNOWLEDGE_ID`、`RAG_BUSINESS_KNOWLEDGE_ID`、`RAG_LITERATURE_KNOWLEDGE_ID`
  配置；缺失或重复 ID 时拒绝检索，不回退到全库查询。
  `business_scenarios.general`、`business_scenarios.xiamen`、`business_scenarios.三新技术适用评估`
  分别配置通用、厦门、三新技术适用评估场景，也可直接添加其他场景，格式一致：`prompt` 为专属提示词，
  `rag` 包含 `knowledge_bases`、`score`、`result_score_threshold`。
  `tools.rag` 只配置共用服务地址、API Token、租户、部门范围和请求选项。
  各场景的提示词、知识库及检索参数可独立调整；
  已配置以下四个子板块的专属回答风格，RAG 参数暂与 `general` 通用场景一致：

  | `module_l2` | 专属回答风格 |
  | --- | --- |
  | 局地生物爆发分析 | 防控研判简报：现状与风险、诱发与扩散因素、分阶段防控、监测复核，主要关注海洋生物防控 |
  | 海域空间管控优化 | 规划决策建议：目标与矛盾、约束与冲突、方案比较、推荐方案与实施步骤 |
  | 生态环境变化分析 | 监测评估报告：总体变化、指标与时空差异、驱动因素、生态影响与管理建议 |
  | 多类灾害关联分析 | 灾害链研判简报：关联风险、灾害链与时空叠加、暴露对象、联动应对与监测 |

  四个场景分别配置通用三库及相同的召回数量、保留数量和分数阈值，后续可独立调整。
  请求可携带 `"front_context":{"module_l1":"海政协同赋能","module_l2":"政策协同分析评估"}`。
  `module_l2` 去除首尾空白后，精确匹配 `business_scenarios` 下的场景名称时，使用该场景的
  参数和专属提示词；未传、空或未配置的名称均使用 `general` 通用场景。`module_l1` 不参与选择。
  新增场景只需在 `config/application.yaml`（或对应环境的覆盖文件）中添加同名配置，保留
  `general` 通用场景，并重启服务生效，无需修改代码。检索客户端自动汇总所有场景配置中的知识库 ID。
  例如，在 `business_scenarios` 下与 `general` 同级添加以下内容后，前端传
  `module_l2="政策协同分析评估"` 即可使用该场景：

  ```yaml
  政策协同分析评估:
    prompt: |
      围绕用户问题分析政策依据、协同机制与适用条件，引用检索资料。
    rag:
      knowledge_bases:
        - knowledge_id: "你的知识库ID"
          name: "政策协同知识库"
          recall_top_n: 20
          top_n: 4
      score: 0.6
      result_score_threshold: 0.3
  ```

  专属提示词配置在对应场景的 `prompt` 中，追加到本次系统提示词，各轮持续生效；前端仅提交板块名称。
  不从历史继承板块，切换板块以本次请求为准；前端不能指定提示词或直接覆盖 RAG 参数。
  日志 `agent_submodule_selected` 记录本次请求的 `front_context` 与板块对应的基础场景；
  `prompt_scenario_selected`、`rag_scenario_selected` 分别记录实际提示词场景与检索场景，
  其中厦门关键词匹配可以覆盖基础场景。不新增数据库字段或提示词快照。
  原 `tools.rag.<场景>` 配置已移除；部署覆盖配置及环境变量须改用新路径，例如
  `NEW_DH_BUSINESS_SCENARIOS__GENERAL__PROMPT`、`NEW_DH_BUSINESS_SCENARIOS__GENERAL__RAG__SCORE`。
  板块映射与厦门关键词优先规则统一维护在 `services/business_scenario.py`；提示词按本轮问题选择，
  RAG 按实际检索词选择，分别在同义词扩展后调用共享规则。
  厦门保持原有例外规则：`query` 同义词扩展后包含“厦门”“疑点疑区”或“图斑”时，优先使用厦门
  检索参数，与子板块无关。本轮问题同义词扩展后命中这些关键词时，优先使用 `business_scenarios.xiamen.prompt`
  厦门专属提示词，替代当前板块提示词；无需调用 RAG 即可在第一轮模型输入中生效。
  通用场景三个库的召回/保留数量依次为 20/4、50/14、20/4，接口分数阈值 0.6，
  本地结果阈值 0.3，合计最多 22 条。厦门场景前三库依次为 12/3、12/4、12/3，另加厦门局业务库 50/12
  （`RAG_XIAMEN_KNOWLEDGE_ID`），接口分数阈值 0.6，本地结果阈值 0.3，合计最多 22 条。
  所有参数按本次调用传入，共用客户端不修改全局阈值，并发问答不会相互覆盖。
  process 名称及 retrieval 的 `tool_name` 保持原有展示规则：检索词命中厦门关键词时为
  “厦门局业务知识库”，否则为“东溟知识库检索”，不随子板块切换。
  RAG 的 process.summary 在开始时展示工具正在执行，成功完成时仅展示本次检索库数及各库
  最终筛选后按来源文件名去重的文件数（同名文件合并，空库为 0），不展示查询语句。
  文件统计在本地分数过滤及每库 top_n 截断之后计算，各库独立计数，不累加历史调用。
  process.details 与 retrieval 补充 library_count、libraries（库 ID、name、file_count）；
  retrieval.query 仍为扩展后的实际检索语句，result_count 仍表示最终片段数。
  选中场景未配置知识库、厦门关键词命中但未配置 `xiamen`，或任一库请求失败时本次工具报错，
  不切换场景；无结果时返回实际空结果。
  工具 Schema 保持 `query`、`tags`。每库 `recall_top_n` 控制接口召回数量，
  `top_n` 控制每库最终保留数量（均可独立调整）。没有时间标签时按返回 `score` 降序筛选；
  有时间标签时先保留完整年月匹配片段，再保留年份匹配片段，各组内按分数降序，
  其他片段补足剩余名额。接口没有返回可识别的时间标签时保留平台召回顺序，避免破坏平台标签优先级。
  同分保持原顺序，
  缺失或无法解析的分数按 0 处理，不足时返回实际数量，不保证来自不同文件。
  不因结果少或为空而动态降低阈值。结果按配置库顺序
  合并，保留各片段的知识库和文档信息；此路径直接使用接口分数，不调用模型重排，
  不做跨库排序或全局 top_n 截断。
  任一库请求失败时本次工具报错，不将失败误报为无结果。
  时间规划规则在 `services/context_service.py` 的通用 RAG 协议中，每轮动态注入，
  已发布的自定义系统提示词也会获得这些规则。未指定时间时默认优先最新资料，先用当前年份标签，
  不默认加当前月；有本轮已知最新年份时优先该年，资料不足时按实际资料年份补查。
  明确年份使用如 `["2021年"]`，年月使用如 `["2021年", "3月"]`，月份不补零；
  具体日期和单年多月范围保留在 `query` 中，回答时核验切片日期，不构造日级标签。
  业务标签叠加，例如断面2021年3月使用 `["断面", "2021年", "3月"]`。
  多年范围逐年分别调用；“近N年”包含当前年，“过去N个完整年度”不包含当前年。
  例如当前为2026年，近五年对应2022—2026年五次检索，各年独立召回和保留，
  支持在同一模型轮次提交多个调用。结果按实际年份汇总并引用，缺失和失败年份单独说明。
  工具结果与 retrieval 事件的 `requested_tags` 表示请求条件，`chunks[].tags` 表示接口返回的实际标签；
  两者不能混用，缺少实际标签时核验正文及来源日期，不能把其他年份的补召回资料归入请求年份。
  接口文档定义请求 `tags` 为字符串数组，但召回响应未声明 `tags` 字段，客户端仅在实际返回时读取
  字符串数组，不从请求条件推造切片标签；多标签组合的 AND/OR 语义需按实际平台联调验证。
  时间标签仅影响文档检索，断面实测数据仍由空间服务或数据库提供。
- 数据库查询（`database_query`）：将自然语言问题转换成 Apache Doris 只读 SQL，并查询授权业务表。
- 通用空间查询服务（`spatial_query_service`）：查询注册空间要素的目录、基础属性和几何（含观测站），以及观测站在线状态、最新数据和时序数据。
- 地图操作（`map_action`）：通过 SSE 驱动前端地图执行标准可视化动作，不查询或修改业务数据。
- 断面专题分析（`dm_duanmian_analysis`）：发送断面专题分析出图参数，由前端打开独立浮层并查询出图。
- 断面要素对比（`dm_duanmian_element_compare`）：按测点与2–12个月份联动全量化学要素对比浮层。

- 疑点疑区研判（`analyze_suspicion`）：五步串行流程，实时进度，仅返回本次摘要。
- 疑点疑区明细（`query_suspicion_results`）：先筛选再分页，默认第一页10个、最多20个。
- 疑点疑区统计（`summarize_suspicion_results`）：各规则覆盖情况及去重图斑统计。

框架还注册了技能加载（`load_skill`）和当前服务器时间（`CurrentServerTime`）两个工具。

## 疑点疑区

业务入口为 `skills/疑点疑区/SKILL.md`。工具共用 `tools.spatial` 配置，base_url为服务根地址
（不含 `/api`），使用 `SPATIAL_API_KEY`，validate POST不自动重试。
执行参数为batchNo、patchNos、save（默认false），固定①海域权属→②生态修复项目→③历史疑点→④自然岸线→⑤生态红线。
五条规则全部调用真实服务，规则编码为SEA_RIGHTS、ECO_RESTORE、HISTORY、COASTLINE、REDLINE。
前端process携带真实规则状态、计数与保存状态；调用失败发送failed，不再有展示占位分支。
正常共7个process：框架原有整体开始/结束，加每条规则结束后一个process。普通规则失败后继续，401/403停止后续，取消终止执行。

执行只返回本次摘要，不暂存研判明细。查询和统计每次都读取空间服务已保存的正式批次结果。
save=false只试算，本次明细无法后续查询；save=true覆盖所选图斑同规则的正式结果，之后可按批次查询或统计。
明细按图斑编号稳定排序，先对全部结果筛选，再返回指定页；page=1、pageSize=10（最多20），includeEvidence默认false。
正式/results接口未提供分页，所以第一版完整读取后由工具分页；这限制交给模型的数据量，不减少后端读取量。
统计覆盖五条规则；EXEMPT为免审批合规，不计为疑点，HISTORY为历史关联命中，不直接证明新增违法。
缺失或失败不算合规，多标签去重，incompletePatchCount与疑点数可能重叠。
源overlapRatio单位不明确，原样保留，不转换成百分比。

三个工具仅调用疑点疑区规则接口，不依赖通用空间目录、字段定义或图斑清单。
省略batchNo由业务接口取最新批次；执行拿到实际批次后，后续规则沿用它。
查询、统计的/results列表未提供实际批次时返回batchNo=null；连续翻页或研判后回查应沿用已知批次。
查询、统计按五条规则返回的图斑编号并集计算覆盖和去重数量，totalPatchCount仅表示已保存结果中的图斑数。
五条规则均无保存记录的图斑无法统计；空结果不表示原始批次为空或全部合规。

分层：`tool_backends/suspicion_backend.py`调用疑点疑区业务接口；`agents/tools/suspicion.py`管理规则流程、展示事件、筛选和统计。
ToolContext仅传入执行研判工具的进度回调，禁止模型覆盖；不传用户或会话身份。整体开始/结束继续由框架发送，工具只上报规则结果，无额外进度管理或结束追踪。
chat_service只分配序号、持久化和发送，不识别规则或解析业务返回。
所有工具进度遵循现有include_process配置。

## 用户输入敏感词拦截

服务启动时从 `dh-knowledge` 拉取已发布的同义词和敏感词 manifest。远端版本
高于本地版本时，按版本下载不可变快照，并原子覆盖持久化在
`data/dictionaries/`。远端暂时不可用时使用最近一次成功缓存；首次启动既无法
访问远端又没有本地缓存时拒绝启动，避免在没有词库保护的情况下提供服务。

启动完成后，后台任务会按 `dictionaries.refresh.interval_seconds` 周期继续检查
版本，并增加最多 `dictionaries.refresh.jitter_seconds` 的随机延迟。发现新版本
后先完成下载和本地持久化，再构建新的不可变匹配器并原子替换；正在处理的请求
继续使用旧匹配器，新请求立即使用新版本，无需重启服务。定时拉取或匹配器构建
失败时保留当前有效版本并在下一个周期重试。可通过
`dictionaries.refresh.enabled` 关闭运行期间的定时检查，但启动同步仍会执行。

同义词快照用于用户输入和 RAG 检索问题扩展。用户原始输入通过敏感词检查后，
先将别称扩展为“别称（标准词）”（例如 `DO → DO（溶解氧）`），再拼接前端地图
上下文并传入智能体，使问题理解和工具调用都能使用标准词。会话历史保存用户
原文，已有历史及结构化地图上下文不参与扩展。RAG 仍对智能体生成的检索语句
执行扩展，已有对应标准词注释会整体保留，避免重复追加。两处共用同一份支持
热更新的内存匹配器。

敏感词快照中的词条用于用户输入检查，文本
仍会执行 Unicode NFKC 和英文大小写归一化。目前保持原有处理逻辑，不区分
`applyScope`、`action` 和 `matchType`。

命中后不会调用大模型或业务工具，但会将用户原始问题、固定助手提示和状态为
`rejected` 的运行记录写入会话历史；
接口仍按 `conversation → token → done` 顺序返回 SSE，固定提示为
“您的输入包含敏感词，请修改后重新提问。”。敏感词拦截开关由
`sensitive_words.enabled` 配置；服务地址和缓存目录分别由 `application.yaml`
中的 `dictionaries.base_url`、`dictionaries.cache_directory` 配置。内部调用
复用 `internal_request` 中的请求头名称和令牌。

## 系统提示词版本管理

主智能体的 `SYSTEM_PROMPT` 由 `dh_agent` 数据库管理。首次启动会把代码中的默认
提示词初始化为线上版本 v1；管理员通过 `/admin/system-prompts/main` 维护唯一草稿，
保存草稿不会影响线上运行，发布后会生成不可变的新版本。历史版本回滚同样生成新版本，
不会覆盖已有发布记录。

每次对话运行开始时读取一次当前发布版，并在整个工具调用和回答过程中固定使用该版本。
发布操作只影响之后开始的运行。运行记录保存提示词版本 ID、版本号和 SHA-256，便于审计。
Skill 索引、前端地图上下文、用户问题和每轮约束仍由代码动态拼装，不属于在线编辑内容。

管理接口只接受 Gateway 注入的 `ADMIN` 身份；外部地址由 Gateway 的
`/agent-api/admin/**` 路由映射至本服务的 `/admin/**`。

管理员反馈接口同样位于该路由下：`GET /admin/feedback` 支持按评价类型、状态、
用户、关键字和时间分页筛选；`GET /admin/feedback/{id}` 返回反馈所在问答的有限
上下文、运行摘要及工具调用摘要。两个接口都通过 `get_current_admin_id` 执行下游
`ADMIN` 二次校验。

## Skill 按需加载

Skill 放在 `skills/<skill-name>/SKILL.md`。文件使用 YAML frontmatter 提供索引信息，
正文保存完整操作说明：

```markdown
---
name: 观测站分析
description: 查询或分析业务观测站时使用。
---

这里编写完整操作说明。
```

模型通过结构化 `tools` 参数获取可用 Tool 的名称、描述和参数 Schema；system prompt
不再重复这部分内容，只额外提供 Skill 的 `name`、`description`。模型判断 Skill 与
任务相关时，通过 `load_skill` 加载正文；文件路径不会暴露给模型。Skill 根目录由
`skills.directory` 配置，默认是项目下的 `skills`。

项目内置“空间联动”和“断面业务”两个 Skill。业务路由、查询步骤和可视化时机放在
各自独立的 Skill 中，Skill 不互相加载；主模型按任务选择对应 Skill。工具描述说明能力、输入输出和副作用，Schema 保留参数校验，
通用工具不按业务类别限制复用；断面专题出图工具保留先加载“断面业务”的前置提示。空间位置、基础属性和分布（含断面）使用通用空间查询；
断面一览统计使用 `spatial_query_service` 的 `dm_duanmian_overview`，接口未覆盖的历史观测和跨期指标统计使用 `database_query`，指标分析默认结合 `dm_duanmian_analysis` 出图。断面信息类问题（含一览、历史分析、月份对比和报告）默认另用通用空间查询取得 E1/E2/E3 的真实几何，再通过 `map_action(GEOJSON_ADD)` 展示；仅问单断面或测点时也展示三条断面背景，原统计和出图范围保持用户条件。明确只要文字、只要表格或不要图/地图时不绘制。

RAG、重排、数据库和 SQL 生成模型的地址、超时、重试、知识库 ID、数据库账号等
普通参数统一配置在 `config/application.yaml`，不同环境的差异写入对应的
`application-{profile}.yaml`。

密码和 Token 不写入 YAML。YAML 使用类似 Spring 的环境变量占位符：

```yaml
api_token: "${RAG_API_TOKEN:}"
password: "${BUSINESS_DB_PASSWORD:}"
```

复制 `.env.example` 为 `.env`，按需填写：

```dotenv
NEW_DH_LLM__API_KEY=
RAG_API_TOKEN=
BUSINESS_DB_PASSWORD=
DEEPSEEK_API_KEY=
SPATIAL_API_KEY=
```

通用空间查询服务通过 `tools.spatial.base_url` 调用空间计算服务，并在请求头中携带
`SPATIAL_API_KEY`。密钥只配置在 `.env` 或部署环境中，不要写入 YAML。

### 通用空间要素查询（同一个 spatial_query_service 工具）

海岛与断面一览统计复用同一工具及 `tools.spatial` 地址、`SPATIAL_API_KEY`：

- `island_overview`：`GET /api/spatial/islands/overview`。可传 `page`（起始页，默认 1）、`size`（每批默认 20，上限 100）、`keyword`、`province`、`city`、`islandClass`、`minLng/minLat/maxLng/maxLat`（矩形四坐标必须同时传）。工具默认自动翻页并合并全部 `items`，显式传顶层 `fetch_all=false` 时保留原单页模式。保留全部筛选条件；`total/byProvince/byClass` 直接采用接口返回值，不按合并列表或单页分组重算统计。
- `dm_duanmian_overview`：`GET /api/spatial/v2/duanmian/overview`，无其他参数。断面情况概览、断面数、测点数、要素数、数据量、最近航次直接采用 `overview`，单断面统计采用 `sections`，要素清单采用 `chemistryElements/hydrometElements`。不自行归并站位编码重算测点数量，也不把总体统计当成时间或选区筛选结果。

统计取数规则配置在 `config/application.yaml` 的两个场景提示词中：海岛规则使用 `business_scenarios.海岛高质量发展与高水平保护.prompt`，断面规则使用 `business_scenarios.海洋断面数据分析.prompt`。前端通过 `front_context.module_l2` 传对应场景名称，后端按现有场景选择逻辑加载本轮提示词；通用系统提示不再全局附加这两类规则。已有的厦门问题优先规则仍然适用。部署后重启服务即可生效，无需重新发布主提示词。统计只采用本轮成功接口返回值；失败或缺字段时如实说明，不用 RAG、固定数字、历史回答或数据库替代，不补零。仅查询一览统计不强制检索文档、查数据库或打开专题图；历史要素和跨期指标分析沿用现有流程。两个一览操作只返回数据，地图展示继续使用通用空间查询和 `map_action`。

观测站也是注册空间要素，位置、基础详情、名称和类型筛选统一使用以下三个 operation：

- `list_spatial_elements`：无其他参数，调用 `/api/spatial/v2/elements/list`，结果放在 `elements`。
- `get_spatial_element_detail`：传 `dataKey`，调用 `/api/spatial/v2/elements/detail`，获取字段、枚举及查询约束。
- `query_spatial_element`：传 `query`，`query` 对应 `/api/spatial/v2/elements/query` 原始请求体。

详情参数 `dataKey` 和查询参数 `query.element` 必须原样使用目录返回的类别标识；缺少目录结果时先单独查询目录并等待返回，已有结果可复用。地图 `layer_id` 仅用于展示，例如 `transect-stations` 不能作为断面测点的查询类别；目录返回 `dm_duanmian` 时使用该值。收到“要素/图层未注册”时重新获取目录，仅在能确定目标类别时保留原条件修正一次，否则说明限制。

已删除重复的 `list_station_types`、`search_stations`、`station_detail` 及旧 HTTP 入口。
类型枚举从字段定义读取，站名、站号和类型作为通用查询条件；不再本地猜测站点类型、改写站名、筛选去重或重算总数。
通用文档未覆盖实时在线状态及观测数据，因此保留 `station_status`、`observation_summary`、`observation_trend`、`observation_range`。注册属性中的运行状态不能替代实时在线状态。

`spatial_query_service` 的所有操作只返回查询数据，不清图、不标点、不定位，也不为了地图展示额外查询单站详情。已移除 `render_map` 参数和 `map_result` 绘图回执；旧调用传入该参数时返回迁移提示。
通用查询保留 FeatureCollection、features、total、returned，默认在一次工具调用内自动翻页并合并全部后续页。`query.limit` 仅控制每次接口请求的数量，偏移按实际 `returned` 递增；类别、原始几何范围、字段、过滤和排序条件保持不变。通常使用 `query.offset=0`；显式从其他偏移开始只取后续记录，不会补取此前数据。需要原单页模式时传工具顶层 `fetch_all=false`，该参数不发给伙伴接口。
自动翻页新增 `pagination`：`complete` 表示是否取得全部匹配结果，`pages` 为成功合并页数，`fetched` 为合并记录数，`start_offset` 为起始位置，`next_offset`/`next_page` 为未完成时的后续位置。`returned` 更新为通用查询合并后的条数；`total` 保留服务总匹配数。后续页失败、重复、计数异常或总数变化时停止，保留已取得数据并返回 `complete=false` 及 `warning`，不冒充全量、不补零、不放宽条件；首个请求失败仍按原逻辑报错。海岛一览采用同一机制，保留伙伴接口的统计值。
Agent 任务内请求 `includeGeometry=true` 后，查询结果额外返回 `result_ref`，引用本次合并取得的全部几何和属性。模型显式调用一次 `map_action(action_type="GEOJSON_ADD", payload={"result_ref":查询返回的引用})` 即可提交全部已取要素；后端直接复用原始 GeoJSON，避免模型再次生成整批坐标和属性。默认图层为 `spatial-element:{dataKey}`（`obs_station` 使用 `observation-stations`），起始 `offset=0` 时替换，否则追加，`fit_bounds=false`；展示参数可显式覆盖。从非零偏移首次展示时传 `replace=true`。引用按任务隔离，只在当前任务内有效；历史引用失效时重新查询。查询仍只返回数据，地图操作由模型显式调用；`pagination.complete=false` 时必须说明展示范围。
引用由后端展开，前端仍收到现有 `GEOJSON_ADD` 和完整 `payload.geojson`，不接收 `result_ref`；详情沿用 GeoJSON properties 拾取。没有引用的其他数据来源仍支持内联 GeoJSON、`MARKER_ADD`；清图和定位由地图工具完成。不同类别分层展示，保留选区；未取全或缺少几何时说明实际覆盖范围。单点查询引用同样可用于 `FLY_TO`。

`GEOJSON_ADD` 的工具 Schema 明确两种输入：仅传 `result_ref`，或同时传 `layer_id` 与 `geojson`。为兼容模型的重复参数，有效引用附带相同 `geojson` 或 `geojson=null` 时复用后端原始结果；类型或要素内容不一致仍拒绝，比对时忽略查询计数等顶层元数据。内联 GeoJSON 遗漏 `layer_id` 时，仅在当前任务缓存中存在唯一、完整匹配的结果时补齐原图层及分页参数；没有匹配、匹配不唯一或引用过期时保留校验错误，不猜测图层。工具失败后模型可修正重试，已发出的失败过程记录不会因后续成功而撤回。

地图动作回执仅代表指令已发送，不代表前端已经渲染。观测站搜索保留缺少坐标的业务记录，缺少坐标只影响后续绘图，不影响查询数量。
工具错误统一标识“通用空间查询服务”，保留 operation、HTTP 路径及服务错误说明；查询失败不代表无匹配数据。
`observation_range` 对应 `GET /api/spatial/stations/observation-range`，参数为 `tzno`、`data_type`、`start_time`、`end_time`、`order_by`、`max_records`。HTTP 500 表示上游服务未能完成请求；仅凭“服务内部错误”无法区分数据库、字段映射或时间处理异常。需结合智能体 `tool_failed` 日志中的 run_id、call_id、arguments 与同一时刻空间服务日志排查；不要把失败当成无数据，也不要盲目改日期或指标重复查询。
单站飞行定位调用 `map_action(action_type="FLY_TO", payload={"geojson": 仅含目标站点的FeatureCollection})`，保留真实 Feature.id、geometry 和完整 properties；中心由 Point 坐标推导。后端依次发送 `CLEAR`、`GEOJSON_ADD`、`FLY_TO`，使用 `map-location-markers` 图层。前端须接入 GeoJSON 拾取并展示 properties，详见《地图操作SSE事件契约.txt》；仅接收事件不会自动实现点击弹窗。
字段及查询约束以目录详情为准，由空间服务最终校验。
复用 `tools.spatial` 和 `SPATIAL_API_KEY`，base_url 继续只配置主机与端口，不附加 v2 路径。

框选区域询问“有什么要素”时，加载“空间联动” Skill，先发现目录，再按同一范围逐类查询，包括 `dm_duanmian`；目录本身不代表区域内存在该类别。断面位置、基础属性和分布通过通用查询返回后调用 `map_action` 展示，不打开断面专题图。某类别无法完成空间查询时说明限制，不静默跳过，也不放宽为全库查询。
一览接口未覆盖的断面历史观测和跨期指标统计按“断面业务” Skill 使用 `database_query`，涉及指标、变化或分析时默认调用 `dm_duanmian_analysis`。三条断面的地图背景优先复用目录中实际注册的断面线几何，仅有测点时展示三条断面的全部测点，不自行连线。一次查询覆盖三条断面并使用合并结果的 `result_ref` 提交地图；分别查询时同图层首次替换、后续追加，避免互相覆盖。历史航次的实际测点坐标仍可由数据库查询后使用 `map_action(MARKER_ADD)` 展示于独立图层 `transect-stations`，保留选区和其他类别图层。专题分析和要素对比浮层不代替三条断面的地图背景，空间服务失败不阻止原专题出图。
工具标识统一为 `spatial_query_service`，中文显示名为“通用空间查询服务”；对接方按工具名处理的逻辑及已发布提示词中的引用也需同步更新。
已有环境需同步发布更新后的系统提示词，将具体业务路由交给当前 Skill；代码只更新默认提示词，不覆盖数据库中已发布的版本。工具代码更新后需重启服务；Skill 文件按次读取，历史对话中已加载的旧说明应在新会话中重新加载。

### 观测站地图联动事件

查询完成后，模型显式调用 `map_action` 才会发送地图事件。当前动作协议版本为 `1.0`：

- `CLEAR` 可清除指定图层；是否清理由 Skill 根据查询结果决定。
- 通用查询优先通过 `result_ref` 展开为 `GEOJSON_ADD` 展示点线面；`MARKER_ADD` 仍支持其他来源的真实点位。
- 单站状态、最新观测和时序查询不自动标点、清图或构造弹窗，地图展示由 `map_action` 完成。
- 独立调用 `FLY_TO` 时，后端会展开为 `CLEAR → GEOJSON_ADD → FLY_TO`；站点定位传入完整单点 `geojson`，普通坐标定位仍支持 `center`（兼容输入 `coordinates`）和 `zoom`。保留最新定位点及其属性，切换后需要展示其他结果时再添加对应图层。

```text
event: map_action
data: {
  "code": 200,
  "message": "success",
  "data": {
    "version": "1.0",
    "conversation_id": "conversation-1",
    "run_id": "run-1",
    "batch_id": "call-map-1",
    "action_id": "call-map-1-1",
    "seq": 1,
    "action_type": "MARKER_ADD",
    "payload": {
      "layer_id": "observation-stations",
      "markers": [],
      "fit_bounds": true
    }
  }
}
```

前端应在同一 `batch_id` 内按 `seq` 串行执行，并用 `action_id` 去重。地图动作
直接复用 `/agent/chat/stream`，当前实现不会调用 `POST /api/map_action`。完整协议
见 `地图操作SSE事件契约.txt`。

### 断面专题分析动作

`dm_duanmian_analysis` 对应前端文档中的同名 operationId，复用现有
`ToolOutput.client_events → AgentLoop → ChatService → SSE` 链路。
该动作是虚拟通道，当前实现不请求 `POST /api/duanmian_analysis`，无需新增空间服务密钥或数据库表。
调用参数和动作数量通过工具调用日志记录。

| 参数 | 规则 |
| --- | --- |
| `analysis` | 必填：`temp-section` 整条断面的化学要素剖面（海水温度、盐度等）、`ts-scatter` T-S 点聚图、`nutrient` 营养盐结构、`do-profile` 溶解氧垂向分布、`surface` 表层沿程变化 |
| `section` | 可选：`E1` / `E2` / `E3`；`ts-scatter` 固定合并全部断面，传入的断面会被移除 |
| `voyage` | 可选：有效的 `yyyy-MM`；省略时不自动查询 |
| `elementType` | 可选：`chemistry` / `hydromet`；`temp-section` 始终设为 `chemistry` |
| `element` | 可选：仅接受 Schema 枚举中的中文名称（如 `温度 T`、`气温`），不接受物理列名；按 `elementType` 选择对应要素 |

前端文档未定义 SSE 事件名和 JSON 外层；本次接入约定复用 `event: map_action`，
以 operationId `dm_duanmian_analysis` 作为 `action_type`，`payload` 保持文档参数名（包括 `elementType`）。
前端需增加该动作分支，按 `payload` 打开断面专题分析浮层；这是新增的联调约定，需前端适配。

```text
event: map_action
data: {"code":200,"message":"success","data":{"version":"1.0","conversation_id":"<会话ID>","run_id":"<运行ID>","batch_id":"call-analysis","action_id":"call-analysis-1","seq":1,"action_type":"dm_duanmian_analysis","payload":{"analysis":"temp-section","section":"E1","voyage":"2025-11","elementType":"chemistry","element":"温度 T"}}}
```

仍按 `action_id` 去重、同一 `batch_id` 内按 `seq` 顺序处理。参数无效时不发送动作。
工具返回 `success: true, message: "action received"` 仅表示指令已接收，不是前端出图成功回执。
断面指标、变化和分析默认查询数据库并展示相关专题图；用户明确只要文字或不要图时不出图。位置、数量和区域总览只联动地图；仅要求出图时仍先加载“断面业务” Skill，但无需先查询数据库。年份或时间范围查询选择结果中最新的实际航次作为示例并说明，不随机编造航次；没有可靠航次时省略 `voyage` 打开浮层。

后续新增观测、海域等分析时，按现有方式增加独立工具模块，在
`agents/tools/registry.py` 注册，并在 `services/chat_service.py` 添加中文展示名。
新动作继续使用各自 operationId 和 payload，前端增加对应处理分支即可；不需要修改执行器或 SSE 编码器。

### 断面要素对比动作

`dm_duanmian_element_compare` 按伙伴提供的 `openapi-duanmian-element-compare.yaml`
接入，沿用上述虚拟动作链路，不请求 `POST /api/duanmian_element_compare`。
先加载“断面业务” Skill；跨月各类要素对比默认联动此浮层，需要文字分析时仍查询数据库。
仅要求出图时无需查询数据库。用户明确不要图时不触发。

- `months`：必填，2–12个有效的 `yyyy-MM`；保留数组顺序，首月为变化基准。
- `station`：可选，E1/E2/E3 前缀的5位测点编码，缺省 `E1001`。
- 前端固定查询全量化学10项；同站同月取最后一次观测（航次），多层水深取表层。
  浮层展示单个测点，默认测点图不能代表三条断面的整体统计。

```text
event: map_action
data: {"code":200,"message":"success","data":{"version":"1.0","conversation_id":"<会话ID>","run_id":"<运行ID>","batch_id":"call-compare","action_id":"call-compare-1","seq":1,"action_type":"dm_duanmian_element_compare","payload":{"months":["2022-11","2021-11"],"station":"E1001"}}}
```

SSE 的 `action_type` 原样使用工具 operationId `dm_duanmian_element_compare`，
由前端映射至其内部 `DUANMIAN_ELEMENT_COMPARE` 动作并打开「断面要素对比」浮层。
工具回执为 `{"success":true,"months":[...],"station":"E1001"}`，只确认指令接收，
不代表前端出图完成。参数无效时不发送动作，并将具体错误返回模型以便修正。

### 地图选区上下文

`/agent/chat/stream` 接受前端通过 `map_context.drawn_geometry` 传入 GeoJSON
`Polygon`，或传入由 `center` 和 `radius` 表示的圆形选区。
圆心坐标格式为 `[经度, 纬度]`，半径单位为米。后端会在接口边界校验坐标并转换为
Agent 可使用的内部上下文；
`AgentLoop`、LLM 客户端和工具不直接依赖前端字段名。
会话保存当前有效选区，每轮只临时拼接在用户问题后，不写入用户消息正文。
未携带 `map_context` 表示继承当前选区；有效几何数据替换选区；显式传入 `null`、空对象或
`drawn_geometry: null` 表示清除选区。`context`、`mapContext` 别名遵循相同规则。
例如，框选后问“有哪些观测站”，再不携带选区追问“具体有哪些雷达站”，仍查询原区域并按雷达类型过滤。
前端应只在选区更新或清空时发送该字段；普通追问省略该字段，不要默认发送空对象或反复回传旧选区。
不额外调用模型判断选区状态。正式回答模型在有效选区下遇到其他地点、无关问题或取消选区时，
先单独调用现有 `map_action(action_type="CLEAR", payload={}, clear_selection=true)`，
收到结果后再回答或查询；例如“介绍一下厦门”“解释潮汐原理”都应先清除旧选区。
`clear_selection` 是新增的顶层工具参数，仅后端使用，不属于前端 payload，不新增工具或 SSE 字段。
会话服务保存空选区，当前回答循环移除已经注入的选区数据并保留工具调用记录；清除后后续请求不再继承旧选区。
清除会话选区后，沿用已约定的 `CLEAR` SSE 事件通知前端清图，`payload` 原样发送；
空对象表示按现有契约清除全部 Map Action 图层，指定 `layer_ids` 时只清理指定图层，无需新增前端字段。
切换地点后的“这里”“该地区”应指向最近明确地点；新地点缺少范围或查询失败时，不借用旧选区。
前端仍可显式发送上述清空字段。普通 CLEAR、单站定位、适配视野和清理标记图层不清除会话选区。
查询本轮新框选范围时保留新选区。前端按既有 CLEAR 契约处理地图清理；后端不等待前端渲染回执。
前端普通追问必须省略 `map_context`，否则重复提交的旧几何会按新选区重新保存。
较早请求迟到的选区写入不能覆盖较新请求的选区状态。
用户明确指定的地点优先于默认选区；除非用户明确询问或比较选区，回答不主动复述框选范围、坐标或无关的历史站点结果。
这些回答规则通过提示词约束，输出端不另做选区内容过滤。单站查询仅返回数据，需要定位时另行调用地图工具；
空间联动中切换到其他地点时，已有可信坐标则通过 `FLY_TO` 移动视角。
只有地点名称或坐标时，不自动猜测区域边界或搜索半径；用户重新框选后，以新选区为准。

```json
{
  "question": "这个区域有哪些观测站点",
  "conversation_id": "后端会话 ID",
  "map_context": {
    "drawn_geometry": {
      "type": "Polygon",
      "coordinates": [[[121.6, 31.1], [122.5, 31.1], [122.5, 31.9], [121.6, 31.9], [121.6, 31.1]]],
      "bbox": [121.6, 31.1, 122.5, 31.9],
      "crs": "EPSG:4490"
    }
  }
}
```

圆形选区请求示例：

```json
{
  "question": "这个圆形区域有哪些观测站点",
  "conversation_id": "后端会话 ID",
  "map_context": {
    "drawn_geometry": {
      "center": [121.8, 30.4],
      "radius": 25000,
      "crs": "EPSG:4490"
    }
  }
}
```

圆形选区也兼容 `query_type: "circle"`。若前端复用地图动作契约，还支持
`{"action_type":"CIRCLE_DRAW","payload":{"center":[121.8,30.4],"radius":25000}}`；
HTTP Map Action 的驼峰字段 `actionType` 同样会在接口边界转换。

前端不要再把 `[系统上下文]` 或外接矩形说明拼入 `question`，否则会造成上下文
重复，并污染保存的用户消息。`conversation_id` 应来自后端创建会话接口；若省略，
流式接口会创建新会话，并在首个 `conversation` 事件中返回 ID。

数据库 SQL 生成默认复用 `llm` 配置；仅当需要独立鉴权模型时填写
`DEEPSEEK_API_KEY`，并在 `tools.database.sql_llm` 中配置独立模型地址和名称。
数据库账号必须是专用只读账号，只授予 `SELECT` 权限。

断面数据库使用 `donghai_dwd` schema；查询协议使用 Doris 的 MySQL 兼容接口。

数据库工具仅允许查询 `database_information/` 中具有 DDL 文件的授权表。
修改表范围时，需要同时更新对应 DDL、断面 `数据库_AI_SQL查询指南.md`，以及涉及
业务语义的 Skill 说明。

### RAG 检索片段流事件

`rag_search` 成功完成召回和可选重排后，流式接口会在工具完成状态之前发送
`retrieval` 事件。该事件表示本次实际返回给 Agent 的最终片段，不包含未通过
筛选或重排的候选记录：

```text
event: retrieval
data: {
  "code": 200,
  "message": "success",
  "data": {
    "tool_call_id": "call-rag-1",
    "tool_name": "东溟知识库检索",
    "query": "东海赤潮相关资料",
    "result_count": 1,
    "chunks": [
      {
        "rank": 1,
        "buffer_id": 19,
        "source": "资料.pdf",
        "preview": "检索片段正文",
        "score": 0.91,
        "document_id": "doc-1",
        "chunk_id": "chunk-1",
        "knowledge_id": "kb-1",
        "title": "章节标题",
        "url": null,
        "page_index": 2,
        "line": null,
        "page": 3
      }
    ]
  }
}
```

`page_index` 是上游返回的零基页索引，`page` 是兼容前端直接展示的一基页码。
前端应以 `tool_call_id` 区分同一轮中的多次检索，并将 `preview` 作为不可信纯文本
渲染。`retrieval.chunks` 只返回本轮内的来源编号 `buffer_id`；此时回答尚未完成，因此不返回
本次回答的 `citation_id`。

RAG 不再使用会话证据缓冲区，也不读写原有缓冲区表。一轮指一次用户提问及其回答过程，
同一轮内可以多次调用工具并使用本轮已检索的片段。`rag_search` 工具消息直接提供片段正文
及 `source_key`，不向系统提示词注入检索内容。
保存到模型历史的 RAG 工具结果只保留 `{"success": true}` 或 `{"success": false}`；
读取旧历史时也会执行同样的处理。下一轮模型仍可看到正常问答历史，但看不到上一轮的
原始检索片段，需要文档依据时须重新检索。
模型使用 `<<SRC_数字>>` 引用本轮来源，后端按首次出现顺序转换为从 1 开始的
连续 `[[cite:n]]` 展示编号：

```text
event: done
data: {
  "code": 200,
  "message": "success",
  "data": {
    "conversation_id": "conversation-1",
    "answer": "报告显示盐度存在变化[[cite:1]]。",
    "model": "dsv4",
    "sources": [
      {
        "citation_id": 1,
        "buffer_id": 19,
        "source": "资料.pdf",
        "score": 0.91,
        "preview": "检索片段正文",
        "title": "章节标题",
        "url": null,
        "page_index": 2,
        "line": null,
        "chunk_id": "chunk-1",
        "knowledge_id": "kb-1"
      }
    ],
    "images": [],
    "assistant_message_id": "message-1"
  }
}
```

只有模型显式输出的合法来源键才构成文档引用；正文仅提及文章名称不会隐式新增来源。
`citation_id` 和兼容字段 `buffer_id` 都只在本轮回答内有效，不可跨轮使用。
`done.sources` 与历史消息仍保存来源快照，供前端恢复原回答的引用卡片；这些快照不会
作为后续问答的检索上下文。旧缓冲区表及历史迁移保留兼容，聊天流程不再使用。

数据库查询成功（`success=true`）且 `rows` 中存在非空值时，本轮自动注册一项
“断面数据库”来源（`source_type=database`），多次查询合并到同一来源。
失败、空行列表及全空值结果不注册；数值 0 是有效值。
数据库来源直接保留在 `done.sources` 和历史消息的来源列表中，
不向回答正文或 `token` 追加数据来源说明。数据库与文档共享本轮编号，
数据库卡片复用 `source/title/preview/url` 字段，`url` 为空，预览显示查询条件和返回行数。

## 内网部署

项目依赖已经锁定为支持 CPython 3.9 的版本。建议先在外网 Python 3.9 打包机上升级 `pip`：

```powershell
python -m pip install --upgrade pip
```

### 导出 Windows x86-64 wheelhouse

下面命令面向常见的 64 位 Intel/AMD Windows：

```powershell
python -m pip download `
  --requirement requirements.txt `
  --dest wheelhouse\windows-x86-cp39 `
  --only-binary=:all: `
  --implementation cp `
  --python-version 39 `
  --abi cp39 `
  --platform win_amd64
```

如果目标确实是 32 位 Windows，将目标目录改为 `windows-x86-32-cp39`，并将平台参数改成：

```powershell
--platform win32
```

注意：部分底层扩展依赖可能不提供 CPython 3.9 的 `win32` wheel；命令失败时应改用 64 位 Windows/Python，或在真正的 32 位 Windows 构建机上逐个构建缺失 wheel。

### 导出 Linux ARM64 wheelhouse

以下命令面向 CPython 3.9、Linux ARM64/aarch64，并优先选择兼容性较广的 manylinux wheel：

项目将 `huggingface-hub` 固定为 `0.27.1`。上下文 Token 计数只使用本地
`tokenizer.json`，不需要 `hf-xet`；请勿额外加入 `hf-xet==1.1.10`，该版本的
ARM64 wheel 仅兼容 `manylinux_2_28_aarch64`，会导致下面面向
`manylinux2014_aarch64` 的导出命令解析失败。

```powershell
python -m pip download `
  --requirement requirements.txt `
  --dest wheelhouse\linux-arm64-cp39 `
  --only-binary=:all: `
  --implementation cp `
  --python-version 39 `
  --abi cp39 `
  --platform manylinux2014_aarch64 `
  --platform manylinux_2_17_aarch64
```

如果 ARM64 服务器使用较新的 glibc，并且某个包没有
`manylinux2014_aarch64` wheel，可另外尝试：

```powershell
python -m pip download `
  --requirement requirements.txt `
  --dest wheelhouse\linux-arm64-cp39 `
  --only-binary=:all: `
  --implementation cp `
  --python-version 39 `
  --abi cp39 `
  --platform manylinux_2_28_aarch64
```

`--only-binary=:all:` 会在任一依赖没有目标平台 wheel 时立即失败，避免误把源码包带入内网。遇到这种情况，应在对应目标系统上构建缺失 wheel，不要直接在 Windows 上交叉编译 Linux wheel。

### 校验离线包是否完整

在 64 位 Windows x86-64、CPython 3.9 环境中使用对应目录模拟无网络安装：

```powershell
python -m pip install `
  --dry-run `
  --no-index `
  --find-links wheelhouse\windows-x86-cp39 `
  --requirement requirements.txt
```

ARM64 wheelhouse 最可靠的验证方式，是在与内网服务器相同的 Linux ARM64、CPython 3.9 环境中执行：

```bash
python -m pip install \
  --dry-run \
  --no-index \
  --find-links wheelhouse/linux-arm64-cp39 \
  --requirement requirements.txt
```

### 内网安装

将项目及目标平台对应的 wheelhouse 目录复制到内网。Windows x86 安装：

```powershell
python -m pip install `
  --no-index `
  --find-links wheelhouse\windows-x86-cp39 `
  --requirement requirements.txt
```

Linux ARM64 安装：

```bash
python -m pip install \
  --no-index \
  --find-links wheelhouse/linux-arm64-cp39 \
  --requirement requirements.txt
```

## 测试

以下命令设置测试用模型密钥并运行后端测试。

```powershell
$env:NEW_DH_LLM__API_KEY = "test-key"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Skill 独立开关

管理员可在管理前端的“Skill 管理”页面独立启停 Skill，所有用户统一生效。状态保存在
MySQL 的 `agent_skill_setting` 表中，启动迁移 `0013` 自动建表，新发现的 Skill 默认启用，
已有设置不会被初始化覆盖，重启后保留。

每次新任务（包括同一会话继续提问）读取不可变的启用名单。关闭的 Skill 不进入模型的目录摘要，
`load_skill` 无法加载它；本次上下文副本也会移除该 Skill 的历史加载调用及返回说明，保留其他
Skill、业务工具结果和正常问答正文，不改写数据库历史。正在执行的任务继续使用启动时的名单。
读取配置失败会终止新任务，不回退为全部启用。开关不关闭任何业务工具，Skill 无需声明 `tools`。

管理接口沿用管理员鉴权与统一响应格式，外部通过 `/agent-api` 网关入口访问：

| 方法与后端路径 | 行为 |
| --- | --- |
| `GET /admin/skills` | 返回已发现的 Skill 名称、说明、状态、修订号和修改信息 |
| `PUT /admin/skills/{skillKey}/enabled` | 接收 `enabled` 和 `expectedRevision`，仅更新指定 Skill |

列表及更新结果使用 `skillKey`、`name`、`description`、`enabled`、`revision`、`updatedBy`、
`updatedAt` 字段。修改冲突返回 HTTP `409`，前端刷新该项并提示重试；不存在的 Skill 返回 `404`。
未授权或普通用户不能调用管理接口。

新增 Skill 只需在配置的 Skill 目录下新增 `<名称>/SKILL.md`，沿用现有 YAML 头部的 `name`、
`description` 和 Markdown 正文，`name` 必须与目录名一致。部署并重启后自动进入管理列表，
无需修改页面、接口、表结构或工具注册。名称是持久化标识，重命名视为新增 Skill。

`tests/test_skill_management.py` 包含自动发现、独立开关、管理员权限、修订冲突与任务快照测试；
上下文、工具和任务测试另覆盖历史过滤、四种开关组合及并发隔离。真实 MySQL 测试默认跳过，
仅在显式指定 `SKILL_TEST_DB_NAME` 且与测试进程数据库配置一致时运行；使用独立临时库，
不要指向共享业务库。本地联调若外部登录或词库不可用，可使用仅监听回环地址的临时认证代理
和测试替身，验证后停止代理并清理测试登录状态，正式登录与鉴权代码始终保留。


## 模型工具调用诊断日志

当前 `config/application.yaml` 默认开启诊断，INFO 级别即可记录；定位后可设置
`NEW_DH_LLM__DIAGNOSTICS_ENABLED=false` 并重启。无需启用 HTTPX/SDK DEBUG，也无新增依赖。

按同一 `run_id`、`round` 对照三层 `llm_diagnostic`：

- `stage=upstream_text/upstream_tools`：OpenAI SDK 解码后的上游 JSON delta，位于 LangChain 工具解析前；包含普通文本、`tool_calls` 或旧式 `function_call`。这不是原始 HTTP 字节抓包；SDK 解码前的协议损坏仍需服务端日志或抓包核查。
- `stage=langchain_text/langchain_tools`：应用收到的 LangChain chunk，记录 `content`、`tool_call_chunks`、`tool_calls`、`invalid_tool_calls` 和结束原因。
- `stage=application_tools`：流结束后的合并结果，包括有效与无效调用。

`llm_stream_summary` 保留所有 chunk 的计数、上游工具 delta 数、解析无效调用数、最终调用数、
真实 `finish_reasons`、上游响应 ID 和模型。未收到结束原因时为空，不推断成 `stop`。
`status=completed` 仅代表应用读完流；例如 `finish_reasons={'length': 1}` 仍表示上游长度截断。
`llm_usage` 在上游提供用量时记录，不为获取用量改变请求参数。

每轮每层的文本、工具日志分别最多 100 条、16384 个 JSON 载荷字符，可通过
`NEW_DH_LLM__DIAGNOSTICS_MAX_EVENTS`、`NEW_DH_LLM__DIAGNOSTICS_MAX_CHARS` 调整。
限额不影响模型生成或工具执行；超限显示 `truncated=true` 或在汇总中计入 `*_omitted`。
文本耗尽额度不会占用工具额度，结束原因与最终计数始终保留；存在截断时不要把未打印的内容当作上游未返回。
日志不采集请求认证头或 API Key，但诊断片段本身可能包含业务数据。

普通日志已取消整段系统提示词、历史消息、检索片段、数据库数据行及完整回答的重复打印。
保留用户问题预览（最多1000字符）、消息角色和长度、工具名称/参数、耗时、错误堆栈、
结果长度、查询/SQL摘要、行数/片段数、文章名、最终回答长度及来源数量。
`agent_completed reason=no_tool_calls` 是业务循环退出条件，与上游 `finish_reason` 分开记录。
