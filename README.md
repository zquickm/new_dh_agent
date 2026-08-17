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

## RAG 与数据库工具

Agent 已注册两个异步工具：

- `rag_search`：检索内部知识库并返回文档片段和来源。
- `database_query`：将自然语言问题转换成只读 SQL，并查询授权业务表。

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
```

数据库 SQL 生成默认复用 `llm` 配置；仅当需要独立鉴权模型时填写
`DEEPSEEK_API_KEY`，并在 `tools.database.sql_llm` 中配置独立模型地址和名称。
数据库账号必须是专用只读账号，只授予 `SELECT` 权限。

数据库工具仅允许查询 `database_information/` 中具有 DDL 文件的授权表。
修改表范围时，需要同时更新对应 DDL 和 `断面数据_AI_SQL查询指南.md`。

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
