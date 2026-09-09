# New DH AgentLoop

精简的 FastAPI + AgentLoop 流式对话后端，接口兼容现有 `dh-frontend`。

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

`X-Username`、`X-Token-Id` 不作为归属依据，因此修改用户名或重新登录不会改变
会话所属用户。请求体和查询参数中的 `user_id`、`userId` 不能覆盖网关身份。

- `POST /conversations`：为当前用户创建会话。
- `GET /conversations`：仅返回当前用户的会话，先按用户过滤再排序、限制条数。
- `GET /conversations/{id}`：仅允许查看自己的会话。
- `POST /agent/chat/stream`：只允许续写自己的会话；未传 `conversation_id` 时，
  自动为当前用户创建会话，通过首个 `conversation` SSE 事件返回 ID。
- 历史消息、引用来源、地图选区上下文和消息写入均按用户与会话 ID 隔离。

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

当前会话仍使用进程内存存储：重启后历史清空，不同 worker/实例之间不共享。
此改动没有增加数据库持久化，暂应使用单 worker；若需要重启恢复或多实例部署，
需后续增加带 `user_id` 归属字段的共享持久化存储。

当前大模型服务不需要鉴权时，`NEW_DH_LLM__API_KEY` 可以留空或省略；如果服务需要鉴权，再配置真实 Key：

```dotenv
NEW_DH_LLM__API_KEY=your-key
```

## 运行

生产环境基线为 CPython 3.9。请使用 3.9.x 创建虚拟环境，避免使用其他 Python 版本生成生产 wheelhouse。

```powershell
.\.venv\Scripts\python.exe main.py
```

流式接口为 `POST /agent/chat/stream`。会话只存放在当前进程内存中，服务重启后清空。

## RAG、数据库、观测与地图工具

Agent 已注册以下业务工具（括号内为协议调用标识）：

- 东溟知识库检索（`rag_search`）：检索内部知识库并返回文档片段和来源。
- 数据库查询（`database_query`）：将自然语言问题转换成 Apache Doris 只读 SQL，并查询授权业务表。
- 观测分析（`observation_service`）：查询观测站空间分布、详情、状态、最新数据、趋势和时间区间数据。
- 空间要素查询（`spatial_query`）：发现注册目录、读取字段定义、按属性和选区查询断面测点等要素，并自动标记有效 Point 结果。
- 断面概况、最新观测、观测明细和航次（`dm_duanmian_overview`、`dm_duanmian_latest`、`dm_duanmian_observations`、`dm_duanmian_voyages`）：调用伙伴断面查询接口。
- 断面专题分析（`dm_duanmian_analysis`）：校验参数后，通过现有地图 SSE 打开分析面板；支持六种专题图。
- 地图操作（`map_action`）：通过 SSE 驱动前端地图执行标准可视化动作，不查询或修改业务数据。

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

同义词快照用于 RAG 检索问题扩展。敏感词快照中的词条用于用户输入检查，文本
仍会执行 Unicode NFKC 和英文大小写归一化。目前保持原有处理逻辑，不区分
`applyScope`、`action` 和 `matchType`。

命中后不会调用大模型或业务工具，也不会将原始问题写入会话历史；
接口仍按 `conversation → token → done` 顺序返回 SSE，固定提示为
“您的输入包含敏感词，请修改后重新提问。”。敏感词拦截开关由
`sensitive_words.enabled` 配置；服务地址和缓存目录分别由 `application.yaml`
中的 `dictionaries.base_url`、`dictionaries.cache_directory` 配置。内部调用
复用 `internal_request` 中的请求头名称和令牌。

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

项目内置“断面数据库查询” Skill，负责断面历史调查数据的查询路由、
条件组织和结果解释；底层 `database_query` 保持为通用只读数据库工具。

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

### 地图选区上下文

通用空间查询复用同一个选区入口和 `event: map_action`，不解析模型正文中的
`json.map_actions`，也不调用虚拟地图 HTTP 接口。已有业务观测站继续由
`observation_service` 处理，固定断面查询由 `dm_duanmian_*` 处理，复杂统计继续由 `database_query` 处理。

`spatial_query` 的 `operation` 支持 `list`、`detail`、`query`，对应空间服务
`/api/spatial/v2/elements/list`、`/detail?dataKey=...` 和 `/query`。
地址和鉴权复用 `tools.spatial` 与 `SPATIAL_API_KEY`，无需新增配置或依赖。
`detail/query` 需要 `element`；属性条件和返回字段必须来自目录详情。
圆形、多边形和矩形范围由工具转换为 `dwithin`、`intersects` 和 `bbox`。

查询工具输入示例（模型工具参数，不是新的 HTTP 请求体）：

```json
{
  "operation": "query",
  "element": "dm_duanmian",
  "area": {
    "query_type": "circle",
    "center": [122.5, 31.3],
    "radius": 25000,
    "crs": "EPSG:4490"
  },
  "limit": 20
}
```

query 会读取并校验目录定义，默认使用注册的展示字段、主键排序，限制返回条数。
绘图请求自动携带 `includeGeometry=true`，以 `spatial-{dataKey}` 为独立图层，
先 CLEAR 再合并发送 MARKER_ADD，`fit_bounds=true`；空结果只清理该图层。
标记 ID 使用 `{dataKey}:{真实主键}`；type 优先使用结果属性中的非空 type，
否则使用服务返回并验证的 dataKey 作为通用要素类型标识。前端需要支持该类型的通用点标记；
专用图标与类型映射需在联调环境确认。

当前自动绘制仅支持具有真实 ID 和有效 EPSG:4490 坐标的 Point；线面和缺失几何仍保留
查询记录，但不会生成假坐标。返回的 total、returned 和 map_count 分别代表匹配总数、
本页记录数与实际标记数；partial 和 has_more 提示分页状态，unmapped_count 表示未绘制记录数。
默认标图只包含本页，后续页不会自动累计到地图；`render_map=false` 可用于候选解析或
补充分页读取，不改变页面。查询失败不会清除当前图层。

通用空间查询保持现有 SSE 事件和动作；断面专题面板通过追加的 DUANMIAN_ANALYSIS 动作接入，见下方说明。

`/agent/chat/stream` 接受前端通过 `map_context.drawn_geometry` 传入 GeoJSON
`Polygon`，或传入由 `center` 和 `radius` 表示的圆形选区。
圆心坐标格式为 `[经度, 纬度]`，半径单位为米。后端会在接口边界校验坐标并转换为
Agent 可使用的内部上下文；
`AgentLoop`、LLM 客户端和工具不直接依赖前端字段名。
同一 `conversation_id` 会保存最近一次有效选区；后续请求未再次携带几何数据时会沿用该选区，
传入新选区时覆盖旧选区。选区状态不会写入用户消息正文。

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

### 断面观测与专题分析

四个查询工具沿用伙伴的 operationId，使用现有 `tools.spatial` 地址和服务端 API Key：

| 工具 | 上游路径（GET） | 入参 |
| --- | --- | --- |
| dm_duanmian_overview | /api/spatial/v2/duanmian/overview | 无，统计范围 E1/E2/E3 |
| dm_duanmian_latest | /api/spatial/v2/duanmian/latest | code |
| dm_duanmian_observations | /api/spatial/v2/duanmian/observations | code、from、to、page、size |
| dm_duanmian_voyages | /api/spatial/v2/duanmian/voyages | code |

查询 code 归一化大小写但保留连字符，支持上游提供的断面/测点；响应的 observedAt、depthM、
elements、chemistryFields/hydrometFields 保持原值，不将缺测填零。
latest 是逐站最新快照，不是统一航次；observations 按三个自然月上限校验（月末截到目标月末），
保留用户起止日，默认 size=100、最多 1000，通过 returned、partial、has_more 区分分页。
上游更严格的范围限制或编码归一化问题仍需现场联调，后端不会自动缩短时间或切换数据源。
复杂聚合、跨期分析继续使用 database_query，不把明细第一页交给模型做全量统计。

模型工具参数示例：

```json
{"code":"E1","from":"2025-11-01","to":"2025-11-30","page":1,"size":100}
```

`dm_duanmian_analysis` 支持 depth-curve、temp-section、ts-scatter、nutrient、do-profile、surface。
调用时不传 voyage 只请求打开面板；自动出图需有效航次和该图对应的必要参数。
最新航次先通过 voyages 取得，分析工具发送前再次核对。月份有多个观测日时不自行合并；
ts-scatter 固定 E1/E2/E3，并核对共同观测日。自动出图的单指标通过最新观测响应字段清单
确认列名，此步骤不把最新快照数据当作绘图航次数据。

```json
{"analysis":"temp-section","section":"E1","voyage":"2025-11-01","element":"温度"}
```

工具产生现有 `map_action` SSE，追加 `DUANMIAN_ANALYSIS` 动作，payload 保留伙伴的
`analysis/section/voyage/elementType/element` 字段；不调用虚拟分析 HTTP 接口。
当前后端没有前端源码，面板由伙伴维护：需按《地图操作SSE事件契约》第十部分增加消费分支，
复用既有面板的取数和绘图逻辑。后端完成推送不代表页面已完成渲染。
“出图并解释”需要另外查询相同口径的实测数据；没有面板状态回传时不能得知用户手动改图后的参数。

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
正文实际引用的来源：

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

`citation_id` 只在本轮回答内有效。同一文档同一页的多个 chunk 共用一个编号，
其文本以空行连接后写入 `source.preview`。会话历史消息会保存最终引用来源，因此
当前服务进程内重新打开会话时仍可恢复引用卡片；内存仓库本身不会跨服务重启保留。

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

```powershell
$env:NEW_DH_LLM__API_KEY = "test-key"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
