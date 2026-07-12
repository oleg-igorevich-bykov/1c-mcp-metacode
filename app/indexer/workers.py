"""
Worker functions for multi-threaded file processing.

These functions are designed to be executed in ThreadPoolExecutor.
Each worker processes a single file and returns structured data.
"""

from pathlib import Path
from typing import Optional, Dict, Any, List
import logging
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


def worker_form_xml(
    xml_path: Path,
    cfg_name: str,
    code_root: Path,
    project_name: str,
    fparser,  # FormXmlParser instance
    cfg_by_name: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """
    Process a single Form.xml file.

    Args:
        xml_path: Path to Form.xml file
        cfg_name: Configuration name
        code_root: Root directory of code
        project_name: Project name
        fparser: FormXmlParser instance
        cfg_by_name: Configuration lookup by name

    Returns:
        Dictionary with parsed form data or None on error
    """
    try:
        from xcf_utils import parse_path_triplet, ru_category_from_folder, compute_form_qn
        from datapath_resolver import resolve_datapath_bindings

        triplet = parse_path_triplet(code_root, xml_path)
        if not triplet:
            return None

        category_folder, object_name, form_name = triplet
        category_ru = ru_category_from_folder(category_folder)
        form_qn = compute_form_qn(project_name, cfg_name, category_ru, object_name, form_name)

        parsed = fparser._parse_form_file(xml_path, form_qn)
        if not parsed:
            return None

        partial: Dict[str, List[Dict[str, Any]]] = {
            "form_updates": [],
            "controls": [],
            "root_rel": [],
            "child_rel": [],
            "events": [],
            "event_rel": [],
            "event_actions": [],
            "form_attributes": [],
            "form_commands": [],
            "form_command_usages": [],
            "data_bindings": [],
        }

        if parsed.get("form_properties"):
            partial["form_updates"].append({
                "form_qn": form_qn,
                "properties": parsed["form_properties"],
            })

        for c in parsed.get("controls", []) or []:
            partial["controls"].append({
                "qn": c["qn"],
                "name": c.get("name") or "",
                "type": None,
                "properties": c.get("properties") or {},
            })

        for rel in parsed.get("root_rel", []) or []:
            partial["root_rel"].append(rel)

        for rel in parsed.get("child_rel", []) or []:
            partial["child_rel"].append(rel)

        for e in parsed.get("events", []) or []:
            partial["events"].append({
                "qn": e["qn"],
                "properties": e.get("properties") or {},
            })

        for er in parsed.get("event_rel", []) or []:
            partial["event_rel"].append(er)

        for ea in parsed.get("event_actions", []) or []:
            partial["event_actions"].append(ea)

        for a in parsed.get("form_attributes", []) or []:
            partial["form_attributes"].append({
                "qn": a["qn"],
                "form_qn": form_qn,
                "name": a.get("name") or "",
                "properties": a.get("properties") or {},
            })

        for fc in parsed.get("form_commands", []) or []:
            partial["form_commands"].append(fc)

        for fu in parsed.get("form_command_usages", []) or []:
            partial["form_command_usages"].append(fu)

        # Resolve DataPath bindings immediately
        try:
            cfg_obj = cfg_by_name.get(cfg_name)
            if cfg_obj:
                resolve_datapath_bindings(cfg_obj, partial, project_name)
        except Exception as re:
            logger.error(
                "DataPath resolution failed for configuration %s (file=%s): %s",
                cfg_name, str(xml_path), str(re)
            )

        return {
            "kind": "form",
            "cfg_name": cfg_name,
            "form_qn": form_qn,
            "rows": partial,
            "form_content_hash": parsed.get("form_content_hash"),
        }

    except Exception as e:
        logger.error("Failed to parse Form.xml %s: %s", str(xml_path), str(e))
        return None


def worker_extension_form(
    form_xml_path: Path,
    is_adopted: bool,
    cfg_name: str,
    code_root: Path,
    project_name: str,
    fparser,
    cfg_obj=None,
) -> Optional[Dict[str, Any]]:
    """
    Process a single Form.xml from an extension directory.
    Returns kind='form' so FormsProcessor.merge_form_result() handles it unchanged.
    """
    try:
        from xcf_utils import parse_path_triplet, ru_category_from_folder, compute_form_qn

        triplet = parse_path_triplet(code_root, form_xml_path)
        if not triplet:
            return None

        category_folder, object_name, form_name = triplet
        category_ru = ru_category_from_folder(category_folder)
        form_qn = compute_form_qn(project_name, cfg_name, category_ru, object_name, form_name)

        parsed = fparser.parse_extension_form_file(form_xml_path, form_qn, is_adopted)
        if not parsed:
            return None

        base_form_hash = parsed.pop("base_form_hash", None)

        partial: Dict[str, List[Dict[str, Any]]] = {
            "form_updates": [],
            "controls": [],
            "root_rel": [],
            "child_rel": [],
            "events": [],
            "event_rel": [],
            "event_actions": [],
            "form_attributes": [],
            "form_commands": [],
            "form_command_usages": [],
            "data_bindings": [],
        }

        if parsed.get("form_properties"):
            partial["form_updates"].append({
                "form_qn": form_qn,
                "properties": parsed["form_properties"],
            })

        for c in parsed.get("controls", []) or []:
            partial["controls"].append({
                "qn": c["qn"],
                "name": c.get("name") or "",
                "type": None,
                "properties": c.get("properties") or {},
            })

        for rel in parsed.get("root_rel", []) or []:
            partial["root_rel"].append(rel)
        for rel in parsed.get("child_rel", []) or []:
            partial["child_rel"].append(rel)

        for e in parsed.get("events", []) or []:
            partial["events"].append({
                "qn": e["qn"],
                "properties": e.get("properties") or {},
            })
        for er in parsed.get("event_rel", []) or []:
            partial["event_rel"].append(er)

        for ea in parsed.get("event_actions", []) or []:
            partial["event_actions"].append(ea)

        for a in parsed.get("form_attributes", []) or []:
            partial["form_attributes"].append({
                "qn": a["qn"],
                "form_qn": form_qn,
                "name": a.get("name") or "",
                "properties": a.get("properties") or {},
            })

        for fc in parsed.get("form_commands", []) or []:
            partial["form_commands"].append(fc)
        for fu in parsed.get("form_command_usages", []) or []:
            partial["form_command_usages"].append(fu)

        if cfg_obj is not None:
            try:
                from datapath_resolver import resolve_datapath_bindings
                resolve_datapath_bindings(cfg_obj, partial, project_name)
            except Exception as re:
                logger.error(
                    "DataPath resolution failed for extension %s (file=%s): %s",
                    cfg_name, str(form_xml_path), str(re)
                )

        return {
            "kind": "form",
            "cfg_name": cfg_name,
            "form_qn": form_qn,
            "rows": partial,
            "base_form_hash": base_form_hash,
        }

    except Exception as e:
        logger.error("worker_extension_form failed for %s: %s", form_xml_path, e)
        return None


def worker_predefined(xml_path: Path, pre_parser) -> Optional[Dict[str, Any]]:
    """
    Process a single Predefined.xml file.

    Args:
        xml_path: Path to Predefined.xml file
        pre_parser: PredefinedParser instance

    Returns:
        Dictionary with parsed predefined data or None on error
    """
    try:
        # Expect path .../<Dir>/<ObjectName>/Ext/Predefined.xml
        obj_dir = xml_path.parent.parent  # .../<ObjectName>
        dir_dir = obj_dir.parent          # .../<Dir>
        object_name = obj_dir.name
        dir_name = dir_dir.name

        # Try dir-based category first (fast)
        from parsers.predefined_parser import DIR_TO_CATEGORY as PRE_DIR_TO_CATEGORY
        category_name = PRE_DIR_TO_CATEGORY.get(dir_name)

        # Fallback: sniff from XML xsi:type when directory mapping is unknown
        if not category_name:
            try:
                tree = ET.parse(xml_path)
                root = tree.getroot()
                xsi_type = root.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}type", "")
                if xsi_type.endswith("CatalogPredefinedItems"):
                    category_name = "Справочники"
                elif xsi_type.endswith("PlanOfCharacteristicKindPredefinedItems"):
                    category_name = "ПланыВидовХарактеристик"
                elif xsi_type.endswith("ChartOfAccountsPredefinedItems"):
                    category_name = "ПланыСчетов"
            except Exception:
                pass

        if not category_name:
            logger.debug("Skipping Predefined.xml under unsupported dir: %s", xml_path)
            return None

        # Use existing internal routine; build local accumulators
        loc_items: List[Dict[str, Any]] = []
        loc_rel: List[Dict[str, Any]] = []
        pre_parser._parse_predefined_file(xml_path, category_name, object_name, loc_items, loc_rel)

        return {
            "kind": "predef",
            "items": loc_items,
            "relations": loc_rel,
        }

    except Exception as e:
        logger.error("Failed to parse Predefined.xml %s: %s", str(xml_path), str(e))
        return None


def worker_help(html_path: Path) -> Optional[Dict[str, Any]]:
    """
    Parse Help/ru.html and extract text content for metadata objects.

    Args:
        html_path: Path to ru.html file

    Returns:
        Dictionary with extracted help content or None on error
    """
    try:
        from bs4 import BeautifulSoup

        # Expected path: .../<Category>/<ObjectName>/Ext/Help/ru.html
        obj_dir = html_path.parent.parent.parent  # ObjectName folder
        cat_dir = obj_dir.parent                  # Category folder
        object_name = obj_dir.name
        category_folder = cat_dir.name

        # Read HTML content
        with open(html_path, 'r', encoding='utf-8') as f:
            html_content = f.read()

        # Parse and extract text using BeautifulSoup
        soup = BeautifulSoup(html_content, 'html.parser')

        # Remove script and style elements
        for script in soup(["script", "style"]):
            script.decompose()

        # Get text and clean up whitespace
        help_text = soup.get_text(separator=' ', strip=True)

        if not help_text:
            return None

        return {
            "kind": "help",
            "category_folder": category_folder,
            "object_name": object_name,
            "help_content": help_text,
        }

    except Exception as e:
        logger.error("Failed to parse Help/ru.html %s: %s", str(html_path), str(e))
        return None


def worker_form_bin(
    bin_path: Path,
    cfg_name: str,
    code_root: Path,
    project_name: str
) -> Optional[Dict[str, Any]]:
    """
    Process a single Form.bin file (extract BSL code and parse).

    Args:
        bin_path: Path to Form.bin file
        cfg_name: Configuration name
        code_root: Root directory of code
        project_name: Project name

    Returns:
        Dictionary with parsed BSL data or None on error
    """
    try:
        from parsers.form_bin_parser import FormBinParser
        from bsl_signature_scanner import scan_bsl_from_form_bin

        # Create parser and extract code from Form.bin
        parser = FormBinParser(code_root)
        code_chunks, module_path_line = parser.parse(bin_path)

        if not code_chunks or not code_chunks[0]:
            return None

        # Parse extracted code using BSL scanner
        return scan_bsl_from_form_bin(
            code_chunks[0],
            bin_path,
            code_root,
            project_name,
            cfg_name
        )

    except Exception as e:
        logger.error("Failed to parse Form.bin %s: %s", str(bin_path), str(e))
        return None


def worker_event_subscription(file_path: Path) -> dict:
    try:
        from parsers.event_subscription_parser import EventSubscriptionParser
        subscription = EventSubscriptionParser().parse_file(file_path)
        return {"kind": "event_sub", "data": subscription}
    except Exception as e:
        logger.error("Failed to parse EventSubscription %s: %s", str(file_path), str(e))
        return {"kind": "event_sub", "data": None}
