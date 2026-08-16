import uuid
import os
import shutil
import zipfile
import tempfile
import gzip
import tarfile
import brotli
import logging
from datetime import datetime
from typing import Any, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update, and_, or_
from app.config.settings import settings
from ..schemes.git_repo_mgmt import CreateRepositoryFromUrl, UpdateRepository
from ..models.git_repo_mgmt import GitRepository, RepoKind


def _remote_git():
    from .remote_git_service import RemoteGitService
    return RemoteGitService


class GitRepositoryService:
    """仓库管理服务"""
    
    @staticmethod
    def _get_base_storage_path():
        """获取基础存储路径"""
        return settings.repo_storage_path
    
    @staticmethod
    async def create_repository_from_url(session: AsyncSession, user_id: str, create_data: CreateRepositoryFromUrl) -> GitRepository:
        """通过Git URL创建仓库""" 
        try:
            # 验证URL
            await GitRepositoryService._validate_url_repository(create_data.repository_url)
            
            # 从URL解析仓库信息
            provider = _remote_git().get_git_provider(create_data.repository_url)
            repo_organization, repo_name = _remote_git().get_git_url_info(create_data.repository_url)
            
            # 检查仓库是否已存在
            existing_repo = await session.execute(
                select(GitRepository).where(
                    GitRepository.git_provider == provider,
                    GitRepository.organization == repo_organization,
                    GitRepository.repository_name == repo_name,
                    GitRepository.user_id == user_id
                )
            )   
            if existing_repo.scalar_one_or_none():
                raise ValueError("仓库已存在")

            # 创建本地路径
            local_repo_path = os.path.join(GitRepositoryService._get_base_storage_path(), repo_organization, repo_name)
            
            # 创建仓库记录，状态为等待克隆
            repository = GitRepository(
                id=str(uuid.uuid4()),
                user_id=user_id,
                git_provider=provider,
                repository_url=create_data.repository_url,
                organization=repo_organization,
                repository_name=repo_name,
                description=create_data.description,
                kind=RepoKind.CODE,
                branch=create_data.branch,
                local_path=local_repo_path,
                is_cloned=False,
                last_sync_time=None
            )
            session.add(repository)
            await session.commit()
            await session.refresh(repository)
            
            # 启动异步克隆任务
            # 当前实现先仅落库仓库记录，后续可在单独任务/接口中补充克隆与同步逻辑
            repository.is_cloned=False
            repository.last_sync_time=None
        
            logging.info(f"Created repository: {repository.repository_name} by user {user_id}")
            return repository
        
        except Exception as e:
            logging.error(f"Failed to create repository: {e}")
            await session.rollback()
            raise

    @staticmethod
    async def _validate_url_repository(repo_url: str):
        """验证URL仓库是否可访问"""
        # 这里可以添加Git URL验证逻辑
        if not repo_url or not repo_url.startswith(('http://', 'https://', 'git@')):
            raise ValueError("无效的仓库URL")
    
    @staticmethod
    async def create_repository_from_package(
        session: AsyncSession, 
        user_id: str, 
        name: str, 
        description: str, 
        file: Any) -> GitRepository:
        """通过上传压缩包创建仓库"""
        try:
            # 检查仓库是否已存在
            existing_repo = await session.execute(
                select(GitRepository).where(
                    GitRepository.repository_name == name,
                    GitRepository.user_id == user_id
                )
            )
            if existing_repo.scalar_one_or_none():
                raise ValueError("仓库已存在")

            # 创建本地存储路径 - 使用用户名和仓库名构建路径
            local_path = os.path.join(GitRepositoryService._get_base_storage_path(), "uploads", user_id, name)
            os.makedirs(local_path, exist_ok=True)
            
            try:
                # 保存上传的文件
                file_path = os.path.join(local_path, file.filename)
                with open(file_path, "wb") as buffer:
                    content = await file.read()
                    buffer.write(content)
                
                # 解压文件
                await GitRepositoryService._extract_archive(file_path, local_path)

                # 处理解压目录
                await GitRepositoryService._process_extracted_directory(local_path)
                
                # 删除原始压缩包
                os.remove(file_path)
                
            except Exception as e:
                # 清理失败的文件
                if os.path.exists(local_path):
                    shutil.rmtree(local_path)
                raise ValueError(f"处理上传文件失败: {str(e)}")
            
            # 创建仓库记录
            repository = GitRepository(
                id=str(uuid.uuid4()),
                user_id=user_id,
                git_provider="uploaded",
                repository_url="",  # 上传方式没有URL
                organization="uploaded",  # 上传方式没有组织
                repository_name=name,
                description=description,
                kind=RepoKind.CODE,
                branch="main",  # 上传方式默认分支
                local_path=local_path,
                is_cloned=False,
                last_sync_time=None
            )
            
            session.add(repository)
            await session.commit()
            await session.refresh(repository)
        
            logging.info(f"Created repository from package: {repository.repository_name} by user {user_id}")
            return repository
            
        except Exception as e:
            # 清理失败的文件
            if local_path and os.path.exists(local_path):
                shutil.rmtree(local_path, ignore_errors=True)
            logging.error(f"创建仓库失败: {str(e)}")
            raise
    
    @staticmethod
    async def _extract_archive(file_path: str, extract_path: str):
        """解压压缩包"""
        try:            
            if file_path.endswith('.zip'):
                with zipfile.ZipFile(file_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_path)
            elif file_path.endswith('.gz'):
                with gzip.open(file_path, 'rb') as f_in:
                    with open(extract_path, 'wb') as f_out:
                        f_out.write(f_in.read())
            elif file_path.endswith('.tar'):
                with tarfile.open(file_path, 'r') as tar_ref:
                    tar_ref.extractall(extract_path)
            elif file_path.endswith('.br'):
                if not brotli:
                    raise ValueError("不支持.br压缩包（缺少brotli依赖）")
                with open(file_path, 'rb') as f_in:
                    with open(extract_path, 'wb') as f_out:
                        f_out.write(brotli.decompress(f_in.read()))
        except Exception as e:
            logging.error(f"解压文件失败: {str(e)}")
            raise
    
    @staticmethod
    async def _process_extracted_directory(extract_path: str):
        """处理解压后的目录结构"""
        try:
            if os.path.exists(extract_path) and os.path.isdir(extract_path):
                # 如果解压后目录下只有一个文件夹，将其内容移动到上级目录
                items = os.listdir(extract_path)
                if len(items) == 1 and os.path.isdir(os.path.join(extract_path, items[0])):
                    sub_dir = os.path.join(extract_path, items[0])
                    for item in os.listdir(sub_dir):
                        src = os.path.join(sub_dir, item)
                        dst = os.path.join(extract_path, item)
                        if os.path.exists(dst):
                            if os.path.isdir(dst):
                                import shutil
                                shutil.rmtree(dst)
                            else:
                                os.remove(dst)
                        os.rename(src, dst)
                    os.rmdir(sub_dir)
        except Exception as e:
            logging.error(f"处理解压目录失败: {str(e)}")
            raise
    
    @staticmethod
    async def create_repository_from_path(
        session: AsyncSession,
        user_id: str,
        name: str,
        description: str,
        local_repo_path: str,
        git_url: str,
        kind: str = RepoKind.CODE,
    ) -> GitRepository:
        """通过指定路径创建仓库"""
        try:
            kind_value = (kind or RepoKind.CODE).strip().lower()
            if kind_value not in RepoKind.VALUES:
                raise ValueError(f"无效 kind={kind!r}，仅支持: {', '.join(RepoKind.VALUES)}")

            # 验证服务端路径
            GitRepositoryService._validate_repo_path(local_repo_path)

            repo_url = git_url.strip() if git_url else ""

            # 检查仓库是否已存在
            existing_repo = await session.execute(
                select(GitRepository).where(
                    GitRepository.repository_name == name,
                    GitRepository.user_id == user_id
                )
            )
            if existing_repo.scalar_one_or_none():
                raise ValueError("仓库已存在")

            # 解析git URL获取provider和organization（允许无远端URL的本地仓）
            if repo_url:
                git_provider = _remote_git().get_git_provider(repo_url) or "local"
                organization, _ = _remote_git().get_git_url_info(repo_url)
            else:
                git_provider = "local"
                organization = "local"

            # 创建仓库记录
            repository = GitRepository(
                id=str(uuid.uuid4()),
                user_id=user_id,
                git_provider=git_provider,
                repository_url=repo_url,
                organization=organization,
                repository_name=name,
                description=description,
                kind=kind_value,
                branch="main",
                local_path=local_repo_path,
                is_cloned=False,
                last_sync_time=None
            )

            session.add(repository)
            await session.commit()
            await session.refresh(repository)

            logging.info(
                "Created repository from path: %s kind=%s by user %s",
                repository.repository_name,
                kind_value,
                user_id,
            )
            return repository

        except Exception as e:
            logging.error(f"创建路径仓库失败: {str(e)}")
            raise
    
    @staticmethod
    def _validate_repo_path(repo_path: str):
        if not repo_path:
            raise ValueError("路径不能为空")
        if not os.path.exists(repo_path):
            raise ValueError(f"路径不存在: {repo_path}")
        if not os.path.isdir(repo_path):
            raise ValueError(f"路径不是目录: {repo_path}")
        if not os.access(repo_path, os.R_OK):
            raise ValueError(f"路径无读取权限: {repo_path}")
    
    @staticmethod
    async def get_repository_by_id(db: AsyncSession, repository_id: str) -> Optional[GitRepository]:
        """根据ID获取仓库"""
        try:
            result = await db.execute(
                select(GitRepository).where(GitRepository.id == repository_id)
            )
            return result.scalar_one_or_none()
        except Exception as e:
            logging.error(f"Failed to get repository by id: {e}")
            raise
    
    @staticmethod
    async def get_repository_list(
        db: AsyncSession,
        user_id: str, 
        page: int = 1, 
        page_size: int = 10, 
        keyword: Optional[str] = None
    ) -> tuple[List[GitRepository], int]:
        """获取用户仓库列表"""
        try:
            query = select(GitRepository).where(GitRepository.user_id == user_id)
            
            # 如果有关键词，则按名称或描述搜索
            if keyword:
                query = query.where(
                    GitRepository.repository_name.contains(keyword) | 
                    GitRepository.description.contains(keyword) |
                    GitRepository.organization.contains(keyword)
                )
            
            # 按创建时间降序排序
            query = query.order_by(GitRepository.created_at.desc())
            
            # 计算总数
            count_result = await db.execute(
                select(GitRepository).where(query.whereclause)
            )
            total = len(count_result.scalars().all())
            
            # 获取分页数据
            query = query.offset((page - 1) * page_size).limit(page_size)
            result = await db.execute(query)
            repositories = result.scalars().all()
            
            return repositories, total
        except Exception as e:
            logging.error(f"Failed to get repository list: {e}")
            raise

    @staticmethod
    async def update_repository(
        db: AsyncSession,
        repository_id: str, 
        user_id: str, 
        update_data: UpdateRepository
    ) -> Optional[GitRepository]:
        """更新仓库"""
        try:
            repository = await GitRepositoryService.get_repository_by_id(db, repository_id)
            if not repository:
                raise ValueError("仓库不存在")
            
            if repository.user_id != user_id:
                raise ValueError("无权限更新仓库")
            
            # 更新仓库信息
            if update_data.description is not None:
                repository.description = update_data.description

            if update_data.repository_url is not None:
                repo_url=update_data.repository_url.strip()
                provider=_remote_git().get_git_provider(repo_url)
                organization,repo_name=_remote_git().get_git_url_info(repo_url)
                repository.repository_url=repo_url
                repository.git_provider=provider or "local"
                repository.organization=organization
                repository.repository_name=repo_name
            
            if update_data.branch is not None:
                repository.branch = update_data.branch
                # 如果更新了分支，需要重新克隆或切换分支
                await GitRepositoryService._update_repository_branch(repository, update_data.branch)
            
            repository.updated_at = datetime.utcnow()
            
            await db.commit()
            await db.refresh(repository)
            
            logging.info(f"Updated repository: {repository.repository_name}")
            return repository
            
        except Exception as e:
            logging.error(f"更新仓库失败: {e}")
            raise
    
    @staticmethod
    async def _update_repository_branch(repository: GitRepository, new_branch: str):
        """更新仓库分支"""
        try:
            # 检查仓库类型
            if repository.git_provider == "uploaded":
                # 上传的仓库没有Git URL，无法切换分支
                logging.warning(f"Repository {repository.repository_name} is uploaded type, cannot switch branch")
                return
            
            if repository.git_provider == "local":
                # 路径类型的仓库，检查是否为Git仓库
                if not os.path.exists(os.path.join(repository.local_path, '.git')):
                    logging.warning(f"Repository {repository.repository_name} is not a Git repository, cannot switch branch")
                    return
            
            # 使用GitService切换分支
            success = _remote_git().checkout_branch(repository.local_path, new_branch)
            if success:
                logging.info(f"Successfully switched repository {repository.repository_name} to branch {new_branch}")
            else:
                logging.error(f"Failed to switch repository {repository.repository_name} to branch {new_branch}")
                raise ValueError(f"切换分支失败: {new_branch}")
                
        except Exception as e:
            logging.error(f"更新仓库分支失败: {e}")
            raise
    
    @staticmethod
    async def delete_repository(
        db: AsyncSession,
        repository_id: str,
        user_id: str,
        delete_local: bool = False,
    ) -> bool:
        """删除仓库"""
        try:
            repository = await GitRepositoryService.get_repository_by_id(db, repository_id)
            if not repository:
                raise ValueError("仓库不存在")
            
            if repository.user_id != user_id:
                raise ValueError("无权限删除仓库")
            
            # 清理该仓库的分析数据（文件分析状态 + 相关向量）
            try:
                from ...repo_analysis.services.analysis_service import AnalysisService
                await AnalysisService.delete_repo_analysis_data(repository_id)
            except Exception as e:
                logging.warning("删除仓库前清理分析数据失败 repo_id=%s error=%s", repository_id,e)

            try:
                from ...repo_analysis.services.experience_service import ExperienceService
                await ExperienceService.clear(repository_id)
            except Exception as e:
                logging.warning("删除仓库前清理经验数据失败 repo_id=%s error=%s", repository_id, e)

            # 删除本地文件
            if delete_local and repository.local_path and os.path.exists(repository.local_path):
                shutil.rmtree(repository.local_path)
            
            await db.execute(delete(GitRepository).where(GitRepository.id == repository_id))
            await db.commit()
            logging.info(f"Deleted repository: {repository.repository_name}")
            return True
        except Exception as e:
            logging.error(f"删除仓库失败: {e}")
            raise
