from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional
import numpy as np

# 搜索表达式相关定义
DEFAULT_MATCH_VECTOR_TOPN = 10
DEFAULT_MATCH_SPARSE_TOPN = 10
VEC = list | np.ndarray


@dataclass
class SparseVector:
    """稀疏向量"""
    indices: list[int]
    values: Optional[list[float | int]] = None

    def __post_init__(self):
        if self.values is not None:
            assert len(self.indices) == len(self.values)

    def to_dict_old(self) -> dict[str, Any]:
        """转换为旧格式字典"""
        d = {"indices": self.indices}
        if self.values is not None:
            d["values"] = self.values
        return d

    def to_dict(self) -> dict[str, float | int]:
        """转换为字典格式"""
        if self.values is None:
            raise ValueError("SparseVector.values is None")
        result = {}
        for i, v in zip(self.indices, self.values):
            result[str(i)] = v
        return result

    @staticmethod
    def from_dict(d: dict[str, Any]) -> 'SparseVector':
        """从字典创建SparseVector"""
        return SparseVector(d["indices"], d.get("values"))

    def __str__(self) -> str:
        return f"SparseVector(indices={self.indices}{'' if self.values is None else f', values={self.values}'})"

    def __repr__(self) -> str:
        return str(self)


class MatchTextExpr(ABC):
    """文本搜索表达式"""
    def __init__(
        self,
        fields: list[str],
        matching_text: str,
        topn: int,
        extra_options: Optional[dict[str, Any]] = None,
    ):
        self.fields = fields
        self.matching_text = matching_text
        self.topn = topn
        self.extra_options = extra_options or {}


class MatchDenseExpr(ABC):
    """密集向量搜索表达式"""
    def __init__(
        self,
        vector_column_name: str,
        embedding_data: VEC,
        embedding_data_type: str,
        distance_type: str,
        topn: int = DEFAULT_MATCH_VECTOR_TOPN,
        extra_options: Optional[dict[str, Any]] = None,
    ):
        self.vector_column_name = vector_column_name
        self.embedding_data = embedding_data
        self.embedding_data_type = embedding_data_type
        self.distance_type = distance_type
        self.topn = topn
        self.extra_options = extra_options or {}


class MatchSparseExpr(ABC):
    """稀疏向量搜索表达式"""
    def __init__(
        self,
        vector_column_name: str,
        sparse_data: SparseVector | dict[str, Any],
        distance_type: str,
        topn: int,
        opt_params: Optional[dict[str, Any]] = None,
    ):
        self.vector_column_name = vector_column_name
        self.sparse_data = sparse_data
        self.distance_type = distance_type
        self.topn = topn
        self.opt_params = opt_params or {}


class MatchTensorExpr(ABC):
    """张量搜索表达式"""
    def __init__(
        self,
        column_name: str,
        query_data: VEC,
        query_data_type: str,
        topn: int,
        extra_option: Optional[dict[str, Any]] = None,
    ):
        self.column_name = column_name
        self.query_data = query_data
        self.query_data_type = query_data_type
        self.topn = topn
        self.extra_option = extra_option or {}


class FusionExpr(ABC):
    """融合搜索表达式"""
    def __init__(
        self,
        method: str,
        topn: int,
        fusion_params: Optional[dict[str, Any]] = None,
    ):
        self.method = method
        self.topn = topn
        self.fusion_params = fusion_params or {}


# 搜索表达式联合类型
MatchExpr = MatchTextExpr | MatchDenseExpr | MatchSparseExpr | MatchTensorExpr | FusionExpr


class SortOrder(Enum):
    """排序方向枚举"""
    ASC = "asc"      # 升序
    DESC = "desc"    # 降序


class SortFieldType(Enum):
    """排序字段类型枚举 - 用于排序的字段类型定义"""
    # 文本类型
    TEXT = "text"
    
    # 数字类型
    FLOAT = "float"         # 单精度浮点数
    DOUBLE = "double"       # 双精度浮点数
    LONG = "long"           # 长整型
    INTEGER = "integer"     # 整型
    
    # 其他类型
    DATE = "date"           # 日期类型
    BOOLEAN = "boolean"     # 布尔类型
    GEO_POINT = "geo_point" # 地理位置类型


class SortMode(Enum):
    """多值字段排序模式枚举"""
    MIN = "min"         # 最小值
    MAX = "max"         # 最大值
    AVG = "avg"         # 平均值
    SUM = "sum"         # 求和
    MEDIAN = "median"   # 中位数


@dataclass
class SortField:
    """
    排序字段配置 - 完整参数设计，支持所有排序场景
    
    Attributes:
        sort_field: 排序字段名
        sort_order: 排序方向 ("asc" 升序 或 "desc" 降序)
        sort_mode: 对于多值字段的排序模式 (使用 SortMode 枚举)
        sort_unmapped_type: 当字段不存在时的默认类型 (使用 SortFieldType 枚举)
        sort_numeric_type: 数字类型的具体类型 (使用 SortFieldType 枚举)
    """
    sort_field: str
    sort_order: str  # 使用 SortOrder 枚举值
    sort_mode: Optional[str] = None  # 使用 SortMode 枚举值
    sort_unmapped_type: Optional[str] = None  # 使用 SortFieldType 枚举值
    sort_numeric_type: Optional[str] = None  # 使用 SortFieldType 枚举值
    
    @classmethod
    def simple_field(cls, field: str, order: SortOrder, 
                    unmapped_type: Optional[SortFieldType] = None) -> "SortField":
        """
        创建简单字段排序（适用于文本、数值、日期等所有类型）
        
        Args:
            field: 字段名
            order: 排序方向
            unmapped_type: 未映射类型 (可选)
        """
        return cls(
            sort_field=field, 
            sort_order=order.value, 
            sort_unmapped_type=unmapped_type.value if unmapped_type else None
        )
    
    @classmethod
    def multi_value_field(cls, field: str, order: SortOrder, 
                         mode: SortMode,
                         unmapped_type: SortFieldType,
                         numeric_type: SortFieldType) -> "SortField":
        """
        创建多值字段排序（主要用于 page_num_int, top_int 等特殊字段）
        
        Args:
            field: 字段名
            order: 排序方向
            mode: 多值排序模式
            unmapped_type: 未映射类型
            numeric_type: 数字类型
        """
        return cls(
            sort_field=field, 
            sort_order=order.value, 
            sort_mode=mode.value,
            sort_unmapped_type=unmapped_type.value,
            sort_numeric_type=numeric_type.value
        )

class RankFeature(ABC):
    """排名特征"""
    def __init__(
        self,
        fields: dict[str, Any], # key是字段名，value是排序权重
        exclude_fields: list[str] = None,
        field_prefix: str = "",
    ):
        self.fields = fields
        self.exclude_fields = exclude_fields
        self.field_prefix = field_prefix


@dataclass
class SearchRequest:
    """统一的搜索请求对象"""
    
    # 字段选择和高亮（保持原始参数名）
    select_fields: Optional[list[str]] = None
    highlight_fields: Optional[list[str]] = None
    
    # 过滤条件（保持原始参数名）
    condition: Optional[dict[str, Any]] = None
    
    # 搜索表达式（保持原始参数名）
    match_exprs: Optional[list[MatchExpr]] = None
    
    # 排序（保持原始参数名，但支持详细排序类型）
    order_by: Optional[list[SortField]] = None
    
    # 分页
    offset: int = 0
    limit: int = 10
    
    # 聚合字段（保持原始参数名）
    agg_fields: Optional[list[str]] = None
    
    # 排名特征（保持原始参数名）
    rank_feature: Optional[RankFeature] = None


class VectorStoreConnection(ABC):
    """
    向量存储连接抽象基类
    提供统一的向量存储接口，屏蔽不同数据库的实现差异
    支持文档、代码、知识等多种类型的数据存储和向量搜索
    """
    @abstractmethod
    def get_db_type(self) -> str:
        """返回数据库类型"""
        raise NotImplementedError("Not implemented")
    
    @abstractmethod
    async def close(self):
        """关闭连接"""
        raise NotImplementedError("Not implemented")

    @abstractmethod
    async def health_check(self) -> dict[str, Any]:
        """健康检查"""
        raise NotImplementedError("Not implemented")

    # 索引管理接口
    @abstractmethod
    async def create_space(self, space_name: str, vector_size: int, **kwargs) -> bool:
        """
        创建索引
        Args:
            space_name: 空间名称
            vector_size: 向量维度
            **kwargs: 其他参数
        """
        raise NotImplementedError("Not implemented")

    @abstractmethod
    async def delete_space(self, space_name: str, **kwargs) -> bool:
        """
        删除索引
        Args:
            space_name: 空间名称
            **kwargs: 其他参数
        """
        raise NotImplementedError("Not implemented")

    @abstractmethod
    async def space_exists(self, space_name: str, **kwargs) -> bool:
        """
        检查索引是否存在
        Args:
            space_name: 空间名称
            **kwargs: 其他参数
        """
        raise NotImplementedError("Not implemented")

    # 文档CRUD接口
    @abstractmethod
    async def insert_records(self, space_name: str, records: list[dict[str, Any]], **kwargs) -> list[str]:
        """
        批量插入数据记录
        Args:
            space_name: 空间名称
            records: 数据记录列表（字典格式）
            **kwargs: 其他参数
        Returns:
            插入成功的记录ID列表
        """
        raise NotImplementedError("Not implemented")

    @abstractmethod
    async def update_records(self, space_name: str, condition: dict[str, Any], new_value: dict[str, Any], fields_to_remove: list[str] = None, **kwargs) -> bool:
        """
        批量更新数据记录
        Args:
            space_name: 空间名称
            condition: 更新条件，支持id、exists、terms、term等查询条件
            new_value: 新的字段值，支持remove、add等特殊操作
            fields_to_remove: 需要先删除的字段列表
            **kwargs: 其他参数
        """
        raise NotImplementedError("Not implemented")

    @abstractmethod
    async def delete_records(self, space_name: str, condition: dict[str, Any], **kwargs) -> int:
        """
        批量删除数据记录
        Args:
            space_name: 空间名称
            condition: 删除条件，支持id、exists、terms、term等查询条件
            **kwargs: 其他参数
        """
        raise NotImplementedError("Not implemented")

    @abstractmethod
    async def get_record(self, space_names: list[str], record_id: str, **kwargs) -> Optional[dict[str, Any]]:
        """
        获取单个数据记录
        Args:
            record_id: 记录ID
            index_name: 索引名称
            **kwargs: 其他参数
        Returns:
            数据记录字典，如果不存在则返回None
        """
        raise NotImplementedError("Not implemented")

    async def list_records(
        self,
        space_name: str,
        *,
        condition: Optional[dict[str, Any]] = None,
        select_fields: Optional[list[str]] = None,
        limit: int = 500,
        **kwargs,
    ) -> list[dict[str, Any]]:
        """按条件列出记录（非向量检索）。默认未实现，由具体后端覆盖。"""
        raise NotImplementedError(f"{self.get_db_type()} 未实现 list_records")

    # 搜索接口
    @abstractmethod
    async def search(self, space_names: list[str], request: SearchRequest, **kwargs) -> dict[str, Any]:
        """
        搜索数据记录
        Args:
            space_names: 空间名称列表
            request: 搜索请求
            **kwargs: 其他参数
        Returns:
            搜索结果字典，包含 documents, total, highlights, aggregations 等字段
        """
        raise NotImplementedError("Not implemented")

    """
    Helper functions for search result
    """
    # 获取总数
    @abstractmethod
    def get_total(self, result) -> int:
        raise NotImplementedError("Not implemented")

    # 获取Chunk IDs
    @abstractmethod
    def get_chunk_ids(self, result) -> list[str]:
        raise NotImplementedError("Not implemented")

    # 获取Fields
    @abstractmethod
    def get_fields(self, result, fields: list[str]) -> dict[str, dict]:
        raise NotImplementedError("Not implemented")

    # 获取Highlight
    @abstractmethod
    def get_highlight(self, result, keywords: list[str], field_name: str) -> dict[str, Any]:
        raise NotImplementedError("Not implemented")

    # 获取Aggregation
    @abstractmethod
    def get_aggregation(self, result, field_name: str) -> dict[str, Any]:
        raise NotImplementedError("Not implemented")

    """
    SQL
    """
    # 执行SQL
    @abstractmethod
    async def sql(self, sql: str, fetch_size: int, format: str):
        """
        Run the sql generated by text-to-sql
        """
        raise NotImplementedError("Not implemented")
