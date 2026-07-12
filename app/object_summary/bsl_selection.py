"""BSL profile builder: routine selection, handler/flow grouping.

Takes raw `bsl_routines`, `bsl_handlers`, `bsl_call_edges` from evidence and
produces a compact `bsl_profile` block:

  {
    "availability": "available" | "empty" | "not_available",
    "stats":   {routines_loaded, handlers_loaded, selected_routines,
                routines_in_profile},
    "modules": [{module, routines, selected}],
    "handlers":[{kind, owner, items: [{event, routine: <id>}]}],
    "routines":[{module, owner, kind, items: [{id, name, code, decorator?,
                                                export?}]}],
    "flows":   [{entry: <id>, calls: "id|id|..."}],
    "uncertain_findings": []
  }

Selection priorities (lower = higher priority, see `_priority`):

  0 : standard object/recordset/value-manager handler, extension_decorator,
      lifecycle
  1 : standard manager/module handler
  2 : called_from_standard, called_from_extension_decorator
  3 : calls_standard, calls_extension_decorator
  4 : command_handler, url_handler
  5 : object_handler
  6 : form_handler
  7 : form_control_handler
  8 : called_from_entry
  9 : export_object_manager
 10 : calls_entry
 20 : other

A routine reaches `routines.items` only if it has a non-empty `code` OR a
non-empty `decorator` — pure handler-routines without behaviour code are not
duplicated in `routines`; their event stays visible only through `handlers`
(and only when the linked routine is in `routines.items`, so empty event
strings don't waste tokens).

`extract_bsl_facts` from `bsl_facts` is reused as-is — it already covers the
full `_BSL_FORMAT_LEGEND` shorthand alphabet.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .bsl_facts import extract_bsl_facts


# ---------------------------------------------------------------------------
# Standard handler tables per category × module type
# ---------------------------------------------------------------------------

_OBJECT_FILL_WRITE_HISTORY = {
    "обработказаполнения",
    "обработкапроверкизаполнения",
    "обработкаформированияповерсииисторииданных",
    "передзаписью",
    "призаписи",
}

_OBJECT_REF = _OBJECT_FILL_WRITE_HISTORY | {
    "передудалением",
    "прикопировании",
}

_MANAGER_CHOICE_PRESENTATION = {
    "обработкаполученияданныхвыбора",
    "обработкаполученияполейпредставления",
    "обработкаполученияпредставления",
    "обработкаполученияформы",
    "обработкапослезаписиверсийисторииданных",
}

_RECORDSET_WRITE = {
    "обработкапроверкизаполнения",
    "передзаписью",
    "призаписи",
}


STANDARD_BSL_HANDLERS_BY_CATEGORY_MODULE: Dict[str, Dict[str, Set[str]]] = {
    "справочники": {
        "ObjectModule": _OBJECT_REF | {"приустановкеновогокода"},
        "ManagerModule": _MANAGER_CHOICE_PRESENTATION,
    },
    "документы": {
        "ObjectModule": _OBJECT_REF | {
            "обработкапроведения",
            "обработкаудаленияпроведения",
            "приустановкеновогономера",
        },
        "ManagerModule": _MANAGER_CHOICE_PRESENTATION,
    },
    "регистрысведений": {
        "RecordSetModule": _OBJECT_FILL_WRITE_HISTORY,
        "ManagerModule": _MANAGER_CHOICE_PRESENTATION,
    },
    "регистрынакопления": {
        "RecordSetModule": _RECORDSET_WRITE,
        "ManagerModule": _MANAGER_CHOICE_PRESENTATION,
    },
    "обработки": {
        "ObjectModule": {"обработкапроверкизаполнения"},
    },
    "httpсервисы": {},
    "бизнеспроцессы": {
        "ObjectModule": _OBJECT_REF | {
            "обработкаинтерактивнойактивации",
            "приустановкеновогономера",
        },
        "ManagerModule": _MANAGER_CHOICE_PRESENTATION,
    },
    "задачи": {
        "ObjectModule": _OBJECT_REF | {
            "обработкаинтерактивнойактивации",
            "обработкапроверкивыполнения",
            "передвыполнением",
            "перединтерактивнымвыполнением",
            "привыполнении",
            "приустановкеновогономера",
        },
        "ManagerModule": _MANAGER_CHOICE_PRESENTATION,
    },
}


def _category_key(category: Any) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", str(category or "").lower())


def _name_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _standard_handlers_for(category: Any, module_type: Any) -> Set[str]:
    table = STANDARD_BSL_HANDLERS_BY_CATEGORY_MODULE.get(_category_key(category), {})
    return set(table.get(str(module_type or "").strip(), set()))


def _standard_reason_for(module_type: Any) -> str:
    mt = str(module_type or "").strip()
    if mt == "ObjectModule":
        return "standard_object_handler"
    if mt == "RecordSetModule":
        return "standard_recordset_handler"
    if mt == "ValueManagerModule":
        return "standard_value_manager_handler"
    if mt == "ManagerModule":
        return "standard_manager_handler"
    return "standard_module_handler"


# ---------------------------------------------------------------------------
# Selection priority
# ---------------------------------------------------------------------------

_PRIORITY: Dict[str, int] = {
    "standard_object_handler": 0,
    "standard_recordset_handler": 0,
    "standard_value_manager_handler": 0,
    "extension_decorator": 0,
    "lifecycle": 0,
    "standard_manager_handler": 1,
    "standard_module_handler": 1,
    "called_from_standard": 2,
    "called_from_extension_decorator": 2,
    "calls_standard": 3,
    "calls_extension_decorator": 3,
    "command_handler": 4,
    "url_handler": 4,
    "object_handler": 5,
    "form_handler": 6,
    "form_control_handler": 7,
    "called_from_entry": 8,
    "export_object_manager": 9,
    "calls_entry": 10,
    "other": 20,
}

_PROTECTED_REASONS = {
    "standard_object_handler",
    "standard_recordset_handler",
    "standard_value_manager_handler",
    "standard_manager_handler",
    "standard_module_handler",
    "extension_decorator",
}

_HANDLER_KIND_TO_REASON = {
    "command": "command_handler",
    "form_event": "form_handler",
    "form_control": "form_control_handler",
    "url": "url_handler",
    "object": "object_handler",
}


def _priority_of(reasons: Sequence[str]) -> int:
    return min((_PRIORITY.get(r, 20) for r in reasons), default=30)


def _decorator(routine: Dict[str, Any]) -> str:
    dt = str(routine.get("decorator_type") or "").strip()
    tg = str(routine.get("decorator_target") or "").strip()
    return f"{dt}:{tg}" if dt and tg else ""


def _routine_kind(routine: Dict[str, Any]) -> str:
    raw = str(routine.get("kind") or "").strip()
    if raw.lower() == "function":
        return "Function"
    return "Procedure"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_bsl_profile(
    *,
    category: Any,
    routines: Sequence[Dict[str, Any]],
    handlers: Sequence[Dict[str, Any]],
    call_edges: Sequence[Dict[str, Any]],
    max_routines: int,
) -> Dict[str, Any]:
    routines = list(routines or [])
    handlers = list(handlers or [])
    call_edges = list(call_edges or [])

    if not routines and not handlers:
        return {
            "availability": "not_available",
            "stats": {
                "routines_loaded": 0,
                "handlers_loaded": 0,
                "selected_routines": 0,
                "routines_in_profile": 0,
            },
            "modules": [],
            "handlers": [],
            "routines": [],
            "flows": [],
            "uncertain_findings": [],
        }

    routines_by_id: Dict[str, Dict[str, Any]] = {}
    for r in routines:
        rid = str(r.get("routine_id") or "").strip()
        if rid:
            routines_by_id[rid] = r

    handler_routine_ids: Set[str] = set()
    for h in handlers:
        rid = str(h.get("routine_id") or "").strip()
        if rid:
            handler_routine_ids.add(rid)

    selected: Dict[str, Dict[str, Any]] = {}

    def add_reason(rid: str, reason: str) -> None:
        bucket = selected.setdefault(rid, {"routine": routines_by_id.get(rid), "reasons": set()})
        bucket["reasons"].add(reason)

    # 1. Standard module handlers + extension decorators + exports.
    for r in routines:
        rid = str(r.get("routine_id") or "").strip()
        if not rid:
            continue
        name_key = _name_key(r.get("name"))
        module_type = r.get("module_type") or ""
        if name_key in _standard_handlers_for(category, module_type):
            add_reason(rid, _standard_reason_for(module_type))
        if _decorator(r):
            add_reason(rid, "extension_decorator")
        if r.get("is_export") and module_type in ("ObjectModule", "ManagerModule"):
            add_reason(rid, "export_object_manager")

    # 2. Entry-point handlers.
    for h in handlers:
        rid = str(h.get("routine_id") or "").strip()
        if not rid or rid not in routines_by_id:
            continue
        reason = _HANDLER_KIND_TO_REASON.get(str(h.get("handler_kind") or "").strip())
        if reason:
            add_reason(rid, reason)

    # 3. Call-edge propagation.
    standard_ids = {rid for rid, info in selected.items()
                    if any(r.startswith("standard_") for r in info["reasons"])}
    decorator_ids = {rid for rid, info in selected.items()
                     if "extension_decorator" in info["reasons"]}
    entry_ids = {rid for rid, info in selected.items()
                 if any(r in {"command_handler", "url_handler", "object_handler",
                              "form_handler", "form_control_handler"}
                        for r in info["reasons"])}

    for edge in call_edges:
        src = str(edge.get("source_id") or "").strip()
        tgt = str(edge.get("target_id") or "").strip()
        if not src or not tgt or src not in routines_by_id or tgt not in routines_by_id:
            continue
        if src in standard_ids:
            add_reason(tgt, "called_from_standard")
        if src in decorator_ids:
            add_reason(tgt, "called_from_extension_decorator")
        if src in entry_ids:
            add_reason(tgt, "called_from_entry")
        if tgt in standard_ids:
            add_reason(src, "calls_standard")
        if tgt in decorator_ids:
            add_reason(src, "calls_extension_decorator")
        if tgt in entry_ids:
            add_reason(src, "calls_entry")

    # 4. Sort by priority and apply max_routines.
    ordered = sorted(
        selected.items(),
        key=lambda kv: (
            _priority_of(kv[1]["reasons"]),
            _name_key((kv[1]["routine"] or {}).get("module_type")),
            _name_key((kv[1]["routine"] or {}).get("name")),
            kv[0],
        ),
    )

    protected: List[Tuple[str, Dict[str, Any]]] = []
    rest: List[Tuple[str, Dict[str, Any]]] = []
    for rid, info in ordered:
        if info["reasons"] & _PROTECTED_REASONS:
            protected.append((rid, info))
        else:
            rest.append((rid, info))
    limit = max(int(max_routines or 0), len(protected))
    final = protected + rest[: max(0, limit - len(protected))]

    # 5. Output filter: include in routines.items only if code or decorator.
    output_routines: List[Tuple[str, Dict[str, Any], str, str]] = []
    for rid, info in final:
        r = info["routine"] or routines_by_id.get(rid) or {}
        decorator = _decorator(r)
        code = extract_bsl_facts(r.get("body_sample") or r.get("body") or "")
        if not code and not decorator:
            continue
        output_routines.append((rid, r, code, decorator))

    id_map: Dict[str, str] = {rid: f"r{i + 1}" for i, (rid, _r, _c, _d) in enumerate(output_routines)}

    # 6. Group routines by (module_type, owner_qn, kind).
    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    group_order: List[Tuple[str, str, str]] = []
    for rid, r, code, decorator in output_routines:
        key = (
            str(r.get("module_type") or "").strip(),
            str(r.get("owner_qn") or "").strip(),
            _routine_kind(r),
        )
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        item: Dict[str, Any] = {
            "id": id_map[rid],
            "name": str(r.get("name") or "").strip(),
        }
        if code:
            item["code"] = code
        if decorator:
            item["decorator"] = decorator
        if r.get("is_export"):
            item["export"] = True
        groups[key].append(item)

    routines_block: List[Dict[str, Any]] = []
    for module_type, owner_qn, kind in group_order:
        entry: Dict[str, Any] = {"module": module_type, "kind": kind, "items": groups[(module_type, owner_qn, kind)]}
        if owner_qn:
            entry["owner"] = owner_qn
        routines_block.append(entry)

    # 7. Handlers — drop those whose routine is not in id_map.
    handler_groups: Dict[Tuple[str, str], List[Dict[str, str]]] = {}
    handler_order: List[Tuple[str, str]] = []
    seen_handler: Set[Tuple[str, str, str, str]] = set()
    for h in handlers:
        rid = str(h.get("routine_id") or "").strip()
        ref = id_map.get(rid)
        if not ref:
            continue
        kind = str(h.get("handler_kind") or "").strip()
        owner = str(h.get("owner") or "").strip()
        event = str(h.get("event") or "").strip()
        key = (kind, owner)
        dedup = (kind, owner, event, ref)
        if dedup in seen_handler:
            continue
        seen_handler.add(dedup)
        if key not in handler_groups:
            handler_groups[key] = []
            handler_order.append(key)
        item: Dict[str, str] = {"routine": ref}
        if event:
            item["event"] = event
        handler_groups[key].append(item)

    handlers_block: List[Dict[str, Any]] = []
    for kind, owner in handler_order:
        entry: Dict[str, Any] = {"kind": kind, "items": handler_groups[(kind, owner)]}
        if owner:
            entry["owner"] = owner
        handlers_block.append(entry)

    # 8. Flows — call edges restricted to routines in id_map.
    flow_buckets: Dict[str, List[str]] = {}
    flow_order: List[str] = []
    for edge in call_edges:
        src = id_map.get(str(edge.get("source_id") or "").strip())
        tgt = id_map.get(str(edge.get("target_id") or "").strip())
        if not src or not tgt or src == tgt:
            continue
        if src not in flow_buckets:
            flow_buckets[src] = []
            flow_order.append(src)
        if tgt not in flow_buckets[src]:
            flow_buckets[src].append(tgt)

    flows_block: List[Dict[str, str]] = []
    for src in flow_order:
        calls = sorted(flow_buckets[src], key=lambda v: int(re.sub(r"\D", "", v) or "0"))
        flows_block.append({"entry": src, "calls": "|".join(calls)})

    # 9. Module stats.
    module_counts: Dict[str, Dict[str, int]] = {}
    module_order: List[str] = []
    for r in routines:
        mt = str(r.get("module_type") or "").strip() or "Unknown"
        bucket = module_counts.setdefault(mt, {"routines": 0, "selected": 0})
        if mt not in module_order:
            module_order.append(mt)
        bucket["routines"] += 1
    for rid, _info in final:
        r = routines_by_id.get(rid) or {}
        mt = str(r.get("module_type") or "").strip() or "Unknown"
        module_counts.setdefault(mt, {"routines": 0, "selected": 0})
        module_counts[mt]["selected"] += 1

    modules_block = [
        {"module": mt, "routines": module_counts[mt]["routines"],
         "selected": module_counts[mt]["selected"]}
        for mt in module_order
    ]

    availability = "available" if output_routines or handlers_block else "empty"

    return {
        "availability": availability,
        "stats": {
            "routines_loaded": len(routines),
            "handlers_loaded": len(handlers),
            "selected_routines": len(final),
            "routines_in_profile": len(output_routines),
        },
        "modules": modules_block,
        "handlers": handlers_block,
        "routines": routines_block,
        "flows": flows_block,
        "uncertain_findings": [],
    }
