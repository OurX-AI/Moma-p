from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List

"""
定义通过语法数据对Repo内容解析后，生成的数据结构，用于最终形成知识图谱的节点和边
"""

# 知识图谱关系方案
#ProjectNode =(CONTAINS)->FolderNode
#ProjectNode =(CONTAINS)->FileNode
#-----
#FolderNode =(CONTAINS)->FolderNode
#FolderNode =(CONTAINS)->FileNode
#-----
#FileNode =(CONTAINS)->ClassNode
#FileNode =(CONTAINS)->FunctionNode
#ClassNode =(CONTAINS)->MethodNode
#-----
#FunctionNode/MethodNode =(CALLS)->FunctionNode/MethodNode/APINode
#----
#FileNode =(DEPENDS_ON)->FileNode
#----
#FileNode =(IMPORTS)->FunctionNode/MethodNode/APINode
#-----
#ClassNode =(DEPENDS_ON)->ClassNode 
#-----
#ClassNode =(INHERITS)->ClassNode
#ClassNode =(IMPLEMENTS)->ClassNode(Interface)
#MethodNode =(OVERRIDES)->MethodNode

class ContentType(str, Enum):
    """代码内容类型"""    
    FOLDER = "folder"    # 注释
    FILE = "file"          # 文件
    CLASS = "class"        # 类
    STRUCT = "struct"      # 结构体
    INTERFACE = "interface" # 接口
    FUNCTION = "function"  # 函数/方法

class Language(str, Enum):
    """支持的编程语言"""
    PYTHON = "python"
    JAVA = "java"
    GO = "go"
    CPP = "cpp"
    C = "c"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    RUST = "rust"
    UNKNOWN = "unknown"

class RelationType:
    # 结构关系
    CONTAINS = "CONTAINS"       # 表示所有包含关系：
                               # - Project包含Folder/File
                               # - Folder包含Folder/File
                               # - File包含Class/Function
                               # - Class包含Method
    
    # 依赖关系
    DEPENDS_ON = "DEPENDS_ON"   # 类之间的依赖关系（通过方法调用产生）
    IMPORTS = "IMPORTS"         # 文件间的导入依赖
    
    # 调用关系
    CALLS = "CALLS"            # 函数调用关系（包括普通函数、类方法和API调用）
    
    # 继承关系
    INHERITS = "INHERITS"      # 类继承
    IMPLEMENTS = "IMPLEMENTS"   # 接口实现
    OVERRIDES = "OVERRIDES"    # 方法重写父类方法

class ClassType(Enum):
    CLASS = "class"
    STRUCT = "struct"
    INTERFACE = "interface"

class FunctionType(str, Enum):
    """函数类型"""
    FUNCTION = "function"  # 普通函数
    METHOD = "method"      # 类方法
    API = "api"           # 外部API

@dataclass
class CallInfo:
    """函数调用信息"""
    name: str                    # 函数名，例如: "save", "join"
    full_name: str              # 完整方法名（包含模块路径），例如:
                               # - 普通函数: "app.utils.helper.process_data"
                               # - 类方法: "app.models.User.save"
                               # - API: "os.path.join"
    signature: str              # 方法签名，例如: "save(self,data:Dict)->bool"
                                # 这个签名的密度是函数调用点 与 函数定义点匹配，在Python中，先保持签名仅是函数名

@dataclass
class FunctionInfo:
    """函数节点（包括普通函数、类方法和API）"""
    # Key fields (必需字段，无默认值)
    name: str                   # 函数名（单纯函数名）
    full_name: str              # 完整方法名，作为函数全局唯一标识，不同语言组成方式不同
    
    signature: str             # 完整签名，包含参数类型和返回值类型
    # 函数类型，用于区分普通函数、类方法和API
    type: str  #FunctionType
    # 函数特征
    source_code: str           # 源代码
    params: List[str]         # 参数名列表
    param_types: List[str]    # 参数类型列表
    returns: List[str]        # 返回值列表
    return_types: List[str]   # 返回类型列表
    # 位置信息
    file_path: str = None      # 文件路径，API可能为None
    start_line: int = None     # 开始行
    end_line: int = None       # 结束行
    docstring: str = None     # 文档字符串
    # 方法特有属性
    class_name: str = None     # 所属类名（方法类型时使用）
    #is_override: bool = False  # 是否重写父类方法  AST解析时无法识别
    accessed_attrs: List[str] = None  # 访问的类属性（方法类型时使用）
    # API特有属性
    api_doc: str = None       # API文档
    # 调用关系
    calls: List[CallInfo] = None  # 此函数调用的其他函数

@dataclass
class ClassInfo:
    """类节点（包含struct等类似概念）"""
    # Key fields
    name: str        # 类名
    full_name: str   # 完整类名（包含模块路径），例如: "app.models.user.User"
    
    # Non-key fields
    # 位置信息    
    file_path: str = None   # 文件路径，用于区分不同文件中的同名类
    node_type: str = None   # NodeType = NodeType.CLASS    
    source_code: str = None
    start_line: int = None     # 开始行
    end_line: int = None       # 结束行
    methods: List[FunctionInfo] = None
    attributes: List[str] = None
    base_classes: List['ClassInfo'] = None  # 父类节点列表
    docstring: Optional[str] = None

@dataclass
class FileInfo:
    """文件节点"""
    # Key fields
    name: str  # 文件名
    file_path: str
    
    # Non-key fields
    language: str   #Language
    functions: List[FunctionInfo]
    classes: List[ClassInfo]
    imports: List[str]
    dependent_files: List[str] = field(default_factory=list)

@dataclass
class FolderInfo:
    """文件夹节点"""
    # Key fields
    name: str
    path: str
    
    # Non-key fields
    files: List[FileInfo]
    subfolders: List['FolderInfo']