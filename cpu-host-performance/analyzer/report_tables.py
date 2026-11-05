"""Native offline disclosure for complete evidence tables."""
import re
from html import escape
from pathlib import Path


def disclose_table(markup, title='完整明细', count=None, force=False):
    if count is None:
        count = max(0, len(re.findall(r'<tr\b', markup)) - 1)
    if not force and count <= 12:
        return markup
    return ('<details class="data-details"><summary>%s（共 %d 条记录，展开完整明细）</summary>'
            '<div class="table-tools"><label>搜索 <input data-table-search type="search" placeholder="关键词 / CPU / TID"></label>'
            '<label>排序列 <select data-table-sort></select></label>'
            '<button type="button" data-table-direction>升序</button>'
            '<span data-table-count>显示 %d / %d 条</span></div>%s</details>') % (escape(title), count, count, count, markup)


def enhance_long_tables(html):
    """Cover legacy hand-written tables too; never duplicate an existing wrapper."""
    pattern = re.compile(r'<details\b[^>]*class="data-details"[^>]*>.*?</details>|<table\b[^>]*>.*?</table>', re.S)
    return pattern.sub(lambda match: match.group(0) if match.group(0).startswith('<details') else disclose_table(match.group(0)), html)


def table_script():
    return '<script>' + (Path(__file__).parent / 'assets/report_tables.js').read_text(encoding='utf-8-sig') + '</script>'
