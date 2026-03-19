"""Configuration module for yeoman."""

from yeoman_shared.config.loader import get_config_path, load_config
from yeoman_shared.config.schema import Config

__all__ = ["Config", "load_config", "get_config_path"]
