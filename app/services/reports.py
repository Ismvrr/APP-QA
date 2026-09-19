"""Monthly PDF rendering from stored analysis results."""

import json
import html
import re
from io import BytesIO

from xhtml2pdf import pisa
from jinja2 import Environment, FileSystemLoader

templates = Environment(loader=FileSystemLoader("app/templates"))


def _result_text(value: str | None) -> str:
    if not value:
        return ""
    decoded = json.loads(value)
    return decoded.get("text", "") if isinstance(decoded, dict) else str(decoded)


def markdown_to_html(value: str) -> str:
    """Convert the limited Markdown commonly returned by Gemini into safe HTML."""
    lines = value.splitlines()
    output: list[str] = []
    in_list = False

    def inline(text: str) -> str:
        text = html.escape(text, quote=False)
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", text)
        return text

    for raw_line in lines:
        line = raw_line.strip()
        if re.fullmatch(r"(?:---+|\*\*\*+|___+)", line):
            continue
        if not line:
            if in_list:
                output.append("</ul>")
                in_list = False
            continue
        heading = re.match(r"^#{1,3}\s+(.+)$", line)
        bullet = re.match(r"^[-*]\s+(.+)$", line)
        if heading:
            if in_list:
                output.append("</ul>")
                in_list = False
            output.append(f"<h3>{inline(heading.group(1))}</h3>")
        elif bullet:
            if not in_list:
                output.append("<ul>")
                in_list = True
            output.append(f"<li>{inline(bullet.group(1))}</li>")
        else:
            if in_list:
                output.append("</ul>")
                in_list = False
            output.append(f"<p>{inline(line)}</p>")
    if in_list:
        output.append("</ul>")
    return "".join(output)


def render_monthly_pdf(report: dict) -> bytes:
    html = templates.get_template("monthly_report.html").render(report=report)
    output = BytesIO()
    result = pisa.CreatePDF(src=html, dest=output, encoding="utf-8")
    if result.err:
        raise RuntimeError("No se pudo generar el PDF mensual")
    return output.getvalue()
