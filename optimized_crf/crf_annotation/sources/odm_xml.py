"""CDISC ODM-XML — the interchange format almost every EDC can export.

Veeva Vault CDMS, Medidata Rave, Medrio, OpenClinica, Castor, Viedoc and
others all emit ODM 1.3.x or 2.x. Supporting this one format is what makes the
pipeline EDC-agnostic in practice; a vendor-native adapter is only worth
writing when the native export carries something ODM drops.

Structure walked here:

    Study/MetaDataVersion
      StudyEventDef  --FormRef-->  FormDef
                                     --ItemGroupRef--> ItemGroupDef
                                                         --ItemRef--> ItemDef

Namespaces are stripped rather than matched, so 1.3 and 2.0 documents and
vendor extension namespaces all parse the same way.
"""

from __future__ import annotations

from collections import Counter

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator, Optional

from ..design import DesignError, Field, Form, StudyDesign, Visit, VisitForm, build
from ..logging_utils import get_logger
from .base import peek, register, suffix_score

logger = get_logger()


def _tag(element: ET.Element) -> str:
    """Local tag name with any namespace stripped."""
    return element.tag.rsplit("}", 1)[-1]


def _find_all(root: ET.Element, name: str) -> Iterator[ET.Element]:
    for element in root.iter():
        if _tag(element) == name:
            yield element


def _child(element: ET.Element, name: str) -> Optional[ET.Element]:
    for candidate in element:
        if _tag(candidate) == name:
            return candidate
    return None


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in element if _tag(c) == name]


def _translated_text(element: Optional[ET.Element], lang: str = "en") -> str:
    """Text of a Question/Description block, preferring `lang`."""
    if element is None:
        return ""
    options = _children(element, "TranslatedText")
    if not options:
        return (element.text or "").strip()
    for option in options:
        attr_lang = next(
            (v for k, v in option.attrib.items() if k.rsplit("}", 1)[-1] == "lang"), ""
        )
        if attr_lang.lower().startswith(lang):
            return (option.text or "").strip()
    return (options[0].text or "").strip()


def _order(element: ET.Element, fallback: int) -> int:
    raw = element.get("OrderNumber") or element.get("Ordinal") or ""
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return fallback


class OdmXmlSource:
    name = "CDISC ODM-XML"
    extensions = (".xml", ".odm")

    def sniff(self, path: Path) -> float:
        score = suffix_score(path, self.extensions)
        if not score:
            return 0.0
        head = peek(path)
        if "cdisc.org/ns/odm" in head:
            score += 0.6
        elif "<ODM" in head:
            score += 0.4
        if "MetaDataVersion" in head or "FormDef" in head:
            score += 0.1
        return min(score, 1.0)

    def load(self, path: Path) -> StudyDesign:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            raise DesignError(f"Malformed XML: {exc}") from exc
        except OSError as exc:
            raise DesignError(f"Cannot read file: {exc}") from exc

        mdv = next(_find_all(root, "MetaDataVersion"), None)
        if mdv is None:
            raise DesignError("No MetaDataVersion element — not an ODM metadata export")

        study_name = ""
        globals_ = next(_find_all(root, "GlobalVariables"), None)
        if globals_ is not None:
            name_element = _child(globals_, "StudyName")
            study_name = (name_element.text or "").strip() if name_element is not None else ""

        item_defs = {e.get("OID", ""): e for e in _find_all(mdv, "ItemDef")}
        group_defs = {e.get("OID", ""): e for e in _find_all(mdv, "ItemGroupDef")}
        form_defs = {e.get("OID", ""): e for e in _find_all(mdv, "FormDef")}
        event_defs = {e.get("OID", ""): e for e in _find_all(mdv, "StudyEventDef")}

        if not form_defs:
            raise DesignError("No FormDef elements found")

        forms = self._forms(form_defs)
        fields, warnings = self._fields(form_defs, group_defs, item_defs)
        visits, visit_forms = self._visits(mdv, event_defs)

        return build(
            self.name, forms, fields, visits, visit_forms,
            study_name=study_name, warnings=warnings,
        )

    # -- sections -----------------------------------------------------------
    @staticmethod
    def _forms(form_defs: dict[str, ET.Element]) -> list[Form]:
        forms = []
        for index, (oid, element) in enumerate(form_defs.items()):
            if not oid:
                continue
            # The Name is the form's title ("Adverse Events"); a Description is
            # often its instructions ("Indicate whether ..."), which no page
            # header prints. Only a technical Name (DM_FORM) gives way to a
            # short Description.
            name = (element.get("Name") or "").strip()
            description = _translated_text(_child(element, "Description")).strip()
            technical = not name or (" " not in name and ("_" in name or name.isupper()))
            label = description if technical and description and len(description) <= 60 else (name or description or oid)
            forms.append(
                Form(
                    oid=oid,
                    label=label.strip(),
                    order=_order(element, index),
                    repeating=element.get("Repeating", "No").lower() == "yes",
                    source_oid=oid,
                )
            )
        return forms

    @staticmethod
    def _fields(
        form_defs: dict[str, ET.Element],
        group_defs: dict[str, ET.Element],
        item_defs: dict[str, ET.Element],
    ) -> tuple[list[Field], list[str]]:
        """Walk FormDef -> ItemGroupRef -> ItemGroupDef -> ItemRef -> ItemDef."""
        fields: list[Field] = []
        warnings: list[str] = []
        missing_groups: set[str] = set()
        missing_items: set[str] = set()
        unlabelled = 0

        for form_oid, form in form_defs.items():
            position = 0
            # Specs usually reference a field by its short Name, but some
            # exports give several items one Name ("Serologic tests
            # Creatinine" for the value, its normal-range flag and its
            # clinical significance). A shared Name would merge them into
            # one field, so those items keep their unique OID instead.
            names = Counter(
                (item_defs[ref.get("ItemOID", "")].get("Name") or ref.get("ItemOID", "")).strip()
                for group_ref in _children(form, "ItemGroupRef")
                if (group := group_defs.get(group_ref.get("ItemGroupOID", ""))) is not None
                for ref in _children(group, "ItemRef")
                if ref.get("ItemOID", "") in item_defs
            )
            for group_ref in _children(form, "ItemGroupRef"):
                group_oid = group_ref.get("ItemGroupOID", "")
                group = group_defs.get(group_oid)
                if group is None:
                    missing_groups.add(group_oid)
                    continue
                for item_ref in _children(group, "ItemRef"):
                    item_oid = item_ref.get("ItemOID", "")
                    item = item_defs.get(item_oid)
                    if item is None:
                        missing_items.add(item_oid)
                        continue

                    label = _translated_text(_child(item, "Question"))
                    if not label:
                        label = _translated_text(_child(item, "Description"))
                    if not label:
                        label = item.get("Name", "")
                        unlabelled += 1

                    codelist = _child(item, "CodeListRef")
                    # Specs reference the field by its short name, not its OID,
                    # unless that name is shared with another item on the form.
                    name = (item.get("Name") or item_oid).strip()
                    if names[name] > 1:
                        name = item_oid
                    fields.append(
                        Field(
                            oid=name,
                            form_oid=form_oid,
                            label=label.strip(),
                            order=position,
                            datatype=item.get("DataType", ""),
                            codelist_oid=codelist.get("CodeListOID", "") if codelist is not None else "",
                            source_oid=item_oid,
                            section=(group.get("Name") or "").strip(),
                        )
                    )
                    position += 1

        if missing_groups:
            warnings.append(f"{len(missing_groups)} ItemGroupRef(s) point at undefined groups")
        if missing_items:
            warnings.append(f"{len(missing_items)} ItemRef(s) point at undefined items")
        if unlabelled:
            warnings.append(
                f"{unlabelled} item(s) have no Question text; their Name was used as the "
                "CRF label and may not match the printed form"
            )
        return fields, warnings

    @staticmethod
    def _visits(
        mdv: ET.Element,
        event_defs: dict[str, ET.Element],
    ) -> tuple[list[Visit], list[VisitForm]]:
        # Protocol/StudyEventRef carries the canonical visit ordering.
        order_by_oid: dict[str, int] = {}
        protocol = _child(mdv, "Protocol")
        if protocol is not None:
            for index, ref in enumerate(_children(protocol, "StudyEventRef")):
                oid = ref.get("StudyEventOID", "")
                if oid:
                    order_by_oid[oid] = _order(ref, index)

        visits: list[Visit] = []
        pairs: list[VisitForm] = []
        for index, (oid, element) in enumerate(event_defs.items()):
            if not oid:
                continue
            # The Name is the form's title ("Adverse Events"); a Description is
            # often its instructions ("Indicate whether ..."), which no page
            # header prints. Only a technical Name (DM_FORM) gives way to a
            # short Description.
            name = (element.get("Name") or "").strip()
            description = _translated_text(_child(element, "Description")).strip()
            technical = not name or (" " not in name and ("_" in name or name.isupper()))
            label = description if technical and description and len(description) <= 60 else (name or description or oid)
            visits.append(
                Visit(
                    oid=oid,
                    label=label.strip(),
                    order=order_by_oid.get(oid, index),
                )
            )
            pairs.extend(
                VisitForm(visit_oid=oid, form_oid=ref.get("FormOID", ""))
                for ref in _children(element, "FormRef")
                if ref.get("FormOID")
            )
        return visits, pairs


register(OdmXmlSource())
