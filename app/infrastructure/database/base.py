from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


class AsyncBaseConnection(ABC):
    """数据库连接抽象基类"""
    
    @abstractmethod
    async def create_engine(self, config: 'DatabaseConfig') -> AsyncEngine:
        """创建数据库引擎"""
        pass
    
    @abstractmethod
    async def get_session(self) -> AsyncSession:
        """获取数据库会话"""
        pass
    
    @abstractmethod
    async def close(self):
        """关闭数据库连接"""
        pass
    
    @abstractmethod
    async def health_check(self) -> bool:
        """健康检查"""
        pass


class DatabaseConfig:
    """数据库配置类"""
    
    def __init__(self, 
                 url: str,
                 pool_size: int = 10,
                 max_overflow: int = 20,
                 pool_recycle: int = 3600,
                 pool_pre_ping: bool = True,
                 echo: bool = False,
                 **kwargs):
        self.url = url
        self.pool_size = pool_size
        self.max_overflow = max_overflow
        self.pool_recycle = pool_recycle
        self.pool_pre_ping = pool_pre_ping
        self.echo = echo
        self.extra_kwargs = kwargs
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            'url': self.url,
            'pool_size': self.pool_size,
            'max_overflow': self.max_overflow,
            'pool_recycle': self.pool_recycle,
            'pool_pre_ping': self.pool_pre_ping,
            'echo': self.echo,
            **self.extra_kwargs
        }
