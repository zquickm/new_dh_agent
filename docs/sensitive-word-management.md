# 敏感词管理与一次性词库迁移

## 运行方式

默认 `sensitive_words.backend=csv`，继续读取 `config/sensitive_words.csv`，修改文件自动重载。
`mysql` 模式使用本项目独立数据库和 Redis；原有 Doris 只读业务连接不参与词库管理。
通用规则在两种模式下均生效，顺序为自伤关怀、词库匹配、辱骂或人身攻击请求。
这些是确定性关键词规则，不是语义审核；历史、工具返回和模型输出不在检测范围内。

普通命中仍返回“您的输入包含敏感词，请修改后重新提问。”。
关怀和拒答通过 `self_harm_reply`、`abuse_reply`、`blocked_reply` 配置。
`risk_rules_enabled=false` 仅关闭通用规则；`enabled=false` 关闭所有输入检测。
聊天的 `conversation → token → done` 契约保持不变，命中不保存问答和地图上下文。
词库完全不可用时返回可重试的 `SENSITIVE_FILTER_UNAVAILABLE` SSE error，不进入 Agent。
聊天接口先校验网关用户身份及已有会话的归属；网关需写入 `X-User-Id` 和
`X-Auth-Source: dh-gateway`。会话历史只读取当前用户的服务端记录，不采信客户端 `chat_history`。

## 配置独立存储

先由部署方准备独立的 MySQL 8.0+ 数据库和 Redis 7+，数据库字符集为 utf8mb4。
运行账号需要查询、插入、更新权限；建表迁移由具备 DDL 权限的账号显式执行。
应用启动不会创建数据库或表。Redis 键为 `new_dh_agent:sensitive:agent`。
MySQL 建连和等待连接池超时默认 5 秒，每条 SQL（含提交、回滚）超时默认 10 秒，
可通过 `mysql.connect_timeout/query_timeout` 调整；超时或请求取消后丢弃当前连接。

以下环境变量可写入项目 `.env`；真实密码、令牌通过部署环境提供，不要提交。

| 变量 | 用途 / 默认值 |
|---|---|
| `NEW_DH_SENSITIVE_WORDS__BACKEND` | `csv` 或 `mysql`，默认 `csv` |
| `NEW_DH_SENSITIVE_WORDS__MYSQL__HOST` | MySQL 主机，默认 `127.0.0.1` |
| `NEW_DH_SENSITIVE_WORDS__MYSQL__PORT` | MySQL 端口，默认 `3306` |
| `NEW_DH_SENSITIVE_WORDS__MYSQL__USER` | 运行账号，默认 `sensitive_word_user` |
| `NEW_DH_SENSITIVE_WORDS__MYSQL__PASSWORD` | 运行账号密码，MySQL 模式必填 |
| `NEW_DH_SENSITIVE_WORDS__MYSQL__DATABASE` | 数据库，默认 `new_dh_agent` |
| `NEW_DH_SENSITIVE_WORDS__REDIS__URL` | Redis URL，支持 `rediss://`，默认 `redis://127.0.0.1:6379/0` |
| `NEW_DH_SENSITIVE_WORDS__ADMIN_TOKEN` | 独立管理密钥；未配置时管理功能不可用 |

安装锁定依赖，为 MySQL 8 密码认证和 Redis 异步访问提供驱动：

```bash
python -m pip install -r requirements.txt
```

在已创建的独立数据库中执行迁移；将主机、账号和库名替换为部署参数，密码在提示中输入：

```bash
mysql --host=127.0.0.1 --port=3306 --user=migration_user -p new_dh_agent < migrations/001_sensitive_words.sql
```

## 从对方 API 一次性复制词库

来源固定为对方 `phase2/phase2_extra` API 的 `agent` 场景，不查询社区词库，不写入对方系统。
源站应在导出期间暂停词库编辑。程序两次读取并比较完整结果，检测到变化会失败；这不是源数据库事务快照。
导出的 JSON 保留分类、词条和启停状态，剔除创建人等无关字段；令牌不会写入文件或错误消息。

导出脚本只读取进程环境变量（不会自动读取 `.env`）：

- `SOURCE_SENSITIVE_BASE_URL`：已部署后端的根地址，包含实际 context path；不是 `.git` 仓库地址。
- `SOURCE_SENSITIVE_TOKEN`：具备词库查询权限的登录令牌，发送为 `X-Access-Token`。

脚本调用 `GET /agent/sensitive/category/list` 和分页的 `GET /agent/sensitive/word/list`。
实际源站地址及令牌尚需部署方提供；项目中不内置对方真实词库。

创建已被 Git 忽略的导出目录，以保存完整词库快照：

```bash
mkdir -p var/sensitive-word-exports
```

使用已配置在进程环境中的源站地址和令牌，导出一份完整 JSON 快照：

```bash
python scripts/export_sensitive_words.py --output var/sensitive-word-exports/agent.json --source-system hk-ai-tutor
```

配置本地 MySQL 连接并将后台数据源设为 `mysql` 后，先执行只读预演检查数量和冲突：

```bash
python scripts/import_sensitive_words.py --input var/sensitive-word-exports/agent.json
```

预演通过后，事务导入数据并刷新 Redis，使已启用的词条参与检测：

```bash
python scripts/import_sensitive_words.py --input var/sensitive-word-exports/agent.json --apply
```

来源标识应保持稳定。相同来源 ID 再次导入会跳过，包含本地已编辑或删除的记录；不会覆盖本地维护结果。
独立来源记录若与本地同名分类或同分类内规范化词条冲突，导入会失败，不会自动合并或部分提交。
不定期同步源站后续修改。仓库内 4 个 CSV 示例词不会自动混入正式词库。

若输出 `persisted=true` 且刷新失败，数据已经提交，应重试管理刷新接口；不要把错误理解为数据库回滚。
确认本地分类、词条、启停数量及抽样匹配结果后，再将实例切换到 MySQL 模式对外提供服务。

## 管理 API

前缀 `/internal/sensitive/agent`；每个请求需要现有网关头 `X-DH-Internal-Token` 和独立头 `X-DH-Admin-Token`。
仅 `agent` 为有效场景。未配置管理密钥或 CSV 后端返回 503；密钥错误返回 403。
响应沿用 `{code,message,data}`。本地分类/词条 ID 为正整数，与来源 ID 分开。

| 方法及相对路径 | 请求与行为 |
|---|---|
| `GET /categories` | 返回未删除分类，包含启用和禁用分类 |
| `POST /categories` | `{"name":"分类名称","status":1}` |
| `PATCH /categories/{id}` | 部分修改 `name/status`，至少一项 |
| `DELETE /categories/{id}` | 软删除分类及所属词条 |
| `GET /words` | `page/page_size/word/category_id/status`，每页最多 200 |
| `POST /words` | `{"category_id":1,"word":"词条","status":1}` |
| `PATCH /words/{id}` | 部分修改 `category_id/word/status`，至少一项 |
| `DELETE /words/{id}` | 软删除词条 |
| `POST /words/import` | `{"category_id":1,"csv_text":"word\n词条\n","dry_run":true}` |
| `POST /check` | `{"text":"待检测文本"}`，返回动作、规则、命中词及版本 |
| `POST /refresh` | 返回版本和词条数量，不返回完整词库 |

分类名最多 128 字符，词条最多 255 字符，状态严格为整数 0/1。
词条及请求先做 NFKC/casefold，匹配为普通子串检测；同分类内规范化去重，不同分类允许相同词条。
导入 CSV 必须有唯一的 `word` 列，默认仅预演；无效行会报告行号并阻止本批写入。
管理变更成功提交后自动刷新缓存。刷新失败响应为 HTTP 503，`data` 携带 `persisted=true,refreshed=false,version`。

## 运行与回滚

每次检测异步读取 Redis 快照，缓存缺失时从 MySQL 重建。
Redis 故障时已有进程保留最后有效词库；冷启动可从 MySQL 加载；两者均无法提供词库时启动失败。
缓存版本单调更新，不允许迟到的旧快照覆盖新版本。降级期间可能继续使用旧版本，恢复后读取最新已发布快照。

将 `NEW_DH_SENSITIVE_WORDS__BACKEND` 改回 `csv` 并重启，可以切回本地词库；需预先准备有效的 CSV，默认示例文件不是正式词库。
源 API 令牌只供一次性导出，应用运行无需继续访问源站。
