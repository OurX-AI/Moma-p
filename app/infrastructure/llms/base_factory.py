import hashlib
import json
import os
import threading
import time
from abc import ABC
from collections import OrderedDict
from copy import copy
from typing import Any, Dict, Generic, List, Optional, Set, Tuple, Type, TypeVar
from app.config.settings import MODELS_CONFIG_DIR


DEFAULT_CACHE_TTL_SECONDS = 3600

# 模型熔断常量
MODEL_FAILS_BEFORE_BLOCK = 3
MODEL_BLOCK_SECONDS = 600.0
MAX_RETRY_MODEL_PAIRS = 2

T = TypeVar('T')

class BaseModelFactory(ABC, Generic[T]):
    """模型工厂基类，提供通用的模型管理功能"""
    
    @property
    def _models(self) -> Dict[str, Type[T]]:
        """模型类映射字典，子类可覆写。默认返回空映射。"""
        return {}

    def __init__(self, config_filename: str):
        """
        初始化工厂类

        Args:
            config_filename: 配置文件名称
        """
        self.config_path = str(MODELS_CONFIG_DIR / config_filename)
        self._config = None
        # 实例缓存
        self._instance_cache_lock = threading.Lock()
        self._instance_cache: OrderedDict = OrderedDict()
        self._cache_ttl_seconds = DEFAULT_CACHE_TTL_SECONDS
        # 模型熔断
        self._model_state_lock = threading.Lock()
        self._model_fail_counts: Dict[Tuple[str, str], int] = {}
        self._model_blocked_until: Dict[Tuple[str, str], float] = {}
        self.load_config()

    def clear_instance_cache(self):
        with self._instance_cache_lock:
            self._instance_cache.clear()
    
    def load_config(self):
        """加载模型配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self._config = json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"模型配置文件未找到: {self.config_path}")
        except json.JSONDecodeError as e:
            raise ValueError(f"模型配置文件格式错误: {self.config_path} {e}")
    
    def get_supported_models(self) -> Dict[str, Any]:
        """
        获取支持的供应方和模型支持的全集（只返回is_valid为1的），并附带默认模型。
        
        Returns:
            Dict[str, Any]: {
                "supported": {
                    "provider_name": {
                        "description": "provider_description",
                        "models": { "model_name": { "description": "model_description" } }
                    }
                },
                "default": {"provider": "...", "model": "..."} 或 None
            }
        """
        result = {}
        all_models = self._config.get("models", {})
        
        for provider_name, provider_config in all_models.items():
            if provider_config.get("is_valid", 0) == 1:
                provider_info = {
                    "description": provider_config.get("description", ""),
                    "models": {},
                }
                instances = provider_config.get("instances", {})
                for model_name, model_config in instances.items():
                    provider_info["models"][model_name] = {
                        "description": model_config.get("description", ""),
                    }
                if provider_info["models"]:
                    result[provider_name] = provider_info
        try:
            provider, model = self.get_default_model()
            default = {"provider": provider, "model": model}
        except Exception:
            default = None
        return {"supported": result, "default": default}

    def if_model_support(self, provider: str, model: str) -> bool:
        """
        判断模型是否支持
        """
        models = self._config.get("models", {})
        if provider not in models:
            return False

        if models[provider].get("is_valid", 0) != 1:
            return False

        if model not in models[provider].get("instances", {}):
            return False

        return True

    def get_model_info_by_name(self, model: str) -> Dict[str, Any]:
        """
        根据模型名称获取模型信息
        编译当前配置文件中所有Provider下的模型，找到名字匹配的模型
        
        Args:
            model: 模型名称
            
        Returns:
            Dict[str, Any]: 包含以下字段的字典：
            {
                "provider": str,        # 供应商名称
                "model_name": str,      # 模型名称
                "provider_info": Dict,  # 供应商信息
                "model_info": Dict      # 模型信息
            }
        """
        models = self._config.get("models", {})
        
        # 遍历所有provider
        for provider_name, provider_config in models.items():
            # 只处理is_valid为1的provider
            if provider_config.get("is_valid", 0) == 1:
                instances = provider_config.get("instances", {})
                
                # 检查该provider下是否有匹配的模型
                if model in instances:
                    return {
                        "provider": provider_name,
                        "model_name": model,
                        "provider_info": {
                            "description": provider_config.get("description", ""),
                            "api_key": provider_config.get("api_key", ""),
                            "base_url": provider_config.get("base_url", ""),
                            "is_valid": provider_config.get("is_valid", 0)
                        },
                        "model_info": instances[model]
                    }
        
        # 如果没有找到匹配的模型
        raise ValueError(f"未找到模型 '{model}'，请检查模型名称是否正确")
        
    def get_default_model(self) -> tuple[str, str]:
        """
        获取默认模型
        
        Returns:
            tuple[str, str]: (供应商名字, 模型名字)
        """
        default_config = self._config.get("default", {})
        
        provider = default_config.get("provider", "")
        model = default_config.get("model", "")

        return provider, model
    
    def get_model_params(self, provider: str, model: str) -> Dict[str, Any]:
        """
        获取模型参数（合并provider和model配置）
        
        Args:
            provider: 供应商名称
            model: 模型名称
            
        Returns:
            Dict[str, Any]: 包含以下字段的字典：
            {
                "success": bool,  # 成功标志
                "api_key": str,   # API密钥
                "base_url": str,  # 基础URL
                "api_type": str,  # API接口类型 (openai/anthropic)
                "model_params": Dict[str, Any]  # 模型参数集
            }
        """
        try:
            # 直接获取配置
            models = self._config.get("models", {})
            if provider not in models:
                raise ValueError(f"不支持的模型供应商: {provider}")
            
            provider_config = models[provider]
            
            # 检查provider是否有效
            if provider_config.get("is_valid", 0) != 1:
                raise ValueError(f"供应商 {provider} 未启用")
            
            instances = provider_config.get("instances", {})
            if model not in instances:
                raise ValueError(f"供应商 {provider} 不支持模型: {model}")
            
            model_config = instances[model]
            
            return {
                "success": True,
                "api_key": provider_config.get("api_key", ""),
                "base_url": provider_config.get("base_url", ""),
                "api_type": provider_config.get("api_type", "openai"),
                "model_params": model_config
            }
        except Exception as e:
            return {
                "success": False,
                "api_key": "",
                "base_url": "",
                "api_type": "",
                "model_params": {},
                "error": str(e)
            }
    
    def create_model(self, 
        provider: Optional[str] = None, 
        model: Optional[str] = None, 
        api_key: Optional[str] = None, 
        language: Optional[str] = "Chinese", 
        **kwargs) -> T:
        """
        创建模型实例
        
        Args:
            provider: 供应商名称，如果为None则使用默认值
            model: 模型名称，如果为None则使用默认值
            api_key: API密钥，如果为None则使用配置中的值
            **kwargs: 其他参数
            
        Returns:
            T: 模型实例
        """

        default_provider, default_model = self.get_default_model()

        # 未指定则使用默认模型
        if not model:
            provider, model = default_provider, default_model
        # 只指定了模型名字，根据模型名字找provider
        elif not provider:
            info = self.get_model_info_by_name(model)
            if info:
                provider = info["provider"]
                model = info["model_name"]
            else:
                provider, model = default_provider, default_model
        # 指定的模型无效，也使用默认模型
        elif not self.if_model_support(provider, model):
            provider, model = default_provider, default_model
        
        # 获取模型参数
        model_para = self.get_model_params(provider, model)
        if not model_para["success"]:
            raise ValueError(f"获取模型参数失败: {model_para.get('error', '未知错误')}")

        # 合并模型参数
        config = copy(kwargs)
        for key, value in model_para["model_params"].items():
            if key not in config:
                config[key] = value

        # 获取模型信息
        api_key = api_key or model_para["api_key"] or ""
        base_url = model_para["base_url"] or ""

        # 计算缓存key（失败则跳过缓存）
        cache_key = None
        try:
            api_key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16] if api_key else ""
            config_key = json.dumps(config, ensure_ascii=False, sort_keys=True, default=str)
            cache_key = (provider, model, language or "", base_url, api_key_hash, config_key)
        except Exception:
            cache_key = None

        if cache_key is not None:
            with self._instance_cache_lock:
                entry = self._instance_cache.get(cache_key)
                if entry is not None:
                    instance, created_at = entry
                    if self._cache_ttl_seconds is not None and created_at > 0:
                        if time.time() - created_at > self._cache_ttl_seconds:
                            del self._instance_cache[cache_key]
                            entry = None
                    if entry is not None:
                        self._instance_cache.move_to_end(cache_key)
                        return instance
        
        # 获取模型类
        model_class = self._get_model_class(provider, model_para)
        
        # 创建模型实例
        instance = model_class(
            api_key=api_key,
            model_provider=provider,
            model_name=model,
            base_url=base_url,
            language=language,
            **config
        )

        if cache_key is not None:
            with self._instance_cache_lock:
                created_at = time.time() if self._cache_ttl_seconds is not None else 0
                self._instance_cache[cache_key] = (instance, created_at)
                self._instance_cache.move_to_end(cache_key)

        return instance
    
    def _get_model_class(self, provider: str, model_para: Dict[str, Any] = None) -> Type[T]:
        model_class = self._models.get(provider)
        if not model_class:
            raise ValueError(f"未知的模型类: {provider}")
        return model_class

    def _is_model_blocked(self, provider: str, model: str) -> bool:
        """判断模型是否处于熔断状态"""
        k = (provider, model)
        with self._model_state_lock:
            until = self._model_blocked_until.get(k)
            if until is None:
                return False
            if time.monotonic() < until:
                return True
            del self._model_blocked_until[k]
            return False

    def _clear_model_block(self, provider: str, model: str) -> None:
        """清除模型熔断状态"""
        k = (provider, model)
        with self._model_state_lock:
            self._model_fail_counts.pop(k, None)
            self._model_blocked_until.pop(k, None)

    def _count_model_failure(self, provider: str, model: str) -> None:
        """计数模型失败次数"""
        k = (provider, model)
        with self._model_state_lock:
            n = self._model_fail_counts.get(k, 0) + 1
            if n >= self._MODEL_FAILS_BEFORE_BLOCK:
                self._model_blocked_until[k] = time.monotonic() + self._MODEL_BLOCK_SECONDS
                self._model_fail_counts.pop(k, None)
            else:
                self._model_fail_counts[k] = n

    def _get_retry_model_pairs(
        self,
        model_provider: Optional[str],
        model_name: Optional[str],
    ) -> List[Tuple[str, str]]:
        """获取可用的重试（失败后换用）模型列表"""
        supported = (self.get_supported_models().get("supported") or {})
        valid_list: List[Tuple[str, str]] = []
        for pname, pinfo in supported.items():
            for mn in (pinfo.get("models") or {}).keys():
                valid_list.append((pname, mn))
        valid_list.sort(key=lambda x: (x[0], x[1]))

        # 去掉用户指定的模型外的其他模型作为重试模型
        retry_list =  [(p, m) for p, m in valid_list if (p, m) != (model_provider, model_name)]
        return retry_list[:MAX_RETRY_MODEL_PAIRS]
