from typing import Optional
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine, AsyncSession, async_sessionmaker
import logging
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool, QueuePool
from sqlalchemy import text
from contextlib import asynccontextmanager
from app.infrastructure.database.base import AsyncBaseConnection, DatabaseConfig

class SQLConnection(AsyncBaseConnection):
    """通用SQL数据库连接类，支持PostgreSQL、MySQL、SQLite、Oracle等"""
    
    # 数据库类型映射
    DB_TYPE_MAPPING = {
        'mysql': 'MySQL',
        'postgresql': 'PostgreSQL', 
        'postgres': 'PostgreSQL',
        'sqlite': 'SQLite',
        'oracle': 'Oracle',
        'mssql': 'SQL Server',
        'sqlserver': 'SQL Server'
    }
    
    def __init__(self, db_type: str):
        self.engine: Optional[AsyncEngine] = None
        self.session_maker = None
        self.db_type = db_type.lower()
        self.db_name = self.DB_TYPE_MAPPING.get(self.db_type, db_type.title())
    
    async def create_engine(self, config: DatabaseConfig) -> AsyncEngine:
        """创建异步数据库引擎"""
        try:
            # 统一配置处理方式
            engine_config = config.extra_kwargs.copy()
            engine_config.update({
                'pool_size': config.pool_size,
                'max_overflow': config.max_overflow,
                'pool_pre_ping': config.pool_pre_ping,
                'pool_recycle': config.pool_recycle,
                'echo': config.echo
            })
            
            # 数据库特定配置
            self._apply_db_specific_config(engine_config)
            
            self.engine = create_async_engine(config.url, **engine_config)
            
            self.session_maker = async_sessionmaker(
                bind=self.engine,
                autocommit=False,
                autoflush=False,
                expire_on_commit=False
            )
            # SQLite 并发写时容易触发 locked，提前启用 WAL 与忙等待。
            if self.db_type == 'sqlite':
                async with self.engine.connect() as conn:
                    await conn.execute(text("PRAGMA journal_mode=WAL"))
                    await conn.execute(text("PRAGMA synchronous=NORMAL"))
                    await conn.execute(text("PRAGMA busy_timeout=30000"))
                    await conn.commit()
            logging.info(f"{self.db_name}异步数据库连接创建成功")
            return self.engine
            
        except Exception as e:
            logging.error(f"{self.db_name}数据库连接创建失败: {e}")
            raise
    
    def _apply_db_specific_config(self, engine_config: dict):
        """应用数据库特定配置"""
        connect_args = engine_config.get('connect_args', {})
        
        if self.db_type == 'mysql':
            connect_args.update({
                'charset': 'utf8mb4',
                'autocommit': False
            })
        elif self.db_type in ['postgresql', 'postgres']:
            connect_args.update({
                'server_settings': {
                    'application_name': 'knowledge_service'
                }
            })
        elif self.db_type == 'sqlite':
            # SQLite 文件库：用 NullPool，避免 QueuePool 在高并发分析下耗尽/锁死
            engine_config.pop('pool_size', None)
            engine_config.pop('max_overflow', None)
            engine_config.pop('pool_recycle', None)
            engine_config['poolclass'] = NullPool
            connect_args.update({
                'check_same_thread': False,
                # 让底层 sqlite3 在遇到锁时等待一段时间再报错
                'timeout': 60
            })
        elif self.db_type in ['oracle']:
            connect_args.update({
                'encoding': 'utf-8'
            })
        elif self.db_type in ['mssql', 'sqlserver']:
            connect_args.update({
                'trusted_connection': 'yes'
            })
        
        if connect_args:
            engine_config['connect_args'] = connect_args
    
    @asynccontextmanager
    async def get_session(self):
        """获取异步会话 - 异步上下文管理器"""
        if not self.session_maker:
            if not self.engine:
                raise RuntimeError("数据库引擎未初始化，请先调用create_engine方法")
            else:
                raise RuntimeError("数据库引擎已创建但session_maker未初始化")
        
        session = self.session_maker()
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
    
    async def health_check(self) -> bool:
        """健康检查"""
        try:
            async with self.get_session() as session:
                await session.execute(text("SELECT 1"))
                logging.debug("数据库健康检查成功")
                return True
        except Exception as e:
            logging.error(f"数据库健康检查失败: {e}")
            return False
    
    async def close(self):
        """关闭数据库连接"""
        if self.engine:
            await self.engine.dispose()
            self.engine = None
            self.session_maker = None
            logging.info("数据库连接已关闭")
