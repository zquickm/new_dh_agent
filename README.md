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

Agent 已注册四个业务工具：

- `rag_search`：检索内部知识库并返回文档片段和来源。
- `database_query`：将自然语言问题转换成只读 SQL，并查询授权业务表。
- `observation_service`：查询观测站空间分布、详情、状态、最新数据、趋势和时间区间数据。
- `map_action`：通过 SSE 驱动前端地图执行标准可视化动作，不查询或修改业务数据。

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

`/agent/chat/stream` 接受前端通过 `map_context.drawn_geometry` 传入 GeoJSON
`Polygon`，或传入由 `type: "Circle"`、`center` 和 `radius` 表示的圆形选区。
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
      "type": "Circle",
      "center": [121.8, 30.4],
      "radius": 25000,
      "crs": "EPSG:4490"
    }
  }
}
```

前端不要再把 `[系统上下文]` 或外接矩形说明拼入 `question`，否则会造成上下文
重复，并污染保存的用户消息。`conversation_id` 应来自后端创建会话接口；若省略，
流式接口会创建新会话，并在首个 `conversation` 事件中返回 ID。

数据库 SQL 生成默认复用 `llm` 配置；仅当需要独立鉴权模型时填写
`DEEPSEEK_API_KEY`，并在 `tools.database.sql_llm` 中配置独立模型地址和名称。
数据库账号必须是专用只读账号，只授予 `SELECT` 权限。

数据库工具仅允许查询 `database_information/` 中具有 DDL 文件的授权表。
修改表范围时，需要同时更新对应 DDL 和 `断面数据_AI_SQL查询指南.md`。

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
    "tool_name": "rag_search",
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
