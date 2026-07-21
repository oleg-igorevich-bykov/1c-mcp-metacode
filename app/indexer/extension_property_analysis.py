"""
Extension property analysis orchestrator для incremental loading.

Содержит:
- analyze_xml_files(xml_files) → (classification_results, extraction_results)
  с теми же сигнатурами, что extensions_loader._analyze_xml_files.

NOTE: реальные Neo4j writes выполняются через ExtensionsLoader._save_extension_analysis_results
(тот же путь, что full load), чтобы сохранить byte-by-byte parity без дублирования qn-derivation
логики. Incremental слой создаёт лёгкий ExtensionsLoader instance для вызова.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


def analyze_xml_files(xml_files: List[Path]) -> Tuple[List[Tuple[Path, Any]], List[Tuple[Path, Any]]]:
    """Запускает Classifier + Extractor параллельно на списке XML-файлов.

    Использует те же реальные методы, что и full-load:
    ExtensionPropertiesClassifier.analyze_metadata_xml(path) и
    ExtensionPropertiesExtractor.extract_from_xml(path).
    """
    if not xml_files:
        return [], []

    from extension_properties_classifier import ExtensionPropertiesClassifier
    from extension_properties_extractor import ExtensionPropertiesExtractor

    classifier = ExtensionPropertiesClassifier()
    extractor = ExtensionPropertiesExtractor()

    classification_results: List[Tuple[Path, Any]] = []
    extraction_results: List[Tuple[Path, Any]] = []

    def _one(path: Path):
        cls_res = None
        ext_res = None
        try:
            cls_res = classifier.analyze_metadata_xml(path)
        except Exception as e:  # noqa: BLE001
            logger.warning("Classifier failed for %s: %s", path, e)
        try:
            ext_res = extractor.extract_from_xml(path)
        except Exception as e:  # noqa: BLE001
            logger.warning("Extractor failed for %s: %s", path, e)
        return path, cls_res, ext_res

    max_workers = min(4, len(xml_files))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [pool.submit(_one, p) for p in xml_files]
        for f in as_completed(futs):
            path, cls_res, ext_res = f.result()
            if cls_res is not None:
                classification_results.append((path, cls_res))
            if ext_res is not None:
                extraction_results.append((path, ext_res))

    return classification_results, extraction_results


def derive_element_qn_and_label(
    obj_result: Any, element: Any, ext_config_qn: str, code_root: Optional[Path] = None
) -> Tuple[str, str]:
    """Compute (label, element_qn) для одного analyzer Element.

    Mirror логики из ExtensionsLoader._save_properties_classification (qn-derivation
    branch для разных element_type). Возвращает ("", "") если element_type unknown.

    code_root: корень выгрузки расширения (ext_code_dir), относительно которого строится
        цепочка вложенных подсистем — см. extensions_loader.subsystem_qn_chain(). Вызывающий
        код (main.py, incremental/artifact_sync.py) всегда знает этот путь и обязан его
        передавать — без него вложенные подсистемы откатятся на плоский QN по имени объекта.
    """
    from indexer.extensions_loader import (
        _ELEMENT_LABEL_WHITELIST,
        ExtensionsLoader,
        subsystem_qn_chain,
    )

    label = _ELEMENT_LABEL_WHITELIST.get(element.element_type)
    if not label:
        return "", ""

    category_ru = ExtensionsLoader._get_category_ru(obj_result.object_type)

    # Subsystem path-based base_qn — пути файла
    if obj_result.object_type == "Subsystem" and getattr(obj_result, "xml_path", None):
        chain = subsystem_qn_chain(obj_result.xml_path, code_root)
        if chain is not None:
            base_qn = f"{ext_config_qn}/Подсистемы/" + "/".join(chain)
        else:
            base_qn = f"{ext_config_qn}/{category_ru}/{obj_result.object_name}"
    else:
        base_qn = f"{ext_config_qn}/{category_ru}/{obj_result.object_name}"

    et = element.element_type
    en = element.element_name
    pn = getattr(element, "parent_name", None)

    if et == "MetadataObject":
        element_qn = base_qn
    elif et == "TabularSection":
        element_qn = f"{base_qn}/TabularPart/{en}"
    elif et == "Attribute":
        if pn:
            element_qn = f"{base_qn}/TabularPart/{pn}/Attribute/{en}"
        else:
            element_qn = f"{base_qn}/Attribute/{en}"
    elif et == "Resource":
        element_qn = f"{base_qn}/Resource/{en}"
    elif et == "Dimension":
        element_qn = f"{base_qn}/Dimension/{en}"
    elif et == "EnumValue":
        element_qn = f"{base_qn}/EnumValue/{en}"
    elif et == "Column":
        element_qn = f"{base_qn}/Graph/{en}"
    elif et == "URLTemplate":
        element_qn = f"{base_qn}/UrlTemplate/{en}"
    elif et == "Method":
        element_qn = f"{base_qn}/UrlTemplate/{pn}/Method/{en}"
    elif et == "AddressingAttribute":
        element_qn = f"{base_qn}/Attribute/{en}"
    else:
        return "", ""

    return label, element_qn
