import uuid
import logging
from datetime import datetime
from typing import List, Optional
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from ..models.git_auth_mgmt import GitAuthority


class GitAuthService:
    """Git认证信息服务"""
        
    @staticmethod
    async def save_git_auth(session: AsyncSession, user_id: str, provider: str, access_token: str) -> GitAuthority:
        """保存Git认证信息"""
        try:
            # 检查是否已存在
            existing_auth = await GitAuthService.get_user_git_auth(session, user_id, provider)
            if existing_auth:
                # 更新现有记录
                await session.execute(
                    update(GitAuthority)
                    .where(GitAuthority.id == existing_auth.id)
                    .values(
                        access_token=access_token,
                        updated_at=datetime.utcnow()
                    )
                )
                await session.commit()
                await session.refresh(existing_auth)
                return existing_auth    
            else:
                # 创建新记录
                git_auth = GitAuthority(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    provider=provider,
                    access_token=access_token,
                    is_active=True,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow()
                )                
                session.add(git_auth)
                await session.commit()
                await session.refresh(git_auth)
                return git_auth                
        except Exception as e:
            logging.error(f"保存Git认证信息失败: {e}")
            await session.rollback()
            raise
    
    @staticmethod
    async def get_user_git_auth(session: AsyncSession, user_id: str, provider: str) -> Optional[GitAuthority]:
        """获取用户特定提供商的Git认证信息"""
        try:
            result = await session.execute(
                select(GitAuthority).where(
                    GitAuthority.user_id == user_id,
                    GitAuthority.provider == provider,
                    GitAuthority.is_active == True
                )
            )
            return result.scalar_one_or_none()
        except Exception as e:
            logging.error(f"获取用户{provider}认证信息失败: {e}")
            return None

    @staticmethod
    async def get_user_git_auths(session: AsyncSession, user_id: str) -> List[GitAuthority]:
        """获取用户的所有Git认证信息"""
        try:
            result = await session.execute( 
                select(GitAuthority).where(
                    GitAuthority.user_id == user_id,
                    GitAuthority.is_active == True
                )
            )
            return result.scalars().all()
        except Exception as e:
            logging.error(f"获取用户Git认证信息失败: {e}")
            return []
    
    @staticmethod
    async def delete_git_auth(session: AsyncSession, user_id: str, provider: str) -> bool:
        """删除Git认证信息"""
        try:
            result = await session.execute(
                delete(GitAuthority).where(
                    GitAuthority.user_id == user_id,
                    GitAuthority.provider == provider
                )
            )
            await session.commit()
            deleted_count = result.rowcount
            if deleted_count > 0:
                return True
            else:
                return False
        except Exception as e:
            logging.error(f"删除Git认证信息失败: {e}")
            await session.rollback()
            return False
    
