import os
import shutil
import logging
from typing import List, Tuple, Optional
from datetime import datetime
from urllib.parse import urlparse
import git
from sqlalchemy.ext.asyncio import AsyncSession
from .git_auth_service import GitAuthService


class GitRepoInfo:
    """Git仓库信息"""
    
    def __init__(self, local_path: str, repository_name: str, organization: str,
                 branch_name: str, commit_time: str, commit_author: str,
                 commit_message: str, version: str):
        self.local_path = local_path
        self.repository_name = repository_name
        self.organization = organization
        self.branch_name = branch_name
        self.commit_time = commit_time
        self.commit_author = commit_author
        self.commit_message = commit_message
        self.version = version
    
    def to_dict(self):
        """转换为字典"""
        return {
            "local_path": self.local_path,
            "repository_name": self.repository_name,
            "organization": self.organization,
            "branch_name": self.branch_name,
            "commit_time": self.commit_time,
            "commit_author": self.commit_author,
            "commit_message": self.commit_message,
            "version": self.version
        }


class RemoteGitService:
    """Git服务"""
    # 根据git地址识别提供商
    @staticmethod
    def get_git_provider(git_url: str) -> Optional[str]:
        """从Git URL识别提供商"""
        git_url_lower = git_url.lower()
        
        if "github.com" in git_url_lower:
            return "github"
        elif "gitee.com" in git_url_lower:
            return "gitee"
        elif "gitlab.com" in git_url_lower:
            return "gitlab"
        else:
            return None 

    # 通过git地址解析组织名、仓库名、本地路径
    @staticmethod
    def get_git_url_info(git_url: str) -> Tuple[str, str]:
        """获取仓库路径"""
        # 解析仓库地址
        parsed_url = urlparse(git_url)
        # 分割仓库地址
        path_segments = parsed_url.path.strip('/').split('/')
        if len(path_segments) < 2:
            raise ValueError("无效的git地址")
        organization = path_segments[0]
        repo_name = path_segments[1].replace('.git', '')
        return organization, repo_name
    
    # 从git仓库下载代码到本地
    @staticmethod
    async def clone_repository(
        session: AsyncSession,
        repository_url: str, 
        local_repo_path: str, 
        branch: str = "main", 
        user_id: str = None) -> GitRepoInfo:
        """克隆仓库"""
        try:
            
            # 检查仓库是否已存在
            if os.path.exists(local_repo_path):
                try:
                    # 获取现有仓库信息
                    repo = git.Repo(local_repo_path)
                    
                    # 检查是否是正确的仓库
                    current_remote_url = repo.remotes.origin.url
                    if current_remote_url != repository_url:
                        logging.info(f"仓库URL已变更，清空目录重新下载: {current_remote_url} -> {repository_url}")
                        shutil.rmtree(local_repo_path, ignore_errors=True)
                    else:
                        # 更新仓库到最新版本
                        logging.info(f"仓库已存在，正在更新: {local_repo_path}")
                        origin = repo.remotes.origin
                        origin.pull()
                        
                        # 获取更新后的仓库信息
                        head = repo.head
                        commit = head.commit
                        
                        # 从路径解析仓库信息
                        path_parts = local_repo_path.split(os.sep)
                        if len(path_parts) >= 2:
                            organization = path_parts[-2]
                            repository_name = path_parts[-1]
                        else:
                            organization = "unknown"
                            repository_name = os.path.basename(local_repo_path)
                        
                        return GitRepoInfo(
                            local_path=local_repo_path,
                            repository_name=repository_name,
                            organization=organization,
                            branch_name=head.ref.name,
                            commit_time=commit.committed_datetime.isoformat(),
                            commit_author=commit.author.name,
                            commit_message=commit.message,
                            version=commit.hexsha
                        )
                except Exception as e:
                    logging.warning(f"读取或更新现有仓库失败，重新克隆: {e}")
                    # 删除目录后重新克隆
                    shutil.rmtree(local_repo_path, ignore_errors=True)
            
            # 创建目录
            os.makedirs(local_repo_path, exist_ok=True)
            
            # 克隆选项
            clone_options = {
                'branch': branch,
                'depth': 0
            }
            
            # 从认证表获取令牌
            access_token = None
            try:
                provider = RemoteGitService.get_git_provider(repository_url)
                if provider:
                    git_auth = await GitAuthService.get_user_git_auth(session, user_id, provider)
                    if git_auth and git_auth.access_token:
                        access_token = git_auth.access_token
                        logging.info(f"使用用户{user_id}的{provider}认证令牌")
                    else:
                        logging.warning(f"用户{user_id}的{provider}认证令牌不存在")
                else:
                    logging.warning(f"无法识别Git提供商: {repository_url}")
            except Exception as e:
                logging.warning(f"获取用户Git认证信息失败: {e}")
            
            # 使用令牌认证克隆仓库
            if access_token:
                # 使用令牌认证
                auth_url = repository_url.replace('https://', f'https://oauth2:{access_token}@')
                logging.info(f"开始克隆仓库: {repository_url}")
                repo = git.Repo.clone_from(auth_url, local_repo_path, **clone_options)
            else:
                # 无认证信息，尝试克隆公开仓库
                logging.info(f"无认证令牌，尝试克隆公开仓库: {repository_url}")
                repo = git.Repo.clone_from(repository_url, local_repo_path, **clone_options)
            
            # 克隆完成
            logging.info(f"仓库克隆完成: {repository_url}")
            
            # 获取仓库信息
            head = repo.head
            commit = head.commit
            
            # 从路径解析仓库信息
            path_parts = local_repo_path.split(os.sep)
            if len(path_parts) >= 2:
                organization = path_parts[-2]
                repository_name = path_parts[-1]
            else:
                organization = "unknown"
                repository_name = os.path.basename(local_repo_path)
            
            return GitRepoInfo(
                local_path=local_repo_path,
                repository_name=repository_name,
                organization=organization,
                branch_name=head.ref.name,
                commit_time=commit.committed_datetime.isoformat(),
                commit_author=commit.author.name,
                commit_message=commit.message,
                version=commit.hexsha
            )
            
        except Exception as e:
            logging.error(f"克隆仓库失败: {e}")
            raise
    
    @staticmethod
    def pull_repository(local_repo_path: str, commit_id: str = "") -> Tuple[List[dict], str]:
        """拉取仓库更新"""
        try:
            if not os.path.exists(local_repo_path):
                raise Exception("仓库不存在，请先克隆仓库")
            
            repo = git.Repo(local_repo_path)
            
            # 拉取最新代码
            origin = repo.remotes.origin
            origin.pull()
            
            # 获取提交记录
            if commit_id:
                try:
                    # 获取从指定commitId到HEAD的所有提交记录
                    commits = []
                    for commit in repo.iter_commits(f'{commit_id}..HEAD'):
                        commits.append({
                            'sha': commit.hexsha,
                            'author': commit.author.name,
                            'email': commit.author.email,
                            'message': commit.message,
                            'committed_datetime': commit.committed_datetime.isoformat()
                        })
                    return commits, repo.head.commit.hexsha
                except Exception as e:
                    logging.warning(f"获取指定提交记录失败: {e}")
            
            # 返回所有提交记录
            commits = []
            for commit in repo.iter_commits():
                commits.append({
                    'sha': commit.hexsha,
                    'author': commit.author.name,
                    'email': commit.author.email,
                    'message': commit.message,
                    'committed_datetime': commit.committed_datetime.isoformat()
                })
            
            return commits, repo.head.commit.hexsha
            
        except Exception as e:
            logging.error(f"拉取仓库失败: {e}")
            raise
    
    @staticmethod
    def get_repository_info(local_repo_path: str) -> Optional[GitRepoInfo]:
        """获取仓库信息"""
        try:
            if not os.path.exists(local_repo_path):
                return None
            
            repo = git.Repo(local_repo_path)
            head = repo.head
            commit = head.commit
            
            # 从路径解析组织名和仓库名
            path_parts = local_repo_path.split(os.sep)
            if len(path_parts) >= 2:
                organization = path_parts[-2]
                repository_name = path_parts[-1]
            else:
                organization = "unknown"
                repository_name = os.path.basename(local_repo_path)
            
            return GitRepoInfo(
                local_path=local_repo_path,
                repository_name=repository_name,
                organization=organization,
                branch_name=head.ref.name,
                commit_time=commit.committed_datetime.isoformat(),
                commit_author=commit.author.name,
                commit_message=commit.message,
                version=commit.hexsha
            )
            
        except Exception as e:
            logging.error(f"获取仓库信息失败: {e}")
            return None
    
    @staticmethod
    def get_branches(local_repo_path: str) -> List[str]:
        """获取仓库分支列表"""
        try:
            if not os.path.exists(local_repo_path):
                return []
            
            repo = git.Repo(local_repo_path)
            branches = [ref.name for ref in repo.references if isinstance(ref, git.refs.RemoteReference)]
            return branches            
        except Exception as e:
            logging.error(f"获取分支列表失败: {e}")
            return []
    
    @staticmethod
    def checkout_branch(local_repo_path: str, branch_name: str) -> bool:
        """切换分支"""
        try:
            if not os.path.exists(local_repo_path):
                return False
            
            repo = git.Repo(local_repo_path)
            repo.git.checkout(branch_name)
            return True            
        except Exception as e:
            logging.error(f"切换分支失败: {e}")
            return False
    
    @staticmethod
    def get_file_history(local_repo_path: str, file_relative_path: str) -> List[dict]:
        """获取文件提交历史"""
        try:
            if not os.path.exists(local_repo_path):
                return []
            
            repo = git.Repo(local_repo_path)
            commits = []
            for commit in repo.iter_commits(paths=file_relative_path):
                commits.append({
                    'sha': commit.hexsha,
                    'author': commit.author.name,
                    'email': commit.author.email,
                    'message': commit.message,
                    'committed_datetime': commit.committed_datetime.isoformat()
                })            
            return commits            
        except Exception as e:
            logging.error(f"获取文件历史失败: {e}")
            return [] 