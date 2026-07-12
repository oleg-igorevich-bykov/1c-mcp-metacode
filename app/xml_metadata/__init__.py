"""
XML metadata source: parses 1C configuration metadata directly from code/<...>.xml
descriptor files into Configuration / MetadataCategory / MetadataObject objects
compatible with the existing TXT pipeline.

Public API:
    from xml_metadata import XmlMetadataParser
"""

from .parser import XmlMetadataParser, XmlMetadataParseSession, apply_extension_base_overlay

__all__ = ["XmlMetadataParser", "XmlMetadataParseSession", "apply_extension_base_overlay"]
