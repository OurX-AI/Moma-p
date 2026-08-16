from enum import Enum


class ContentType(str, Enum):
    """代码内容类型"""    
    FOLDER = "folder"    # 注释
    FILE = "file"          # 文件
    CLASS = "class"        # 类
    STRUCT = "struct"      # 结构体
    INTERFACE = "interface" # 接口
    FUNCTION = "function"  # 函数/方法
    CODE_CHUNK = "code_chunk"  # 代码片段