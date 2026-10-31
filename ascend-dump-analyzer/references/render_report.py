#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export a reviewed Markdown report to offline HTML; verify the pair with --check.

Uses only the standard library. Supports headings, paragraphs, pipe tables,
flat lists, blockquotes, fenced code, inline code/emphasis and safe links.
Raw HTML is displayed as text. Analysis and secret redaction happen upstream.
"""
import argparse
from html import escape
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


CSS = """
:root{color-scheme:dark;--bg:#101722;--card:#172231;--line:#33435a;--ink:#e7edf6;--muted:#a9b8cc;--accent:#70d4c0}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.75 system-ui,'Microsoft YaHei',sans-serif}
main{max-width:1180px;margin:auto;padding:42px 28px 80px}h1{font-size:32px;line-height:1.4;border-bottom:2px solid var(--accent);padding-bottom:20px}
h2{margin-top:42px;color:var(--accent)}h3{margin-top:28px}p,li{overflow-wrap:anywhere}a{color:#8cbdff}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px;margin:20px 0}table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:12px 16px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line);overflow-wrap:anywhere}
th{background:#223148;white-space:nowrap}tr:last-child td{border-bottom:0}tr:nth-child(even){background:var(--card)}
code{font:13px/1.7 Consolas,monospace;background:#243247;border-radius:4px;padding:2px 5px}
pre{overflow:auto;background:#0b111b;border:1px solid var(--line);padding:18px;border-radius:8px}pre code{background:none;padding:0}
blockquote{border-left:3px solid var(--accent);padding:8px 18px;margin:20px 0;color:var(--muted);background:var(--card)}
.source{color:var(--muted);font-size:13px}hr{border:0;border-top:1px solid var(--line);margin:30px 0}
@media(max-width:640px){main{padding:20px 14px}h1{font-size:25px}th,td{padding:9px}}
@media print{:root{color-scheme:light}body{background:white;color:#111}h2,a{color:#164e63}th,td{border-color:#aaa}th,tr:nth-child(even),code,pre,blockquote{background:#f2f4f6;color:#111}main{max-width:none;padding:0}pre{white-space:pre-wrap}}
"""
INLINE = re.compile(r'(`+)(.+?)\1|\[([^\]\n]+)\]\(([^\s)]+)\)|\*\*([^*\n]+)\*\*|(?<!\*)\*([^*\n]+)\*(?!\*)')
HEADING = re.compile(r'^(#{1,6})\s+(.+)$')
LIST = re.compile(r'^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$')
FENCE = re.compile(r'^\s*(`{3,}|~{3,})(.*)$')


def inline(text):
    """Interpret a small inline subset, escaping every literal data fragment."""
    result, pos = [], 0
    for match in INLINE.finditer(text):
        result.append(escape(text[pos:match.start()], quote=True))
        ticks, code, label, url, strong, emphasis = match.groups()
        if ticks:
            result.append('<code>' + escape(code, quote=True) + '</code>')
        elif label is not None:
            # Local paths and http(s) links are inert until clicked; no data/JS URLs.
            scheme = urlsplit(url).scheme.lower()
            if scheme in ('http', 'https', '') and not url.startswith(('//', '\\')):
                result.append('<a href="' + escape(url, quote=True) + '">' + escape(label, quote=True) + '</a>')
            else:
                result.append(escape(match.group(), quote=True))
        elif strong is not None:
            result.append('<strong>' + escape(strong, quote=True) + '</strong>')
        else:
            result.append('<em>' + escape(emphasis, quote=True) + '</em>')
        pos = match.end()
    result.append(escape(text[pos:], quote=True))
    return ''.join(result)


def cells(line):
    """Keep escaped pipes and pipes inside code spans in the same cell."""
    parts, current, tick_run = [], [], ''
    i = 0
    while i < len(line):
        char = line[i]
        if char == '\\' and i + 1 < len(line) and line[i + 1] == '|':
            current.append('|')
            i += 2
            continue
        if char == '`':
            end = i + 1
            while end < len(line) and line[end] == '`':
                end += 1
            run = line[i:end]
            tick_run = '' if tick_run == run else (run if not tick_run else tick_run)
            current.append(run)
            i = end
            continue
        if char == '|' and not tick_run:
            parts.append(''.join(current).strip())
            current = []
        else:
            current.append(char)
        i += 1
    parts.append(''.join(current).strip())
    if line.lstrip().startswith('|'):
        parts = parts[1:]
    if line.rstrip().endswith('|') and parts[-1] == '':
        parts = parts[:-1]
    return parts


def separator(line):
    values = cells(line)
    return bool(values) and all(re.fullmatch(r':?-{3,}:?', value) for value in values)


def render_markdown(source):
    lines, output, index = source.splitlines(), [], 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        fence = FENCE.match(line)
        heading = HEADING.match(line)
        item = LIST.match(line)
        if fence:
            marker, language = fence.groups()
            code = []
            index += 1
            closing = re.compile(r'^\s*' + re.escape(marker[0]) + '{' + str(len(marker)) + r',}\s*$')
            while index < len(lines) and not closing.match(lines[index]):
                code.append(lines[index])
                index += 1
            if index == len(lines):
                raise ValueError('unclosed fenced code block in Markdown')
            output.append('<pre><code>' + escape('\n'.join(code), quote=True) + '</code></pre>')
        elif heading:
            level = len(heading[1])
            output.append(f'<h{level}>' + inline(heading[2]) + f'</h{level}>')
        elif index + 1 < len(lines) and '|' in line and separator(lines[index + 1]):
            header = cells(line)
            rows = ['<tr>' + ''.join('<th>' + inline(v) + '</th>' for v in header) + '</tr>']
            index += 2
            while index < len(lines) and lines[index].strip() and '|' in lines[index]:
                row = cells(lines[index])
                if len(row) != len(header):
                    raise ValueError(f'table column count mismatch at line {index + 1}; escape literal pipes with \\|')
                rows.append('<tr>' + ''.join('<td>' + inline(v) + '</td>' for v in row) + '</tr>')
                index += 1
            output.append('<div class="table-wrap"><table><thead>' + rows[0] + '</thead><tbody>' + ''.join(rows[1:]) + '</tbody></table></div>')
            continue
        elif item:
            ordered = item[2] is not None
            tag, items = ('ol' if ordered else 'ul'), []
            while index < len(lines):
                match = LIST.match(lines[index])
                if not match or (match[2] is not None) != ordered:
                    break
                items.append('<li>' + inline(match[3]) + '</li>')
                index += 1
            output.append(f'<{tag}>' + ''.join(items) + f'</{tag}>')
            continue
        elif line.startswith('> '):
            output.append('<blockquote>' + inline(line[2:]) + '</blockquote>')
        elif re.fullmatch(r'\s*(?:-{3,}|\*{3,}|_{3,})\s*', line):
            output.append('<hr>')
        else:
            paragraph = [line]
            index += 1
            while index < len(lines) and lines[index].strip():
                following = lines[index]
                if HEADING.match(following) or FENCE.match(following) or LIST.match(following) or following.startswith('> '):
                    break
                if index + 1 < len(lines) and '|' in following and separator(lines[index + 1]):
                    break
                paragraph.append(following)
                index += 1
            output.append('<p>' + inline('\n'.join(paragraph)) + '</p>')
            continue
        index += 1
    return '\n'.join(output)


def document(source, filename):
    title = next((m[2] for line in source.splitlines() if (m := HEADING.match(line)) and len(m[1]) == 1), Path(filename).stem)
    return ('<!DOCTYPE html>\n<html lang="zh-CN">\n<head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>' + escape(title, quote=True) + '</title><style>' + CSS + '</style></head>\n'
            '<body><main><p class="source">Markdown 源文件：' + escape(filename, quote=True) + '</p>\n'
            + render_markdown(source) + '\n</main></body></html>\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description='从已完成的 Markdown 报告生成同名离线 HTML；不执行诊断或脱敏。')
    parser.add_argument('markdown', type=Path, help='已写入的 UTF-8 .md / .markdown 报告')
    parser.add_argument('--check', action='store_true', help='只读检查两个文件存在、非空且内容同步；失败返回非零')
    args = parser.parse_args(argv)
    md = args.markdown.resolve()
    html = md.with_suffix('.html')
    try:
        if md.suffix.lower() not in ('.md', '.markdown'):
            raise ValueError('source report must have .md or .markdown extension')
        source = md.read_text(encoding='utf-8-sig')
        if not source.strip():
            raise ValueError('Markdown report is empty')
        expected = document(source, md.name)
        if args.check:
            if html.read_text(encoding='utf-8') != expected:
                raise ValueError('HTML is stale or modified; regenerate it from the current Markdown report')
        else:
            html.write_text(expected, encoding='utf-8')
        # Verify the actual files before claiming successful delivery.
        if not md.stat().st_size or html.read_text(encoding='utf-8') != expected:
            raise ValueError('report pair verification failed')
    except (OSError, UnicodeError, ValueError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    print(f'{"VERIFIED" if args.check else "GENERATED"}\nMD: {md}\nHTML: {html}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
