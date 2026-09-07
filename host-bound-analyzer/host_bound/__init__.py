"""host-bound-analyzer: CPU / Host Bound performance diagnosis skill.

Zero-dependency (Python standard library only). See DESIGN.md for architecture.
"""

VERSION = "0.2.0"
SCHEMA_VERSION = "1.0"
REPORT_VERSION = "1.0"
RULE_VERSION = "1.0"

VERSION_BLOCK = {
    "tool_version": VERSION,
    "schema_version": SCHEMA_VERSION,
    "report_version": REPORT_VERSION,
    "rule_version": RULE_VERSION,
}
