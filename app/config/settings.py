import os
from pathlib import Path
from typing import Optional
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings
from app.utils.common import get_project_meta, normalize_path


# 定义全局配置常量
_meta = get_project_meta()
APP_NAME = _meta["name"]
APP_VERSION = _meta["version"]
APP_DESCRIPTION = _meta["description"]
APP_BASE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME_DATA_DIR = Path.home() / ".moma"


def _resolve_env_file() -> str:
    """统一使用 ~/.moma/env 作为配置文件（开发态与部署态相同）。"""
    default_env = str((DEFAULT_RUNTIME_DATA_DIR / "env").resolve())
    if os.path.exists(default_env):
        return default_env
    else:
        return str((REPO_ROOT / "env").resolve())

class Settings(BaseSettings):
    """应用配置类 - 平铺结构"""
    
    # 应用基础配置
    debug: bool = Field(default=False, description="调试模式", env="DEBUG")
    app_log_level: str = Field(default="INFO", description="日志级别", env="APP_LOG_LEVEL")

    # 运行时数据目录    
    runtime_data_dir: str = Field(default=str(DEFAULT_RUNTIME_DATA_DIR), description="运行时数据目录", env="RUNTIME_DATA_DIR")
    
    # 数据库配置
    db_name: str = Field(default="moma_coder_service", description="数据库名称", env="DB_NAME")
    database_type: str = Field(default="sqlite", description="数据库类型: postgresql/mysql/sqlite", env="DATABASE_TYPE")
    db_pool_size: int = Field(default=10, description="连接池大小", env="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=20, description="最大溢出连接数", env="DB_MAX_OVERFLOW")
    
    # PostgreSQL 配置
    postgresql_host: str = Field(default="localhost", description="PostgreSQL主机地址", env="POSTGRESQL_HOST")
    postgresql_port: int = Field(default=5432, description="PostgreSQL端口", env="POSTGRESQL_PORT")
    postgresql_user: str = Field(default="postgres", description="PostgreSQL用户名", env="POSTGRESQL_USER")
    postgresql_password: str = Field(default="your_password", description="PostgreSQL密码", env="POSTGRESQL_PASSWORD")
    
    # MySQL 配置
    mysql_host: str = Field(default="localhost", description="MySQL主机地址", env="MYSQL_HOST")
    mysql_port: int = Field(default=3306, description="MySQL端口", env="MYSQL_PORT")
    mysql_user: str = Field(default="root", description="MySQL用户名", env="MYSQL_USER")
    mysql_password: str = Field(default="your_password", description="MySQL密码", env="MYSQL_PASSWORD")

    # =============================================================================
    # 向量存储配置 - Vector Store
    # =============================================================================
    # 向量存储引擎类型 (lancedb, elasticsearch, opensearch)
    vector_store_engine: str = Field(default="lancedb", description="向量存储引擎类型", env="VECTOR_STORE_ENGINE")
    # 向量存储映射文件名称（elasticsearch/opensearch 使用）
    vector_store_mapping: str = Field(default="es_doc_mapping.json", description="向量存储映射文件名称", env="VECTOR_STORE_MAPPING")
    
    # Elasticsearch配置
    es_hosts: str = Field(default="https://localhost:9200", description="Elasticsearch主机地址", env="ES_HOSTS")
    es_username: str = Field(default="elastic", description="Elasticsearch用户名", env="ES_USERNAME")
    es_password: str = Field(default="changeme", description="Elasticsearch密码", env="ES_PASSWORD")
    es_verify_certs: bool = Field(default=False, description="是否校验 ES 服务端证书，本地 HTTPS 自签证书可设为 False", env="ES_VERIFY_CERTS")
    
    # OpenSearch配置
    os_hosts: str = Field(default="http://localhost:9200", description="OpenSearch主机地址", env="OS_HOSTS")
    os_username: str = Field(default="admin", description="OpenSearch用户名", env="OS_USERNAME")
    os_password: str = Field(default="admin", description="OpenSearch密码", env="OS_PASSWORD")

    # =============================================================================
    # 图数据库配置
    # =============================================================================
    neo4j_uri: str = Field(default="neo4j://localhost:7687", description="图数据库URI", env="NEO4J_URI")
    neo4j_user: str = Field(default="neo4j", description="图数据库用户名", env="NEO4J_USER")
    neo4j_password: str = Field(default="<REMOVED>", description="图数据库密码", env="NEO4J_PASSWORD")
    neo4j_pool_size: int = Field(default=5, description="连接池大小", env="NEO4J_POOL_SIZE")
    neo4j_max_overflow: int = Field(default=10, description="最大溢出连接数", env="NEO4J_MAX_OVERFLOW")


    # =============================================================================
    # 模型配置说明 见：app/config/xxx.json
    # =============================================================================


    # =============================================================================
    # Web 工具配置 - Web Search / Fetch
    # =============================================================================
    tavily_api_key: str = Field(default="", description="Tavily API密钥（搜索与抽取）", env="TAVILY_API_KEY")
    brave_api_key: str = Field(default="", description="Brave搜索API密钥", env="BRAVE_API_KEY")
    serper_api_key: str = Field(default="", description="Serper搜索API密钥", env="SERPER_API_KEY")
    firecrawl_api_key: str = Field(default="", description="Firecrawl API密钥", env="FIRECRAWL_API_KEY")
    jina_api_key: str = Field(default="", description="Jina Reader API密钥（可选，无Key时匿名限流）", env="JINA_API_KEY")
    web_search_primary: str = Field(default="tavily", description="web_search 主选: tavily | brave | serper | duckduckgo", env="WEB_SEARCH_PRIMARY")
    web_search_fallback: str = Field(default="brave", description="web_search 备选；留空表示不回退", env="WEB_SEARCH_FALLBACK")
    web_fetch_primary: str = Field(default="static", description="web_fetch 主选: static | tavily | firecrawl | jina", env="WEB_FETCH_PRIMARY")
    web_fetch_fallback: str = Field(default="tavily", description="web_fetch 备选；留空表示不回退", env="WEB_FETCH_FALLBACK")
    web_fetch_llm_extract: bool = Field(default=True, description="web_fetch 是否用会话模型按 prompt 二次抽取", env="WEB_FETCH_LLM_EXTRACT")

    # =============================================================================
    # Skills Hub
    # =============================================================================
    github_token: str = Field(default="", description="GitHub PAT，Skills Hub 拉取仓库文件", env="GITHUB_TOKEN")
    gh_token: str = Field(default="", description="GitHub PAT 别名", env="GH_TOKEN")

    # =============================================================================
    # Agent配置 - Agent 会话存储
    # =============================================================================
    enable_local_session_storage: bool = Field(default=False, description="为 True 时会话存本地文件，为 False 时存数据库", env="ENABLE_LOCAL_SESSION_STORAGE")
    enable_cron: bool = Field(default=True, description="当前进程是否运行 cron 调度循环；多进程部署时仅在一个进程设为 True，避免重复执行", env="ENABLE_CRON")
    enable_message_interrupt: bool = Field(default=True, description="同一会话新消息是否中断当前Agent，true=中断并优先处理新消息，false=排队等待", env="ENABLE_MESSAGE_INTERRUPT")
    agent_run_hard_kill_grace_sec: int = Field(default=15, description="/stop 软中止后等待协作退出的宽限(秒)，超时则 cancel run；/kill 不使用", env="AGENT_RUN_HARD_KILL_GRACE_SEC")
    enable_tool_result_truncate: bool = Field(default=True, description="工具结果超长保存到文件", env="ENABLE_TOOL_RESULT_TRUNCATE")
    tool_result_truncate_max_lines: int = Field(default=2000, description="工具结果截断最大行数", env="TOOL_RESULT_TRUNCATE_MAX_LINES")
    tool_result_truncate_max_bytes: int = Field(default=51200, description="工具结果截断最大字节数(50KB)", env="TOOL_RESULT_TRUNCATE_MAX_BYTES")
    enable_file_state_guard: bool = Field(default=True, description="跨 Actor 文件读写协调（file_state）", env="ENABLE_FILE_STATE_GUARD")
    file_tool_workspace_boundary_enabled: bool = Field(default=True, description="文件工具强制路径在 workspace/runtime_data_dir 内，防止越界读写；关闭后无此限制", env="FILE_TOOL_WORKSPACE_BOUNDARY_ENABLED")
    max_parallel_tool_workers: int = Field(default=8, description="Agent 单轮可并行执行的工具调用上限", env="MAX_PARALLEL_TOOL_WORKERS")
    lsp_enabled: bool = Field(default=True, description="是否启用 LSP（读写后诊断 / lsp 工具）", env="LSP_ENABLED")
    use_powershell_tool: Optional[bool] = Field(
        default=None,
        description="是否向 Agent 暴露 powershell 工具；未设置时 Windows 默认开，Linux/macOS 默认关",
        env="USE_POWERSHELL_TOOL",
    )

    # =============================================================================
    # 会话压缩 - Session Compaction
    # =============================================================================
    compaction_auto: bool = Field(default=True, description="上下文溢出时是否自动压缩会话", env="COMPACTION_AUTO")
    compaction_reserved: int = Field(
        default=13_000,
        description="为压缩预留的 token 缓冲",
        env="COMPACTION_RESERVED",
    )
    compaction_context_limit: int = Field(default=64_000, description="模型上下文上限(token)，用于溢出判断", env="COMPACTION_CONTEXT_LIMIT")
    compaction_keep_last_n: int = Field(
        default=10,
        description="触发压缩时保留的最近消息条数",
        env="COMPACTION_KEEP_LAST_N",
    )
    compaction_prune: bool = Field(default=True, description="是否启用旧工具输出修剪(prune/microcompact)", env="COMPACTION_PRUNE")
    compaction_prune_compactable_tools: str = Field(
        default=(
            "read_file,bash,powershell,shell_process,grep_search,glob_search,"
            "web_search,web_fetch,write_file,edit_file,apply_patch"
        ),
        description="可参与prune清空的工具名(逗号分隔)",
        env="COMPACTION_PRUNE_COMPACTABLE_TOOLS",
    )
    compaction_reactive_max_attempts: int = Field(
        default=3,
        description="上下文溢出后 reactive compact 最大重试次数（连续失败收紧 keep）",
        env="COMPACTION_REACTIVE_MAX_ATTEMPTS",
    )

    # =============================================================================
    # CodeBase 模块配置（来自 Moma-CodeBase）
    # =============================================================================
    codebase_enabled: bool = Field(
        default=True,
        description="CodeBase 总开关（关闭后停止所有扫描、向量化和图谱构建）",
        env="CODEBASE_ENABLED",
    )
    # =============================================================================
    # CodeGraph
    # =============================================================================
    code_graph_enabled: bool = Field(
        default=True,
        description="是否启用代码依赖图谱（分析 + related/图谱检索）",
        env="CODE_GRAPH_ENABLED",
    )
    code_analysis_related_include_graph: bool = Field(
        default=False,
        description="related/定位是否融入 CodeGraph（默认关：定位与关系分离；关系用 dependents/callers）",
        env="CODE_ANALYSIS_RELATED_INCLUDE_GRAPH",
    )
    code_graph_provider: str = Field(
        default="codegraph",
        description="CodeGraph 实现：codegraph（开源 CLI，默认）| builtin（自研 Neo4j）",
        env="CODE_GRAPH_PROVIDER",
    )

    # =============================================================================
    # 代码仓分析 - 行切片（codechunk/code_chunk）
    # =============================================================================
    # 四类能力开关：同时控制「分析入库」与对应「检索接口」（默认全开）
    # - Symbol → analyze 符号摘要向量 + search related
    # - Line chunk → analyze 行块向量 + search similar
    # - CodeGraph → analyze 图谱 + search related / dependents 等
    # - MR experience → experience analyze + search pattern
    code_analysis_line_chunk_enabled: bool = Field(
        default=True,
        description="是否启用行切片向量（分析 + 相似片段检索）",
        env="CODE_ANALYSIS_LINE_CHUNK_ENABLED",
    )
    code_analysis_line_chunk_target_lines: int = Field(
        default=5,
        description="行切片目标窗口行数",
        env="CODE_ANALYSIS_LINE_CHUNK_TARGET_LINES",
    )
    code_analysis_line_chunk_overlap_lines: int = Field(
        default=1,
        description="行切片滑动重叠行数",
        env="CODE_ANALYSIS_LINE_CHUNK_OVERLAP_LINES",
    )
    code_analysis_line_chunk_max_lines: int = Field(
        default=200,
        description="单行切片经扩展后的最大行数上限",
        env="CODE_ANALYSIS_LINE_CHUNK_MAX_LINES",
    )
    code_analysis_symbol_body_max_lines_function: int = Field(
        default=500,
        ge=1,
        description="符号体整段入库：函数/方法最大行数（超限跳过，交给行窗）",
        env="CODE_ANALYSIS_SYMBOL_BODY_MAX_LINES_FUNCTION",
    )
    code_analysis_symbol_body_max_lines_class: int = Field(
        default=120,
        ge=1,
        description="符号体整段入库：类最大行数（超限不切整类，仅尝试方法）",
        env="CODE_ANALYSIS_SYMBOL_BODY_MAX_LINES_CLASS",
    )
    code_analysis_embed_max_chars: int = Field(
        default=12000,
        ge=1,
        description="单条 embedding 文本最大字符数（超限跳过不截断；防超长行打爆 API）",
        env="CODE_ANALYSIS_EMBED_MAX_CHARS",
    )
    code_analysis_symbol_summary_llm_concurrency: int = Field(
        default=4,
        ge=1,
        le=32,
        description="符号摘要阶段 LLM 并发上限（批量时为并行批次数）",
        env="CODE_ANALYSIS_SYMBOL_SUMMARY_LLM_CONCURRENCY",
    )
    code_analysis_symbol_summary_llm_batch_size: int = Field(
        default=6,
        ge=1,
        le=32,
        description="符号摘要单次 LLM 打包符号数；1=逐条（旧行为）",
        env="CODE_ANALYSIS_SYMBOL_SUMMARY_LLM_BATCH_SIZE",
    )
    code_analysis_symbol_summary_enabled: bool = Field(
        default=True,
        description="是否启用符号 LLM 摘要与符号向量（分析 + related 检索）",
        env="CODE_ANALYSIS_SYMBOL_SUMMARY_ENABLED",
    )
    code_analysis_content_grep_enabled: bool = Field(
        default=True,
        description="是否启用仓库内全文/标识符 grep（resolve auto 并联通道）",
        env="CODE_ANALYSIS_CONTENT_GREP_ENABLED",
    )
    code_analysis_nl_to_code_enabled: bool = Field(
        default=False,
        description="是否启用 NL→Code 检索增强（多视角 embed、仓内词表、token 加权；默认关=档位 B）",
        env="CODE_ANALYSIS_NL_TO_CODE_ENABLED",
    )
    code_analysis_nl_rewrite_enabled: bool = Field(
        default=False,
        description="是否启用 NL→Code 查询 LLM 改写（需总开关开启；默认关）",
        env="CODE_ANALYSIS_NL_REWRITE_ENABLED",
    )
    code_analysis_nl_rewrite_mode: str = Field(
        default="weak",
        description="NL 改写触发：always=每次 NL；weak=仅 resolve 首轮召回弱时",
        env="CODE_ANALYSIS_NL_REWRITE_MODE",
    )
    code_analysis_file_worker_count: int = Field(
        default=10,
        ge=1,
        le=64,
        description="单仓库文件分析 worker 并发数",
        env="CODE_ANALYSIS_FILE_WORKER_COUNT",
    )

    # =============================================================================
    # MR 经验沉淀
    # =============================================================================
    mr_experience_enabled: bool = Field(
        default=True,
        description="是否启用 MR/合入经验沉淀与检索（experience analyze + search pattern）",
        env="MR_EXPERIENCE_ENABLED",
    )
    mr_experience_min_quality_score: float = Field(
        default=0.55,
        description="MR经验最小质量分（低于该分值会被丢弃）",
        env="MR_EXPERIENCE_MIN_QUALITY_SCORE",
    )
    mr_experience_merge_by_scenario: bool = Field(
        default=True,
        description="检索时是否按场景合并多条MR经验",
        env="MR_EXPERIENCE_MERGE_BY_SCENARIO",
    )    
    mr_experience_lookback_days: int = Field(
        default=730,
        ge=1,
        description="MR经验首次分析回看天数（默认2年）",
        env="MR_EXPERIENCE_LOOKBACK_DAYS",
    )
    mr_experience_max_collect_per_run: int = Field(
        default=5000,
        ge=1,
        description="单次收集 MR 条目上限（保护性，防止极端活跃仓拉爆 git log）",
        env="MR_EXPERIENCE_MAX_COLLECT_PER_RUN",
    )
    mr_experience_process_batch_size: int = Field(
        default=50,
        ge=1,
        description="单轮处理 PENDING 条目数（跨 tick 推进，支持多天完成）",
        env="MR_EXPERIENCE_PROCESS_BATCH_SIZE",
    )

    # =============================================================================
    # 仓库存储 / 增量扫描
    # =============================================================================
    enable_incremental_scan: bool = Field(
        default=True,
        description="程序运行后是否定时扫描已登记仓库变更并自动更新 repo/lib analysis 与 MR 经验",
        env="ENABLE_INCREMENTAL_SCAN",
    )
    incremental_scan_interval_sec: int = Field(
        default=300,
        ge=30,
        description="增量扫描间隔（秒）",
        env="INCREMENTAL_SCAN_INTERVAL_SEC",
    )
    resolve_channel_timeout_ms: int = Field(
        default=120000,
        ge=0,
        description="resolve 单通道超时毫秒；0=不限制。超时只丢该通道，其它通道仍融合",
        env="RESOLVE_CHANNEL_TIMEOUT_MS",
    )
    doctor_embed_probe_timeout_ms: int = Field(
        default=15000,
        ge=1000,
        description="doctor/setup 探测 embedding 的超时毫秒",
        env="DOCTOR_EMBED_PROBE_TIMEOUT_MS",
    )

    class Config:
        env_file = _resolve_env_file()
        env_file_encoding = "utf-8"
        extra = "ignore"

    @model_validator(mode="after")
    def _resolve_runtime_data_dir(self):
        """相对路径相对仓库根解析（避免任意 cwd 执行 moma 时 ./data 指错）。"""
        raw = Path(str(self.runtime_data_dir)).expanduser()
        if not raw.is_absolute():
            raw = REPO_ROOT / raw
        object.__setattr__(self, "runtime_data_dir", str(raw.resolve()))
        return self

    @property
    def repo_storage_path(self) -> str:
        """仓库存储目录：{runtime_data_dir}/repos。"""
        return os.path.abspath(str(Path(self.runtime_data_dir) / "repos"))

    @property
    def database_url(self) -> str:
        """生成数据库连接URL"""
        if self.database_type.lower() == "postgresql":
            return f"postgresql+asyncpg://{self.postgresql_user}:{self.postgresql_password}@{self.postgresql_host}:{self.postgresql_port}/{self.db_name}"
        elif self.database_type.lower() == "mysql":
            return f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}@{self.mysql_host}:{self.mysql_port}/{self.db_name}"
        else:
            filename = self.db_name if self.db_name.lower().endswith(".db") else f"{self.db_name}.db"
            raw_path = str(Path(self.runtime_data_dir) / "sqlite" / filename)
            abs_path = os.path.abspath(raw_path)
            parent_dir = os.path.dirname(abs_path)
            if parent_dir and not os.path.isdir(parent_dir):
                os.makedirs(parent_dir, exist_ok=True)
            norm_path = normalize_path(abs_path)
            return f"sqlite+aiosqlite:///{norm_path}"
    
    @property
    def lancedb_uri(self) -> str:
        """LanceDB 目录：{runtime_data_dir}/lancedb。"""
        return os.path.abspath(str(Path(self.runtime_data_dir) / "lancedb"))

    @property
    def model_cache_dir(self) -> str:
        """模型缓存目录：{runtime_data_dir}/model_cache_dir。"""
        return os.path.abspath(str(Path(self.runtime_data_dir) / "model_cache_dir"))

    @property
    def model_temp_dir(self) -> str:
        """模型临时目录：{runtime_data_dir}/model_temp_dir。"""
        return os.path.abspath(str(Path(self.runtime_data_dir) / "model_temp_dir"))
    
    @property
    def app_name(self) -> str:
        """应用名称(用于JWT issuer等)"""
        return APP_NAME


# 全局配置实例
settings = Settings()


# 全局配置常量
MODELS_CONFIG_DIR = Path(settings.runtime_data_dir) / "models"