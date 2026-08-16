# app/logger.py
import logging
import sys
import os
from datetime import datetime
from pathlib import Path
from colorama import init, Fore, Style
from app.config.settings import settings

# 初始化 colorama
init(autoreset=True)

class ColoredFormatter(logging.Formatter):
    """彩色日志格式器"""
    
    # 颜色代码定义
    class ColorCode:
        GREEN = Fore.GREEN
        YELLOW = Fore.YELLOW
        RED = Fore.RED
        MAGENTA = Fore.MAGENTA
        CYAN = Fore.CYAN
        RESET = Style.RESET_ALL
    
    # 日志级别定义
    class LogLevel:
        INFO = 'INFO'
        WARNING = 'WARNING'
        ERROR = 'ERROR'
        FATAL = 'CRITICAL'

    def format(self, record):
        # 获取相对路径
        try:
            pathname = os.path.relpath(record.pathname)
        except ValueError:
            pathname = record.pathname
        if pathname.endswith('.py'):
            pathname = pathname[:-3]
        module_path = pathname.replace(os.sep, '.')

        # 格式化时间
        timestamp = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        
        # 获取日志级别和消息
        level = record.levelname
        message = record.getMessage()
        func_name = record.funcName
        lineno = record.lineno

        # 根据日志级别选择颜色
        color_code = {
            self.LogLevel.INFO: self.ColorCode.GREEN,
            self.LogLevel.WARNING: self.ColorCode.YELLOW,
            self.LogLevel.ERROR: self.ColorCode.RED,
            self.LogLevel.FATAL: self.ColorCode.MAGENTA
        }.get(level, self.ColorCode.RESET)

        # 构建日志格式
        log_format = (
            f"{self.ColorCode.GREEN}{timestamp}{self.ColorCode.RESET} | "
            f"{color_code}{level:8}{self.ColorCode.RESET} | "
            f"{self.ColorCode.CYAN}{module_path}:{func_name}:{lineno}{self.ColorCode.RESET} - "
            f"{message}"
        )

        if record.exc_info:
            log_format += "\n" + self.formatException(record.exc_info)
        elif record.exc_text:
            log_format += "\n" + record.exc_text

        return log_format

def setup_logging(*, console: bool = True):
    """初始化日志系统，从环境变量读取日志级别。"""
    # 禁用 Numba 调试日志
    os.environ["NUMBA_LOGGING"] = "0"
    os.environ["NUMBA_DISABLE_JIT"] = "0"
    
    # 尝试导入并配置 Numba 日志
    try:
        import numba
        numba.config.LOGGING = False
        # 设置 Numba 日志级别为 WARNING
        numba_logger = logging.getLogger('numba')
        numba_logger.setLevel(logging.WARNING)
        numba_logger.propagate = False
    except ImportError:
        pass
    
    # 从环境变量获取日志级别，默认 INFO
    log_level_str = settings.app_log_level.upper()
    level_mapping = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    log_level = level_mapping.get(log_level_str, logging.INFO)
    
    # 配置根 Logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # 清理默认 Handler
    root_logger.handlers.clear()

    # 控制台 Handler（带颜色）
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(ColoredFormatter())
        root_logger.addHandler(console_handler)

    # 文件 Handler（无颜色，纯文本）
    log_dir = Path(settings.runtime_data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(
        str(log_dir / f"app_{datetime.now().strftime('%Y%m%d')}.log"),
        encoding='utf-8'
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(pathname)s:%(funcName)s:%(lineno)d - %(message)s",
        "%Y-%m-%d %H:%M:%S"
    ))
    root_logger.addHandler(file_handler)

def set_log_level(level_str: str):
    """动态设置日志级别"""
    level_str = level_str.upper()
    level_mapping = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    
    if level_str not in level_mapping:
        raise ValueError(f"Invalid log level: {level_str}. Valid levels: {list(level_mapping.keys())}")
    
    log_level = level_mapping[level_str]
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    
    # 同时更新所有处理器的级别
    for handler in root_logger.handlers:
        handler.setLevel(log_level)
    
    logging.info(f"日志级别已动态设置为: {level_str}")


