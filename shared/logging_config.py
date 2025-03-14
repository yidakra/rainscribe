#!/usr/bin/env python3
"""
Logging Configuration Module

This module provides standardized logging configuration for all services 
in the rainscribe system.
"""

import os
import sys
import json
import logging
import logging.config
import logging.handlers
from datetime import datetime
from typing import Dict, Optional, Union, List, Any

# Default log directory
DEFAULT_LOG_DIR = os.environ.get("LOG_DIR", os.path.expanduser("~/.rainscribe/logs"))

# Default logging levels
DEFAULT_CONSOLE_LEVEL = os.environ.get("CONSOLE_LOG_LEVEL", "INFO")
DEFAULT_FILE_LEVEL = os.environ.get("FILE_LOG_LEVEL", "DEBUG")

# Ensure log directory exists
os.makedirs(DEFAULT_LOG_DIR, exist_ok=True)

# Format strings
DETAILED_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s"
SIMPLE_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
JSON_FORMAT = {
    "timestamp": "%(asctime)s",
    "name": "%(name)s",
    "level": "%(levelname)s",
    "file": "%(filename)s",
    "line": "%(lineno)d",
    "message": "%(message)s"
}

class JsonFormatter(logging.Formatter):
    """Custom formatter that outputs log records as JSON objects."""
    
    def __init__(self, fmt_dict: Optional[Dict] = None):
        """
        Initialize the JSON formatter.
        
        Args:
            fmt_dict: Format dictionary (keys are output keys, values are log record attributes)
        """
        self.fmt_dict = fmt_dict or JSON_FORMAT
        super().__init__()
    
    def format(self, record: logging.LogRecord) -> str:
        """
        Format the log record as JSON.
        
        Args:
            record: The log record to format
            
        Returns:
            str: JSON-formatted log record
        """
        record_dict = {}
        
        # Apply standard formatting to the record
        record.asctime = self.formatTime(record)
        
        # Extract fields from the record
        for key, fmt in self.fmt_dict.items():
            try:
                record_dict[key] = fmt % record.__dict__
            except Exception:
                record_dict[key] = fmt
        
        # Add exception info if present
        if record.exc_info:
            record_dict["exception"] = self.formatException(record.exc_info)
        
        # Add any extra attributes
        for key, value in record.__dict__.items():
            if key not in ["args", "exc_info", "exc_text", "msg", "message"] and not key.startswith("_"):
                if key not in record_dict and isinstance(value, (str, int, float, bool, type(None))):
                    record_dict[key] = value
        
        return json.dumps(record_dict)

def get_rotating_file_handler(
    log_file: str,
    level: str = DEFAULT_FILE_LEVEL,
    max_bytes: int = 10485760,  # 10MB
    backup_count: int = 10,
    formatter: str = "detailed"
) -> logging.Handler:
    """
    Create a rotating file handler.
    
    Args:
        log_file: Path to the log file
        level: Logging level
        max_bytes: Maximum file size before rotation
        backup_count: Number of backup files to keep
        formatter: Formatter to use
        
    Returns:
        logging.Handler: Configured handler
    """
    handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count
    )
    handler.setLevel(level)
    
    if formatter == "json":
        handler.setFormatter(JsonFormatter())
    elif formatter == "simple":
        handler.setFormatter(logging.Formatter(SIMPLE_FORMAT))
    else:
        handler.setFormatter(logging.Formatter(DETAILED_FORMAT))
    
    return handler

def get_console_handler(
    level: str = DEFAULT_CONSOLE_LEVEL,
    formatter: str = "simple"
) -> logging.Handler:
    """
    Create a console handler.
    
    Args:
        level: Logging level
        formatter: Formatter to use
        
    Returns:
        logging.Handler: Configured handler
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    
    if formatter == "json":
        handler.setFormatter(JsonFormatter())
    elif formatter == "detailed":
        handler.setFormatter(logging.Formatter(DETAILED_FORMAT))
    else:
        handler.setFormatter(logging.Formatter(SIMPLE_FORMAT))
    
    return handler

def configure_logging(
    service_name: str,
    console_level: str = DEFAULT_CONSOLE_LEVEL,
    file_level: str = DEFAULT_FILE_LEVEL,
    log_dir: str = DEFAULT_LOG_DIR,
    enable_console: bool = True,
    enable_file: bool = True,
    log_format: str = "detailed",
    json_logs: bool = False
) -> logging.Logger:
    """
    Configure logging for a service.
    
    Args:
        service_name: Name of the service
        console_level: Console logging level
        file_level: File logging level
        log_dir: Directory for log files
        enable_console: Enable console logging
        enable_file: Enable file logging
        log_format: Format for logs ("simple", "detailed")
        json_logs: Whether to format logs as JSON
        
    Returns:
        logging.Logger: Configured logger
    """
    # Create logger
    logger = logging.getLogger(service_name)
    logger.setLevel(logging.DEBUG)  # Set to lowest level, handlers will filter
    
    # Remove any existing handlers
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    # Use JSON format if requested
    if json_logs:
        log_format = "json"
    
    # Add console handler if enabled
    if enable_console:
        console_handler = get_console_handler(console_level, log_format)
        logger.addHandler(console_handler)
    
    # Add file handler if enabled
    if enable_file:
        # Create log file path
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"{service_name}.log")
        
        file_handler = get_rotating_file_handler(log_file, file_level, formatter=log_format)
        logger.addHandler(file_handler)
    
    # Don't propagate to root logger
    logger.propagate = False
    
    logger.info(f"Logging configured for {service_name}")
    return logger

def load_config_from_file(config_file: str) -> Dict:
    """
    Load logging configuration from a file.
    
    Args:
        config_file: Path to configuration file (JSON or YAML)
        
    Returns:
        Dict: Configuration dictionary
    """
    if not os.path.exists(config_file):
        raise FileNotFoundError(f"Logging config file not found: {config_file}")
    
    with open(config_file, 'r') as f:
        if config_file.endswith('.json'):
            config = json.load(f)
        else:
            # Assume YAML
            try:
                import yaml
                config = yaml.safe_load(f)
            except ImportError:
                raise ImportError("YAML configuration requires PyYAML package")
    
    return config

def configure_from_file(config_file: str) -> None:
    """
    Configure logging from a configuration file.
    
    Args:
        config_file: Path to configuration file
    """
    config = load_config_from_file(config_file)
    logging.config.dictConfig(config)
    logging.info(f"Logging configured from file: {config_file}")

def get_default_config(service_name: str) -> Dict:
    """
    Get a default logging configuration dictionary.
    
    Args:
        service_name: Name of the service
        
    Returns:
        Dict: Configuration dictionary
    """
    log_file = os.path.join(DEFAULT_LOG_DIR, f"{service_name}.log")
    
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "simple": {
                "format": SIMPLE_FORMAT
            },
            "detailed": {
                "format": DETAILED_FORMAT
            },
            "json": {
                "()": "shared.logging_config.JsonFormatter"
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": DEFAULT_CONSOLE_LEVEL,
                "formatter": "simple",
                "stream": "ext://sys.stdout"
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": DEFAULT_FILE_LEVEL,
                "formatter": "detailed",
                "filename": log_file,
                "maxBytes": 10485760,
                "backupCount": 10
            }
        },
        "loggers": {
            service_name: {
                "level": "DEBUG",
                "handlers": ["console", "file"],
                "propagate": False
            }
        },
        "root": {
            "level": "INFO",
            "handlers": ["console"]
        }
    }

if __name__ == "__main__":
    # Test the logging configuration
    service_name = "test-service"
    logger = configure_logging(service_name)
    
    logger.debug("This is a debug message")
    logger.info("This is an info message")
    logger.warning("This is a warning message")
    logger.error("This is an error message")
    try:
        1/0
    except Exception as e:
        logger.exception("This is an exception message")
    
    # Test JSON formatter
    json_logger = configure_logging(f"{service_name}-json", json_logs=True)
    json_logger.info("This is a JSON formatted log message")
    
    # Test logging to file only
    file_logger = configure_logging(
        f"{service_name}-file-only",
        enable_console=False,
        enable_file=True
    )
    file_logger.info("This message should only go to file") 