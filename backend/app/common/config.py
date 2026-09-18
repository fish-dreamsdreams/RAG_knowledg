"""应用配置：全部来自环境变量（见仓库根 .env.example）。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# BGE-M3 dense 向量维度；Milvus collection 与 Embedder 必须一致（TECH_SPEC §5.3）
EMBED_DIM = 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # 先读仓库根 .env，再读 backend/.env（后者优先），便于从任一路径启动
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # 依赖地址（宿主端口避开其他项目占用，见 docker-compose.yml）
    database_url: str = "postgresql+asyncpg://kb:kb_dev_password@127.0.0.1:15432/knowledge_base"
    redis_url: str = "redis://127.0.0.1:16379/0"
    celery_broker_url: str = "redis://127.0.0.1:16379/1"
    celery_result_backend: str = "redis://127.0.0.1:16379/2"
    milvus_uri: str = "http://127.0.0.1:19530"

    # 对象存储（MinIO）：原件 / 解析产物 / 图片（TECH_SPEC §8.0）
    # 与 Milvus 内部的 minio 是两个实例：后者只给 Milvus 用且不发布端口
    minio_endpoint: str = "http://127.0.0.1:19000"
    minio_access_key: str = "kb_minio"
    minio_secret_key: str = "kb_minio_dev_password"
    minio_bucket: str = "kb-assets"
    minio_secure: bool = False

    # 安全
    secret_key: str = "dev-only-change-me"
    access_token_expire_minutes: int = 480
    # 模型配置里 API Key 的 Fernet 密钥（urlsafe base64 的 32 字节）。留空则从 SECRET_KEY
    # 派生：开发环境不必多配一项，但轮换 SECRET_KEY 会让已存密文全部解不开，生产必须显式配置
    config_encryption_key: str = ""

    # 看板口径时区：审计时间戳是 UTC，"今日/近 7 天"与按天趋势必须换算到业务时区，
    # 否则北京时间上午 8 点前的提问会被算到前一天
    report_timezone: str = "Asia/Shanghai"

    # Chat 网关（仅 Chat；Embedding 走本地 BGE-M3）
    llm_base_url: str = ""
    llm_api_key: str = ""
    # 模型名兜底。刻意不设默认值：不同网关的模型名毫无共性（DeepSeek 是 deepseek-v4-pro，
    # 别的网关又是另一套），猜一个只会让「配错了」表现为运行时 400 而不是启动时就报清楚
    llm_chat_model: str = ""
    llm_timeout_seconds: int = 60

    # 本地模型
    embed_model: str = "BAAI/bge-m3"
    embed_device: str = "cuda"
    embed_sparse_enabled: bool = True
    # 启动即加载权重并跑一次最小编码：把权重加载与 CUDA context 创建挑到启动期，
    # 避免首个导入任务既等模型又等首次初始化
    embed_warmup_enabled: bool = True
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    # 留空表示跟 embed_device；重排不写向量库，显存紧张时可以单独放 cpu
    rerank_device: str = ""
    hf_home: str = ""
    hyde_enabled: bool = True
    rerank_cliff_min_keep: int = 3
    rerank_cliff_max_keep: int = 10
    # 归一化落差比阈值（落差 / 最高分），不是绝对落差；依据见 engines/rerank 模块注释
    rerank_cliff_min_ratio: float = 0.13

    # FAQ 挖掘的语义归并阈值：比 faq_sim_threshold 宽，因为要合并的是「同义问法」
    # （「年假几天」与「年假能休多少天」），而不是判定「是不是同一个问题」
    faq_cluster_sim: float = 0.8

    # MinerU 在线 API
    mineru_api_base_url: str = "https://mineru.net/api/v4"
    mineru_api_key: str = ""
    mineru_model_version: str = "pipeline"
    mineru_language: str = "ch"
    mineru_poll_interval_seconds: int = 5
    mineru_task_timeout_seconds: int = 900
    mineru_output_dir: str = "./var/mineru"

    # 导入与上传
    ingest_concurrency: int = 4
    # 已废弃：原件改存 MinIO（source_path 现在存对象 key）；仅历史本地文件兼容用
    upload_dir: str = "./var/uploads"

    # 图片资产（TECH_SPEC §8.0）：解出上限，超限跳过且不阻断导入
    asset_max_file_bytes: int = 10 * 1024 * 1024
    asset_max_total_bytes: int = 100 * 1024 * 1024
    asset_max_files: int = 200

    # 视觉图注（TECH_SPEC §8.0）：只对 pdf/docx 的图片生成；默认关闭
    assets_vision_enabled: bool = False
    vision_base_url: str = ""
    vision_api_key: str = ""
    vision_model: str = ""
    vision_concurrency: int = 3
    vision_max_images_per_unit: int = 50

    # 单条答案最多下发几张图（TECH_SPEC §8.1）；0 表示不下发
    answer_max_images: int = 3

    # CORS：前端 Vite dev server
    cors_origins: list[str] = ["http://127.0.0.1:5173", "http://localhost:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
