#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render the shipped Ascend HTML dashboards from reviewed JSON slot values."""
import argparse
from html import escape
from html.parser import HTMLParser
import json
from math import pi
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


TEMPLATES = {
    'single': 'template_single_dump.html',
    'comparison': 'template_comparison.html',
}
SLOT = re.compile(r'[{][{][{]([A-Z][A-Z0-9_]*)[}][}][}]|[{][{]([A-Z][A-Z0-9_]*)[}][}]')
DERIVED = {'SCORE_OFFSET', 'SIMILARITY_WIDTH'}
COLORS = {'var(--' + name + ')' for name in ('ok', 'warn', 'fail', 'info', 'accent', 'ink-muted')}
REQUIRED_BLOCKS = {
    'single': ('NAV_ITEMS', 'ASSESSMENT_BANNER', 'META_DATA_ROWS', 'ISSUE_ITEMS', 'MISSING_ITEMS'),
    'comparison': ('NAV_ITEMS', 'ASSESSMENT_BANNER', 'SCOPE_COMPARISON_ROWS', 'FINDING_BANNERS'),
}
FRAGMENT_TAGS = set('a b blockquote br code dd details div dl dt em h2 h3 h4 hr li ol p pre section small span strong summary table tbody td th thead tr ul'.split())
VOID_TAGS = {'br', 'hr'}


def safe_link(value):
    if not value or any(char.isspace() or ord(char) < 32 for char in value):
        return False
    if re.fullmatch(r'#[A-Za-z][A-Za-z0-9_-]*', value):
        return True
    url = urlsplit(value)
    return url.scheme in ('http', 'https') and bool(url.hostname) and url.username is None


class Fragment(HTMLParser):
    """Rebuild only the passive markup used in dashboard slots."""

    def __init__(self, name):
        super().__init__()
        self.name = name
        self.parts = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag not in FRAGMENT_TAGS:
            raise ValueError(f'{self.name}: unsupported HTML tag <{tag}>')
        rendered = []
        for key, value in attrs:
            if key == 'open' and tag == 'details':
                rendered.append(' open')
                continue
            if not (key in ('class', 'id', 'title', 'aria-label')
                    or (key == 'href' and tag == 'a' and safe_link(value))
                    or (key in ('colspan', 'rowspan') and tag in ('td', 'th')
                        and re.fullmatch(r'[1-9][0-9]{0,2}', value or ''))):
                raise ValueError(f'{self.name}: unsupported HTML attribute {key}')
            rendered.append(f' {key}="{escape(value or "", quote=True)}"')
        self.parts.append('<' + tag + ''.join(rendered) + '>')
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise ValueError(f'{self.name}: unmatched closing tag </{tag}>')
        self.parts.append(f'</{tag}>')

    def handle_startendtag(self, tag, attrs):
        if tag not in VOID_TAGS:
            raise ValueError(f'{self.name}: <{tag}> requires a closing tag')
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        self.parts.append(escape(data))


def template_slots(template):
    return {markup or plain: 'html' if markup else 'text'
            for markup, plain in SLOT.findall(template) if (markup or plain) not in DERIVED}


def render(template, mode, values):
    if not isinstance(values, dict):
        raise ValueError('slot data must be a JSON object')
    slots = template_slots(template)
    missing = slots.keys() - values.keys()
    extra = values.keys() - slots.keys()
    if missing or extra:
        raise ValueError(f'missing slots: {", ".join(sorted(missing)) or "none"}; '
                         f'unexpected slots: {", ".join(sorted(extra)) or "none"}')
    for name in REQUIRED_BLOCKS[mode]:
        if not isinstance(values[name], str) or not values[name].strip():
            raise ValueError(f'{name}: fill in evidence or explain why it is unavailable')
    score_key = 'HEALTH_SCORE' if mode == 'single' else 'SIMILARITY'
    score = values[score_key]
    if score is not None and (type(score) not in (int, float) or not 0 <= score <= 100):
        raise ValueError(f'{score_key}: expected a number from 0 to 100 or null')
    if not isinstance(values['SCORE_COLOR'], str) or values['SCORE_COLOR'] not in COLORS:
        raise ValueError('SCORE_COLOR: use a template color')
    ratio = 0 if score is None else score / 100
    rendered = {'SCORE_OFFSET': f'{2 * pi * 34 * (1 - ratio):.2f}',
                'SIMILARITY_WIDTH': str(score if score is not None else 0)}
    for name, kind in slots.items():
        value = values[name]
        if kind == 'html':
            if not isinstance(value, str):
                raise ValueError(f'{name}: expected an HTML fragment or an empty string')
            fragment = Fragment(name)
            fragment.feed(value)
            fragment.close()
            if fragment.stack:
                raise ValueError(f'{name}: unclosed HTML tag <{fragment.stack[-1]}>')
            rendered[name] = ''.join(fragment.parts)
        else:
            if value is not None and type(value) not in (str, int, float):
                raise ValueError(f'{name}: expected a string, number or null')
            text = '—' if value is None else str(value)
            rendered[name] = escape(text + ('%' if name == 'SIMILARITY' and value is not None else ''))
    return SLOT.sub(lambda match: rendered[match[1] or match[2]], template)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Render a reviewed Ascend report with the selected HTML template.')
    parser.add_argument('--template', choices=TEMPLATES, required=True)
    parser.add_argument('--slots', type=Path, help='reviewed and redacted JSON slot values')
    parser.add_argument('--output', type=Path, help='HTML report path')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--list-slots', action='store_true', help='list the input slots and types')
    action.add_argument('--check', action='store_true', help='verify without writing')
    args = parser.parse_args(argv)
    if not args.list_slots and (args.slots is None or args.output is None):
        parser.error('--slots and --output are required unless listing slots')
    try:
        path = Path(__file__).with_name(TEMPLATES[args.template])
        template = path.read_text(encoding='utf-8')
        if args.list_slots:
            print(json.dumps(template_slots(template), indent=2))
            return 0
        inputs = {path.with_name(name).resolve() for name in TEMPLATES.values()}
        inputs.add(args.slots.resolve())
        if args.output.suffix.lower() != '.html' or args.output.resolve() in inputs:
            raise ValueError('output must be a separate .html file')
        expected = render(template, args.template, json.loads(args.slots.read_text(encoding='utf-8-sig')))
        if not args.check:
            args.output.write_text(expected, encoding='utf-8')
        if args.output.read_text(encoding='utf-8') != expected:
            raise ValueError('HTML does not match the selected template and slot data')
    except (OSError, ValueError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    print(f'{"VERIFIED" if args.check else "GENERATED"} {args.output.resolve()}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
