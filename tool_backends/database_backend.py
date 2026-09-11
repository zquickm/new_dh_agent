from __future__ import annotations

import asyncio
import json
import math
import os
import re
import ssl
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from sqlglot import exp, parse
from sqlglot.dialects.doris import Doris
from sqlglot.errors import ParseError


class DatabaseToolError(RuntimeError):
    """A safe error raised by the database tool backend."""

    error_type = "database"

    def __init__(
        self, message: str, *, code: Optional[int] = None,
        sql: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.sql = sql


class DatabaseConnectionError(DatabaseToolError):
    """The database server could not be reached or authenticated."""

    error_type = "connection"


class SqlGenerationError(DatabaseToolError):
    """The SQL generation service failed before returning usable SQL."""

    error_type = "sql_generation"


class SqlValidationError(DatabaseToolError):
    """Generated SQL failed the local read-only policy."""

    error_type = "sql_validation"


class SqlExecutionError(DatabaseToolError):
    """The database rejected SQL that the model may be able to repair."""

    error_type = "sql_execution"


class DatabaseInformationCatalog:
    """Read the trusted query guide and authorized per-table DDL files."""

    def __init__(
        self,
        root: Path,
        *,
        guide_filename: str,
        max_tables: int = 20,
        max_content_chars: int = 50000,
    ) -> None:
        self.root = root.resolve()
        self.guide_path = (self.root / guide_filename).resolve()
        self.max_tables = max_tables
        self.max_content_chars = max_content_chars
        if not self.root.is_dir():
            raise DatabaseToolError(
                f"Database information directory does not exist: {self.root}"
            )
        if not self.guide_path.is_file() or self.guide_path.parent != self.root:
            raise DatabaseToolError(
                f"Database query guide does not exist: {self.guide_path}"
            )

        self._table_paths: dict[str, Path] = {}
        for candidate in self.root.glob("*.sql"):
            path = candidate.resolve()
            name = candidate.stem.casefold()
            if path.parent != self.root or name == "documents_ddl" or "_bak" in name:
                continue
            self._table_paths[name] = path
        if not self._table_paths:
            raise DatabaseToolError("No authorized table DDL files were found")
        if len(self._table_paths) > self.max_tables:
            raise DatabaseToolError("Too many authorized table DDL files")

    @property
    def available_tables(self) -> tuple[str, ...]:
        return tuple(sorted(self._table_paths))

    def read_guide(self) -> str:
        guide = self._read_text(self.guide_path).strip()
        if not guide or len(guide) > self.max_content_chars:
            raise DatabaseToolError("Database query guide is empty or too large")
        tables = "、".join(sorted(self._table_paths))
        return f"{guide}\n\n## 当前授权表\n{tables}"

    def read_all_schemas(self) -> str:
        blocks: list[str] = []
        total = 0
        for table_name, path in sorted(self._table_paths.items()):
            ddl = self._read_text(path).strip()
            block = f"### {table_name}\n```sql\n{ddl}\n```"
            total += len(block)
            if total > self.max_content_chars:
                raise DatabaseToolError("Authorized table DDL content is too large")
            blocks.append(block)
        return "\n\n".join(blocks)

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise DatabaseToolError(f"Unable to read database information: {path.name}") from exc


class _ReadonlyDoris(Doris):
    class Parser(Doris.Parser):
        # SQLGlot 30.12 misreads DISTINCT separators and reorders ORDER BY.
        # Preserve Doris GROUP_CONCAT arguments while still inspecting its AST.
        FUNCTIONS = {
            **Doris.Parser.FUNCTIONS,
            "GROUP_CONCAT": lambda args: exp.Anonymous(
                this="GROUP_CONCAT", expressions=args,
            ),
        }


class ReadonlySqlValidator:
    """Allow one read-only Doris query and enforce a bounded LIMIT."""

    def validate(
        self,
        sql: str,
        maximum_rows: int,
        allowed_tables: tuple[str, ...],
    ) -> str:
        text = sql.strip()
        if not text:
            raise SqlValidationError("SQL 不能为空")
        try:
            statements = [item for item in parse(text, read=_ReadonlyDoris) if item is not None]
        except ParseError as exc:
            raise SqlValidationError("SQL 无法按 Doris 方言解析") from exc
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise SqlValidationError("只允许执行一条只读 SELECT 或 WITH ... SELECT 查询")
        expression = statements[0]
        if list(expression.find_all(exp.Into)):
            raise SqlValidationError("不允许 SELECT INTO")
        if list(expression.find_all(exp.Lock)):
            raise SqlValidationError("不允许加锁查询")

        cte_names = {
            str(item.alias_or_name).strip().casefold()
            for item in expression.find_all(exp.CTE)
            if str(item.alias_or_name).strip()
        }
        referenced_tables = {
            str(item.name).strip().casefold()
            for item in expression.find_all(exp.Table)
            if str(item.name).strip()
        }
        unauthorized_tables = referenced_tables - set(allowed_tables) - cte_names
        if unauthorized_tables:
            raise SqlValidationError(
                "SQL 引用了未授权表：" + "、".join(sorted(unauthorized_tables))
            )

        blocked_functions = {"BENCHMARK", "LOAD_FILE", "SLEEP"}
        used_blocked_functions: set[str] = set()
        for function in expression.find_all(exp.Func):
            name = str(getattr(function, "name", "") or function.sql_name()).upper()
            if name in blocked_functions:
                used_blocked_functions.add(name)
        if used_blocked_functions:
            raise SqlValidationError(
                "SQL 使用了禁止函数：" + "、".join(sorted(used_blocked_functions))
            )

        # Fetch one extra row so the result can report whether it was truncated.
        requested = maximum_rows + 1
        existing = expression.args.get("limit")
        if existing is not None:
            value = existing.expression
            if isinstance(value, exp.Literal) and value.is_int:
                existing_value = int(value.this)
                if existing_value >= 0:
                    requested = min(requested, existing_value)
        return expression.limit(requested, copy=True).sql(
            dialect=_ReadonlyDoris,
            pretty=False,
        )


class MysqlReadonlyGateway:
    """Execute validated SQL through a reusable asyncmy connection pool."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        connect_timeout: float,
        query_timeout: float,
        configured_max_rows: int,
        allowed_tables: tuple[str, ...],
        pool_max_size: int,
        ssl_enabled: bool,
        ssl_ca: Optional[str],
        ssl_verify_cert: bool,
    ) -> None:
        if not host or not user or not password or not database:
            raise DatabaseToolError(
                "BUSINESS_DB_HOST/BUSINESS_DB_USER/BUSINESS_DB_PASSWORD/"
                "BUSINESS_DB_NAME are required"
            )
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.connect_timeout = connect_timeout
        self.query_timeout = query_timeout
        self.configured_max_rows = configured_max_rows
        self.allowed_tables = allowed_tables
        self.pool_max_size = pool_max_size
        self.ssl_enabled = ssl_enabled
        self.ssl_ca = ssl_ca
        self.ssl_verify_cert = ssl_verify_cert
        self.validator = ReadonlySqlValidator()
        self._pool: Optional[Any] = None
        self._pool_lock: Optional[asyncio.Lock] = None

    async def query(self, sql: str, limit: int) -> dict[str, Any]:
        effective_limit = min(max(1, limit), self.configured_max_rows)
        bounded_sql = self.validator.validate(
            sql,
            effective_limit,
            self.allowed_tables,
        )
        await self._get_pool()
        try:
            return await asyncio.wait_for(
                self._execute(bounded_sql, effective_limit),
                timeout=self.query_timeout,
            )
        except DatabaseToolError:
            raise
        except asyncio.TimeoutError as exc:
            raise DatabaseToolError("数据库查询超时，请缩小查询范围", sql=bounded_sql) from exc
        except Exception as exc:
            code = self._mysql_error_code(exc)
            if code is None:
                raise
            # Session setup/pool errors are not failures of the generated SQL.
            raise DatabaseToolError(self._safe_error(exc), code=code) from exc

    async def close(self) -> None:
        async with self._get_pool_lock():
            pool = self._pool
            self._pool = None
        if pool is not None:
            pool.close()
            await pool.wait_closed()

    async def _execute(self, sql: str, limit: int) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            async with connection.cursor() as cursor:
                timeout_seconds = max(1, math.ceil(self.query_timeout))
                await cursor.execute(f"SET query_timeout = {timeout_seconds}")
                try:
                    await cursor.execute(sql)
                    raw_rows = list(await cursor.fetchall())
                except Exception as exc:
                    code = self._mysql_error_code(exc)
                    if code is None:
                        raise
                    raise SqlExecutionError(
                        self._safe_error(exc), code=code, sql=sql,
                    ) from exc
                columns = self._column_names(cursor.description)

        rows = [
            {
                columns[index]: self._normalize_value(value)
                for index, value in enumerate(raw_row)
            }
            for raw_row in raw_rows[:limit]
        ]
        return {
            "sql": sql,
            "columns": list(columns),
            "rows": rows,
            "truncated": len(raw_rows) > limit,
        }

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        async with self._get_pool_lock():
            if self._pool is None:
                try:
                    import asyncmy
                except ImportError as exc:
                    raise DatabaseToolError("缺少 asyncmy 依赖") from exc
                options: dict[str, Any] = {
                    "host": self.host,
                    "port": self.port,
                    "user": self.user,
                    "password": self.password,
                    "db": self.database,
                    "charset": "utf8mb4",
                    "autocommit": True,
                    "connect_timeout": self.connect_timeout,
                    "minsize": 1,
                    "maxsize": self.pool_max_size,
                }
                if self.ssl_enabled:
                    options["ssl"] = self._ssl_context()
                try:
                    self._pool = await asyncio.wait_for(
                        asyncmy.create_pool(**options),
                        timeout=self.connect_timeout,
                    )
                except asyncio.TimeoutError as exc:
                    raise DatabaseConnectionError(
                        "数据库连接超时，请检查 host、port 和网络访问策略"
                    ) from exc
                except DatabaseToolError:
                    raise
                except OSError as exc:
                    raise DatabaseConnectionError(str(exc)) from exc
                except Exception as exc:
                    code = self._mysql_error_code(exc)
                    if code is None:
                        raise
                    raise DatabaseConnectionError(
                        self._safe_error(exc), code=code,
                    ) from exc
        return self._pool

    def _get_pool_lock(self) -> asyncio.Lock:
        if self._pool_lock is None:
            self._pool_lock = asyncio.Lock()
        return self._pool_lock

    def _ssl_context(self) -> ssl.SSLContext:
        if self.ssl_verify_cert:
            return ssl.create_default_context(cafile=self.ssl_ca)
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    @staticmethod
    def _column_names(description: Any) -> tuple[str, ...]:
        names: list[str] = []
        used: dict[str, int] = {}
        for index, item in enumerate(description or []):
            base = str(item[0] or f"column_{index + 1}")
            key = base.casefold()
            count = used.get(key, 0) + 1
            used[key] = count
            names.append(base if count == 1 else f"{base}_{count}")
        return tuple(names)

    @staticmethod
    def _normalize_value(value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, default=str)
        return str(value)

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        args = getattr(exc, "args", ())
        code = MysqlReadonlyGateway._mysql_error_code(exc)
        if code is not None and len(args) > 1:
            return str(args[1])
        return str(exc)

    @staticmethod
    def _mysql_error_code(exc: Exception) -> Optional[int]:
        args = getattr(exc, "args", ())
        return args[0] if args and isinstance(args[0], int) else None


class DatabaseQueryBackend:
    """Natural-language question -> SQL -> validation -> read-only query."""

    _SYSTEM_PROMPT = """你正在执行受控的 Apache Doris（MySQL 兼容方言）Text-to-SQL 任务。

必须遵守：
1. 只根据用户问题、数据库查询指南和授权 DDL 生成 SQL。
2. 只能输出一条 SELECT 或最终返回 SELECT 的 WITH 查询。
3. 不得输出解释、Markdown、注释或多条语句。
4. 不得生成 INSERT、UPDATE、DELETE、DROP、ALTER、CREATE、TRUNCATE、
   SELECT INTO、存储过程、文件输出或加锁语句。
5. 数据库查询指南是业务语义和查询组织规则的唯一依据，必须完整遵守。
6. 只能使用 DDL 中真实存在的表和字段；DDL 未定义的关系不得猜测。
7. 完整保留用户问题中的过滤条件和统计口径，不得自行改变查询目标。
8. 只使用 Apache Doris 与当前 Doris 校验器兼容的语法，禁止 FULL OUTER JOIN、
    INTERSECT、EXCEPT 和 QUALIFY。
   GROUP_CONCAT 的分隔符使用逗号参数（如 GROUP_CONCAT(DISTINCT field, ',')），不用 SEPARATOR。
9. 执行器最多返回 {max_rows} 行并检测截断；不要为迎合此上限主动添加 LIMIT，只有用户明确要求前若干条或分页时才使用 LIMIT。
10. 输出前确认括号成对、CTE 和别名已定义、字段均来自 DDL，且 UNION 分支列对齐。
"""

    def __init__(
        self,
        *,
        catalog: DatabaseInformationCatalog,
        gateway: MysqlReadonlyGateway,
        llm: Any,
        generation_timeout: float,
    ) -> None:
        self.catalog = catalog
        self.gateway = gateway
        self.llm = llm
        self.generation_timeout = generation_timeout

    @classmethod
    def from_env(
        cls,
        *,
        default_llm_base_url: str = "",
        default_llm_model: str = "",
        default_llm_api_key: str = "",
        default_llm_temperature: float = 0,
    ) -> "DatabaseQueryBackend":
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            raise DatabaseToolError("缺少 langchain-openai 依赖") from exc

        default_information_dir = Path(__file__).resolve().parent.parent / "database_information"
        root = Path(
            os.getenv("DATABASE_INFORMATION_DIR", str(default_information_dir))
        )
        guide_filename = os.getenv(
            "DATABASE_QUERY_GUIDE_FILE",
            "数据库_AI_SQL查询指南.md",
        ).strip()
        catalog = DatabaseInformationCatalog(
            root,
            guide_filename=guide_filename,
        )

        gateway = MysqlReadonlyGateway(
            host=os.getenv("BUSINESS_DB_HOST", "127.0.0.1").strip(),
            port=int(os.getenv("BUSINESS_DB_PORT", "3306")),
            user=os.getenv("BUSINESS_DB_USER", "").strip(),
            password=os.getenv("BUSINESS_DB_PASSWORD", "").strip(),
            database=os.getenv("BUSINESS_DB_NAME", "donghai_dwd").strip(),
            connect_timeout=float(os.getenv("BUSINESS_DB_CONNECT_TIMEOUT", "5")),
            query_timeout=float(os.getenv("BUSINESS_DB_QUERY_TIMEOUT", "10")),
            configured_max_rows=int(os.getenv("BUSINESS_DB_MAX_ROWS", "1000")),
            allowed_tables=catalog.available_tables,
            pool_max_size=int(os.getenv("BUSINESS_DB_POOL_MAX_SIZE", "3")),
            ssl_enabled=_env_bool("BUSINESS_DB_SSL_ENABLED", False),
            ssl_ca=os.getenv("BUSINESS_DB_SSL_CA", "").strip() or None,
            ssl_verify_cert=_env_bool("BUSINESS_DB_SSL_VERIFY_CERT", True),
        )

        generation_timeout = float(os.getenv("DATABASE_SQL_LLM_TIMEOUT", "120"))
        base_url = os.getenv("DEEPSEEK_BASE_URL", "").strip() or os.getenv(
            "LLM_BASE_URL", ""
        ).strip() or os.getenv(
            "NEW_DH_LLM__BASE_URL", ""
        ).strip() or default_llm_base_url.strip()
        model = os.getenv("DEEPSEEK_MODEL", "").strip() or os.getenv(
            "LLM_MODEL_ID", ""
        ).strip() or os.getenv(
            "NEW_DH_LLM__MODEL", ""
        ).strip() or default_llm_model.strip()
        if not base_url or not model:
            raise DatabaseToolError("DEEPSEEK_BASE_URL and DEEPSEEK_MODEL are required")
        llm = ChatOpenAI(
            api_key=(
                os.getenv("DEEPSEEK_API_KEY", "").strip()
                or os.getenv("LLM_API_KEY", "").strip()
                or os.getenv("NEW_DH_LLM__API_KEY", "").strip()
                or default_llm_api_key.strip()
                or "not-required"
            ),
            base_url=base_url,
            model=model,
            temperature=float(
                os.getenv("DEEPSEEK_TEMPERATURE", "").strip()
                or os.getenv("NEW_DH_LLM__TEMPERATURE", "").strip()
                or str(default_llm_temperature)
            ),
            timeout=generation_timeout,
            max_retries=1,
        )
        return cls(
            catalog=catalog,
            gateway=gateway,
            llm=llm,
            generation_timeout=generation_timeout,
        )

    async def query(self, question: str, limit: int) -> dict[str, Any]:
        question = question.strip()
        if not question:
            raise DatabaseToolError("question 不能为空")

        guide = self.catalog.read_guide()
        schemas = self.catalog.read_all_schemas()
        previous_sql: Optional[str] = None
        previous_error: Optional[str] = None

        # One original attempt and one error-guided retry.
        for attempt in range(2):
            sql = previous_sql
            try:
                sql = await self._generate_sql(
                    question=question,
                    limit=limit,
                    guide=guide,
                    schemas=schemas,
                    previous_sql=previous_sql,
                    previous_error=previous_error,
                )
                return await self.gateway.query(sql, limit)
            except (SqlValidationError, SqlExecutionError) as exc:
                previous_sql = exc.sql or sql
                previous_error = (
                    f"数据库错误 {exc.code}：{exc}" if exc.code is not None else str(exc)
                )
                if attempt == 1:
                    raise

        raise DatabaseToolError("数据库查询未成功")

    async def close(self) -> None:
        await self.gateway.close()

    async def _generate_sql(
        self,
        *,
        question: str,
        limit: int,
        guide: str,
        schemas: str,
        previous_sql: Optional[str],
        previous_error: Optional[str],
    ) -> str:
        retry_context = ""
        if previous_sql or previous_error:
            retry_context = (
                "\n\n上一次生成的 SQL：\n"
                f"{previous_sql or '无'}\n\n"
                "上一次错误：\n"
                f"{previous_error or '无'}\n"
                "请只针对该错误修复 SQL，不要改变查询目标。"
            )

        user_prompt = (
            f"用户问题：\n{question}\n\n"
            f"数据库查询指南：\n{guide}\n\n"
            f"全部授权表结构：\n{schemas}"
            f"{retry_context}"
        )
        messages = [
            {"role": "system", "content": self._SYSTEM_PROMPT.format(max_rows=limit)},
            {"role": "user", "content": user_prompt},
        ]
        try:
            response = await asyncio.wait_for(
                self.llm.ainvoke(messages),
                timeout=self.generation_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise SqlGenerationError("SQL 生成超时") from exc
        except Exception as exc:
            raise SqlGenerationError("SQL 生成服务不可用") from exc
        content = self._extract_content(getattr(response, "content", response))
        return self._parse_sql(content)

    @staticmethod
    def _extract_content(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "".join(parts).strip()
        return str(content).strip()

    @staticmethod
    def _parse_sql(content: str) -> str:
        text = content.strip()
        fenced = re.fullmatch(
            r"```(?:sql)?\s*(.*?)\s*```",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if fenced is not None:
            text = fenced.group(1).strip()
        if not re.match(r"^(SELECT|WITH)\b", text, re.IGNORECASE):
            raise SqlValidationError("SQL 模型没有返回 SELECT 或 WITH 查询")
        return text


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise DatabaseToolError(f"{name} must be true or false")
