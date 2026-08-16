import logging
import asyncio
import time
from contextlib import asynccontextmanager
from typing import Optional,AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession
from app.infrastructure.database.base import AsyncBaseConnection, DatabaseConfig
from app.infrastructure.database.sql_connect import SQLConnection
from app.config.settings import settings


class DatabaseFactory:
    """数据库工厂类 - 懒加载 + 健康检查 + 自动重连"""
    
    def __init__(self):
        self._connection_type: Optional[str] = None
        self._connection: Optional[AsyncBaseConnection] = None
        self._connection_lock = asyncio.Lock()
        self._last_health_check: float = 0
        self._health_check_interval: int = 30  # 健康检查间隔（秒）
    
    async def _create_connection(self) -> AsyncBaseConnection:
        """
        创建数据库连接（内部方法）
        
        Returns:
            AsyncBaseConnection: 数据库连接实例
        """
        # 如果已存在，先关闭旧连接
        if self._connection:
            try:
                await self._connection.close()
            except Exception as e:
                logging.warning(f"关闭旧数据库连接失败: {e}")
            self._connection = None
        
        # 使用配置中的默认值
        actual_db_type = settings.database_type
        db_type_lower = actual_db_type.lower()
        
        try:
            # 验证数据库类型
            supported_types = ['postgresql', 'postgres', 'mysql', 'sqlite', 'oracle', 'mssql', 'sqlserver']
            if db_type_lower not in supported_types:
                raise ValueError(f"不支持的数据库类型: {db_type_lower}，支持的类型: {', '.join(supported_types)}")
            
            # 直接构造数据库配置
            config = DatabaseConfig(
                url=settings.database_url,
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                pool_recycle=3600,  # 默认值
                pool_pre_ping=True,  # 默认值
                echo=False  # 默认值
            )
            
            # 创建数据库连接
            connection = SQLConnection(db_type_lower)
            await connection.create_engine(config)
            
            # 保存连接信息
            self._connection = connection
            self._connection_type = db_type_lower
            
            logging.info(f"数据库连接创建成功: {actual_db_type}")
            return connection
            
        except Exception as e:
            logging.error(f"创建数据库连接失败: {e}")
            raise
    
    def _should_check_health(self) -> bool:
        """判断是否需要健康检查"""
        return time.time() - self._last_health_check > self._health_check_interval
    
    async def _health_check(self) -> bool:
        """健康检查"""
        try:
            if not self._connection:
                return False
            return await self._connection.health_check()
        except Exception as e:
            logging.warning(f"数据库健康检查失败: {e}")
            return False
    
    async def get_connection(self, db_type: str = None) -> Optional[AsyncBaseConnection]:
        """获取连接（懒加载 + 健康检查）"""
        async with self._connection_lock:
            # 懒加载：如果不存在则创建
            if not self._connection:
                await self._create_connection()
            
            # 健康检查：如果连接存在，定期检查健康状态
            if self._connection:
                if self._should_check_health():
                    if not await self._health_check():
                        logging.warning("数据库连接不健康，重新创建")
                        await self._create_connection()
                    self._last_health_check = time.time()
            
            return self._connection

# 全局工厂实例
_database_factory = DatabaseFactory()

# 通过以来注释方式使用，通过全局方法方式对外暴漏
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库会话（兼容旧依赖注入写法）"""
    global _database_factory

    # 使用新的懒加载 + 健康检查机制
    conn = await _database_factory.get_connection()
    if not conn:
        raise RuntimeError("数据库连接不可用")

    async with conn.get_session() as session:
        yield session

@asynccontextmanager
async def get_db_session():
    """获取数据库会话上下文 - 适用于后台任务；内部复用 get_db。"""
    agen = get_db()
    try:
        session = await agen.__anext__()
        yield session
    finally:
        await agen.aclose()

async def close_db():
    """关闭数据库连接"""
    global _database_factory
    
    if _database_factory._connection:
        await _database_factory._connection.close()
        _database_factory._connection = None
        logging.info("数据库连接已关闭")

async def health_check_db() -> bool:
    """数据库健康检查"""
    global _database_factory
    
    try:
        # 使用新的懒加载 + 健康检查机制
        conn = await _database_factory.get_connection()
        if not conn:
            raise RuntimeError("数据库连接不可用")
        
        # 执行健康检查
        return await conn.health_check()
    except Exception as e:
        logging.error(f"数据库健康检查失败: {e}")
        return False
