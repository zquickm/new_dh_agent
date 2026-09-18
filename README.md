# New DH AgentLoop

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

## RAG、数据库、观测、地图与专题分析工具

Agent 已注册五个业务工具（括号内为协议调用标识）：

- 东溟知识库检索（`rag_search`）：检索内部知识库并返回文档片段和来源。

  工具仅接受必填的 `query` 和可选的 `tags`（标签名称数组）。标签用于优先检索，
  未命中时平台会在配置的授权范围内扩大检索。每次并发查询三个库，所有请求头共用
  `RAG_API_TOKEN`，各请求体的 `knowledge_ids` 仅包含一个库 ID。三个 ID 分别通过
  `RAG_POLICY_KNOWLEDGE_ID`、`RAG_BUSINESS_KNOWLEDGE_ID`、`RAG_LITERATURE_KNOWLEDGE_ID`
  配置；缺失或重复 ID 时拒绝检索，不回退到全库查询。
  `tools.rag.knowledge_bases` 分别配置三个库，每库 `recall_top_n: 20` 控制接口召回数量，
  `top_n: 4` 控制按返回 `score` 降序筛选后的保留数量（均可独立调整）。同分保持原顺序，
  缺失或无法解析的分数按 0 处理，不足时返回实际数量。默认合计最多 12 条切片，不保证来自不同文件。
  召回阈值固定采用配置 `score: 0.6`，不再因结果少或为空而动态降低。结果按配置库顺序
  合并，保留各片段的知识库和文档信息；此路径直接使用接口分数，不调用模型重排，
  不做跨库排序或全局 top_n 截断。
  任一库请求失败时本次工具报错，不将失败误报为无结果。
- 数据库查询（`database_query`）：将自然语言问题转换成 Apache Doris 只读 SQL，并查询授权业务表。
- 观测分析（`observation_service`）：查询观测站空间分布、详情、状态、最新数据、趋势和时间区间数据，也支持通用注册空间要素查询。
- 地图操作（`map_action`）：通过 SSE 驱动前端地图执行标准可视化动作，不查询或修改业务数据。
- 断面专题分析（`dm_duanmian_analysis`）：发送断面专题分析出图参数，由前端打开独立浮层并查询出图。

框架还注册了技能加载（`load_skill`）和当前服务器时间（`CurrentServerTime`）两个工具。

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

项目内置“断面业务” Skill，当前已实现断面数据库查询和专题分析图两项能力。
数据查询、统计比较与描述性分析使用 `database_query`，专题分析图使用
`dm_duanmian_analysis`；底层数据库工具保持通用只读职责。

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

观测工具通过 `tools.spatial.base_url` 调用空间计算服务，并在请求头中携带
`SPATIAL_API_KEY`。密钥只配置在 `.env` 或部署环境中，不要写入 YAML。

### 通用空间要素查询（同一个 observation_service 工具）

新增三个 operation，原有站点操作和 AgentLoop 保持不变：

- `list_spatial_elements`：无其他参数，调用 `/api/spatial/v2/elements/list`，结果放在 `elements`。
- `get_spatial_element_detail`：传 `dataKey`，调用 `/api/spatial/v2/elements/detail`，获取字段、枚举及查询约束。
- `query_spatial_element`：传 `query` 和可选顶层 `render_map`，`query` 对应 `/api/spatial/v2/elements/query` 原始请求体。

通用查询保留 FeatureCollection、features、total、returned；空间查询默认自动分页取全并发送地图事件，属性查询需显式设置 `render_map=true` 才展示。
点要素使用 `MARKER_ADD`（观测站沿用 `observation-stations` 图层及站号），线面使用 `GEOJSON_ADD`；仅清除对应类别图层，保留选区。
自动展示强制 `includeGeometry=true` 且要求 `offset=0`；只查数据或补取文字记录时设置 `render_map=false`。文字结果可能只有首批，`map_result` 描述已提交的地图数据，不代表前端渲染回执。
`map_result.fetched` 为已取回记录数，`mapped` 为加入绘图指令的要素数，`missing_geometry` 为缺少几何的数量；`complete` 表示指令是否覆盖全部匹配要素，`render_confirmed=false` 表示没有前端渲染确认。不能根据 `returned`、`complete` 或事件数量声称地图已显示全部结果。
工具错误按 operation 区分“通用空间查询”和“观测站服务”，保留 HTTP 路径及服务错误说明；查询失败不代表无匹配数据。
字段及查询约束以目录详情为准，由空间服务最终校验。
复用 `tools.spatial` 和 `SPATIAL_API_KEY`，base_url 继续只配置主机与端口，不附加 v2 路径。

断面及其测点的位置、属性、历史观测和统计统一使用 `database_query`；通用目录过滤 `dm_duanmian`，工具拒绝其详情及查询操作。专题出图使用 `dm_duanmian_analysis`。
明确要求地图展示断面测点时，按“断面业务” Skill 查询真实点位，再调用 `map_action(MARKER_ADD)`，使用独立图层 `transect-stations`；替换结果只清除该图层，保留选区和其他类别图层。数据库结果不会自动绘图，专题分析浮层也不代替测点地图分布。
已有环境若发布过旧 Skill 名称或断面转空间查询的提示词，应同步更新为先加载“断面业务” Skill，再按其说明选择工具；代码不会覆盖线上已发布版本。

### 观测站地图联动事件

`observation_service` 执行 `search_stations` 成功后，会在工具完成事件之前发送
一组 `map_action`。当前动作协议版本为 `1.0`，首版支持：

- `CLEAR`：先清除旧的 `observation-stations` 图层，空结果也会发送。
- `MARKER_ADD`：添加站点标记，并通过 `fit_bounds=true` 自动适配视野。
- 单站详情、状态、最新观测、趋势和区间分析会清除旧站点，只保留当前站点标记，
  并通过 `popup_content` 携带可点击查看的站点与观测摘要。
- 单站联动按 `CLEAR → MARKER_ADD → FLY_TO` 执行，保留带观测详情的真实站点标记。
- 独立调用通用 `FLY_TO` 时，后端会展开为 `CLEAR → MARKER_ADD → FLY_TO`；
  使用 `center`（兼容输入 `coordinates`）和 `zoom`，并只保留最新定位点。
- 通用 `map_action` 工具支持地图操作定义中的全部动作类型。

```text
event: map_action
data: {
  "code": 200,
  "message": "success",
  "data": {
    "version": "1.0",
    "conversation_id": "conversation-1",
    "run_id": "run-1",
    "batch_id": "call-observation-1",
    "action_id": "call-observation-1-1",
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
| `analysis` | 必填：`depth-curve` 沿水深曲线、`temp-section` 化学要素剖面、`ts-scatter` T-S 点聚图、`nutrient` 营养盐结构、`do-profile` 溶解氧垂向分布、`surface` 表层沿程变化 |
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
用户只要求统计或文字分析时仍走 `database_query`；仅要求出图时无需先查询数据库。

后续新增观测、海域等分析时，按现有方式增加独立工具模块，在
`agents/tools/registry.py` 注册，并在 `services/chat_service.py` 添加中文展示名。
新动作继续使用各自 operationId 和 payload，前端增加对应处理分支即可；不需要修改执行器或 SSE 编码器。

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
前端仍可显式发送上述清空字段。普通 CLEAR、单站自动定位、适配视野和清理标记图层不清除会话选区。
查询本轮新框选范围时保留新选区。前端按既有 CLEAR 契约处理地图清理；后端不等待前端渲染回执。
前端普通追问必须省略 `map_context`，否则重复提交的旧几何会按新选区重新保存。
较早请求迟到的选区写入不能覆盖较新请求的选区状态。
用户明确指定的地点优先于默认选区；除非用户明确询问或比较选区，回答不主动复述框选范围、坐标或无关的历史站点结果。
这些回答规则通过提示词约束，输出端不另做选区内容过滤。单站查询自动定位；
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
        "citation_id": 1,
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
渲染。

RAG 片段同时会在单次请求内注册为引用来源。模型使用内部 `<<SRC_n>>` 来源键，
后端将合法来源键转换为 `[[cite:n]]`，删除无效引用，并在 `done.sources` 中仅返回
正文实际引用的来源，以及正文明确点名的本轮检索文章：

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

回答明确提及本轮检索文章的完整名称时，即使没有引用标记，也会补入 `done.sources`。
匹配忽略文件扩展名、空白及全半角差异；书名号内按完整名称匹配，无书名号时仅匹配
至少 6 个字符的完整名称。不使用片段标题或主题关键词猜测文章，补入时同一文章只选一项，
已有正文引用的文章不重复补入，回答正文不追加标记。

`citation_id` 只在本轮回答内有效。同一文档同一页的多个 chunk 共用一个编号，
其文本以空行连接后写入 `source.preview`。会话历史消息会保存最终引用来源，因此
重新打开会话时仍可恢复引用卡片；生产环境的 MySQL 仓库可跨服务重启保留。

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
