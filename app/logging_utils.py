"""
Logging utilities for the 1C Metadata to Neo4j Loader
"""

import logging
import json
import re
from config import settings


class Neo4jDebugFilter(logging.Filter):
    """Custom filter to truncate long database parameters in Neo4j debug logs."""

    def __init__(self, max_data_length: int = 1000):
        super().__init__()
        self.max_data_length = max_data_length
        # Pattern to match Neo4j debug logs with parameters (C: RUN, S: SUCCESS, etc.)
        self.neo4j_debug_pattern = re.compile(
            r'(\w+):\s*(.+?)(?=\s*$)',
            re.DOTALL
        )

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter and truncate Neo4j debug logs containing large parameter data."""
        if not settings.enable_debug:
            return True

        # Only process Neo4j driver debug logs
        if not record.name.startswith('neo4j'):
            return True

        try:
            message = record.getMessage()

            # Look for patterns like "C: RUN 'query' {params}" or "S: SUCCESS {result}"
            if any(prefix in message for prefix in ['C: RUN', 'S: SUCCESS', 'S: FAILURE']):
                truncated_message = self._truncate_parameters(message)
                if truncated_message != message:
                    # Update the record with truncated message
                    record.msg = truncated_message
                    record.args = ()

        except Exception:
            # If anything goes wrong, don't filter the message
            pass

        return True

    def _truncate_parameters(self, message: str) -> str:
        """Truncate large parameter values in Neo4j debug messages."""
        try:
            # Split message into parts while preserving the structure
            parts = message.split("'", 2)  # Split on first two single quotes
            if len(parts) >= 3:
                query_part = parts[0] + "'" + parts[1] + "'"
                params_part = parts[2]

                # Try to parse and truncate the parameters
                truncated_params = self._truncate_json_data(params_part, self.max_data_length)
                return query_part + truncated_params

            return message
        except Exception:
            return message

    def _truncate_json_data(self, params_str: str, max_length: int) -> str:
        """Truncate JSON parameter data to max_length characters."""
        try:
            # Remove leading/trailing whitespace and braces
            params_str = params_str.strip()
            if not params_str:
                return params_str

            # Try to parse as JSON to identify the structure
            if params_str.startswith('{') and params_str.endswith('}'):
                try:
                    data = json.loads(params_str)
                    truncated_data = self._truncate_dict_values(data, max_length)
                    return json.dumps(truncated_data, ensure_ascii=False)
                except json.JSONDecodeError:
                    pass

            # Fallback: simple character truncation
            if len(params_str) > max_length:
                return params_str[:max_length] + "... [truncated]"

            return params_str
        except Exception:
            return params_str

    def _truncate_dict_values(self, data, max_length: int):
        """Recursively truncate string values in nested dictionaries."""
        if isinstance(data, dict):
            result = {}
            current_length = 0

            for key, value in data.items():
                if isinstance(value, str) and len(value) > 100:  # Only truncate long strings
                    # Reserve space for key and JSON overhead
                    available_length = max(50, max_length - current_length - len(key) - 10)
                    if available_length > 0:
                        truncated_value = value[:available_length] + "..."
                        result[key] = truncated_value
                        current_length += len(truncated_value)
                    else:
                        result[key] = "..."
                elif isinstance(value, (dict, list)):
                    result[key] = self._truncate_dict_values(value, max_length)
                else:
                    result[key] = value

            return result
        elif isinstance(data, list):
            return [self._truncate_dict_values(item, max_length) if isinstance(item, (dict, list)) else item for item in data]
        else:
            return data


def setup_neo4j_debug_filtering():
    """Set up Neo4j debug log filtering if debug mode is enabled."""
    if settings.enable_debug:
        # Create and add the custom filter to Neo4j loggers
        debug_filter = Neo4jDebugFilter(max_data_length=settings.debug_log_max_data_length)
        for name in ("neo4j", "neo4j.bolt", "neo4j.pool", "neo4j.transport", "neo4j.io"):
            neo4j_logger = logging.getLogger(name)
            neo4j_logger.addFilter(debug_filter)
            neo4j_logger.setLevel(logging.DEBUG)
    else:
        # Suppress Neo4j driver warnings in non-debug mode
        for name in ("neo4j", "neo4j.bolt", "neo4j.pool", "neo4j.transport", "neo4j.io"):
            logging.getLogger(name).setLevel(logging.ERROR)