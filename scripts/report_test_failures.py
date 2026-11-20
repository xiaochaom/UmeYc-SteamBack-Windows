"""Expose failed isolated test details as GitHub check annotations."""
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


sys.stdout.reconfigure(encoding="utf-8")
report = Path("test-results.xml")
if report.exists():
    for case in ET.parse(report).iter("testcase"):
        for failure in (*case.findall("failure"), *case.findall("error")):
            detail = f"{case.get('classname')}.{case.get('name')}\n{failure.text or failure.get('message', '')}"
            escaped = detail[:12000].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::error::{escaped}")
