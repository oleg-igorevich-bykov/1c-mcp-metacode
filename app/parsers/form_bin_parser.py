"""
FormBinParser: extracts 1C form module code from Form.bin files (ordinary forms).

Form.bin files contain both form layout information and module code.
This parser extracts ONLY the module code for BSL signature scanning.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, List, Tuple
import re
import logging

logger = logging.getLogger(__name__)


class FormBinParser:
    def __init__(self, code_path: Path):
        self.code_path = code_path
        # ZWNBSP (Zero Width No-Break Space) character - U+FEFF
        self.zwnbsp = '\ufeff'

    def parse(self, file_path: Path, content: Optional[str] = None) -> Tuple[List[str], str]:
        """
        Parses a Form.bin file to extract 1C form module code for indexing.

        Start of code is determined by two consecutive lines:
          1) A line that ends with "m o d u l e" where letters may be separated by NULs,
             and may have NULs/spaces after 'e'.
          2) The next line matches: "<8 hex> <8 hex> 7fffffff"

        End of code is whichever appears first after the start pair:
          - a line matching the same hex/7fffffff pattern: "<8 hex> <8 hex> 7fffffff"
          - OR a line that starts with "{" strictly at the beginning of the line

        The code to index is everything between those marker lines (excluding them).

        Args:
            file_path: Path to the file (used for logging and module path generation)
            content: Pre-read file content. If None, will read the file.

        Returns:
            Tuple of (code_chunks: List[str], module_path_line: str)
            code_chunks contains extracted code fragments (usually one)
            module_path_line is a comment line with the file path
        """
        if content is None:
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
            except Exception as e:
                logger.error(f"Error reading form file {file_path}: {e}")
                return [], ""

        lines = content.splitlines()

        # Matches a line that ends with "m o d u l e" with possible NULs/spaces between letters and after 'e'
        module_tail_regex = re.compile(
            r'(?:m\x00?\s*o\x00?\s*d\x00?\s*u\x00?\s*l\x00?\s*e[\x00\s]*)$',
            re.IGNORECASE
        )
        # Matches a line like "0003a6b2 0003a6b2 7fffffff" (8 hex, space, 8 hex, space, 7fffffff)
        hex_line_regex = re.compile(r'^[0-9a-fA-F]{8}\s+[0-9a-fA-F]{8}\s+7fffffff\s*$', re.IGNORECASE)

        start_index = None
        for i in range(len(lines) - 1):
            if module_tail_regex.search(lines[i]):
                if hex_line_regex.match(lines[i + 1]):
                    start_index = i + 2  # code starts after these two lines
                    break

        if start_index is None:
            logger.debug(f"Form.bin start markers not found in {file_path}")
            return [], ""

        end_index = None
        curly_line_regex = re.compile(r'^\{')
        for j in range(start_index, len(lines)):
            line_j = lines[j]
            if hex_line_regex.match(line_j) or curly_line_regex.match(line_j):
                end_index = j
                break

        if end_index is None:
            # Fallback: take until EOF if no end marker is found
            end_index = len(lines)

        code_lines = lines[start_index:end_index]
        code_text = "\n".join(code_lines).rstrip()

        # Cleanup: remove ZWNBSP and common non-printable control chars (e.g., NUL) from the fragment
        if self.zwnbsp in code_text:
            code_text = code_text.replace(self.zwnbsp, '')
        # Remove NUL bytes that may be present in binary-derived text
        code_text = code_text.replace('\x00', '')
        # Remove additional zero-width characters if present
        code_text = re.sub(r'[\u200B\u200C\u200D\u2060]', '', code_text)

        # If nothing remains after cleanup, or only non-printable characters remain, treat as no code
        if not code_text.strip():
            logger.debug(f"No code content found between start/end markers in {file_path} after cleanup")
            return [], ""

        has_visible = any(ch.isprintable() and not ch.isspace() for ch in code_text)
        if not has_visible:
            logger.debug(f"Only non-printable characters present in extracted code for {file_path}. Skipping.")
            return [], ""

        chunks = [code_text]

        # Generate module path line (comment with file path for reference)
        module_path_line = self._translate_form_path_to_module_path(file_path)

        # Combine all found chunks into one, with the module path at the top
        full_content = module_path_line + "\n" + "\n\n".join(chunks)

        return [full_content], module_path_line

    def _translate_form_path_to_module_path(self, file_path: Path) -> str:
        """
        Translate Form.bin file path to a readable module path comment.

        Example:
            /app/code/Catalogs/Банки/Forms/ФормаЭлемента/Ext/Form.bin
            -> // Модуль формы: Справочники.Банки.Форма.ФормаЭлемента (обычная)
        """
        try:
            rel = file_path.relative_to(self.code_path)
            parts = list(rel.parts)

            # Try to parse the path structure
            # Expected: <Category>/<Object>/Forms/<FormName>/Ext/Form.bin
            # or: CommonForms/<FormName>/Ext/Form.bin

            if "CommonForms" in parts:
                idx = parts.index("CommonForms")
                if idx + 1 < len(parts):
                    form_name = parts[idx + 1]
                    return f"// Модуль формы: ОбщиеФормы.{form_name} (обычная)"

            if "Forms" in parts:
                idx = parts.index("Forms")
                if idx >= 2 and idx + 1 < len(parts):
                    cat_folder = parts[idx - 2]
                    obj_name = parts[idx - 1]
                    form_name = parts[idx + 1]

                    # Map category folder to Russian name
                    from xcf_utils import ru_category_from_folder
                    cat_ru = ru_category_from_folder(cat_folder)

                    return f"// Модуль формы: {cat_ru}.{obj_name}.Форма.{form_name} (обычная)"

            # Fallback: use file path as-is
            return f"// Модуль обычной формы: {file_path.name}"
        except Exception as e:
            logger.debug(f"Failed to translate form path {file_path}: {e}")
            return f"// Модуль обычной формы: {file_path.name}"
