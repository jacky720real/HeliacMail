#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日志模块配置
支持控制台输出和按天轮转的文件日志
"""

import os
import sys
import logging
from logging.handlers import TimedRotatingFileHandler
from typing import Optional, Dict, Any


def setup_logger(
    name: str = __name__,
    log_level: str = "INFO",
    log_format: Optional[str] = None,
    log_dir: str = "logs"
) -> logging.Logger:
    """
    配置并返回一个日志记录器
    
    Args:
        name: 日志记录器名称
        log_level: 日志级别 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_format: 日志格式字符串
        log_dir: 日志文件目录
    
    Returns:
        logging.Logger: 配置好的日志记录器
    """
    # 创建日志目录
    os.makedirs(log_dir, exist_ok=True)
    
    # 默认日志格式
    if log_format is None:
        log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    # 创建格式化器
    formatter = logging.Formatter(log_format)
    
    # 创建日志记录器
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, log_level.upper()))
    
    # 避免重复添加处理器
    if not logger.handlers:
        # 控制台处理器
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        
        # 文件处理器（按天轮转）
        log_file = os.path.join(log_dir, "app.log")
        file_handler = TimedRotatingFileHandler(
            log_file,
            when="midnight",
            interval=1,
            backupCount=7,
            encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


def setup_logging_from_config(config: Dict[str, Any]) -> None:
    """
    根据配置文件设置日志
    
    Args:
        config: 配置文件字典
    """
    log_config = config.get('logging', {})
    log_level = log_config.get('level', 'INFO')
    log_format = log_config.get('format')
    log_dir = "logs"  # 固定目录
    
    # 设置根日志记录器
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))
    
    # 移除所有现有的处理器（避免重复）
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # 创建日志目录
    os.makedirs(log_dir, exist_ok=True)
    
    # 默认日志格式
    if log_format is None:
        log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    # 创建格式化器
    formatter = logging.Formatter(log_format)
    
    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # 文件处理器（按天轮转）
    log_file = os.path.join(log_dir, "app.log")
    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=log_config.get('backup_count', 5),
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    # 设置一些第三方库的日志级别
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)