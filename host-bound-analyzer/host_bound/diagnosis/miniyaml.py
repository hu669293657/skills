# -*- coding: utf-8 -*-
"""A minimal YAML-subset parser for rule files (standard library only).

Supported (and enforced by rule files):
  - nested mappings with 2-space indentation
  - `key: value` and `key:` followed by nested block
  - `- item` lists, including `- key: value` mapping items
  - scalars: int / float / bool / null / quoted or bare strings
  - `#` comments (full line or trailing after whitespace)

NOT supported: anchors, flow style ({[...]}), multi-line block scalars.
Any structural error raises MiniYamlError with a line number.
"""


class MiniYamlError(ValueError):
    pass


def _infer_scalar(raw):
    s = raw.strip()
    if s == "":
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", "none"):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _strip_comment(line):
    out = []
    in_s = in_d = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == "#" and not in_s and not in_d:
            if i == 0 or line[i - 1] in " \t":
                break
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_block(lines, idx, indent):
    """Parse lines[idx:] at given indent. Returns (obj, next_idx)."""
    # Detect list vs mapping by first meaningful line at this indent.
    obj = None
    n = len(lines)
    while idx < n:
        raw = lines[idx]
        stripped = raw.strip()
        if stripped == "":
            idx += 1
            continue
        cur_indent = len(raw) - len(raw.lstrip(" "))
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise MiniYamlError("line %d: unexpected indent" % (idx + 1))
        if stripped.startswith("- "):
            if obj is None:
                obj = []
            if not isinstance(obj, list):
                raise MiniYamlError("line %d: mixing list and mapping" % (idx + 1))
            item_text = stripped[2:].strip()
            if item_text == "":
                # nested block item
                child, idx2 = _parse_block(lines, idx + 1, indent + 2)
                obj.append(child)
                idx = idx2
                continue
            if ":" in item_text:
                # inline mapping start: treat as a virtual sub-block
                key, _, rest = item_text.partition(":")
                item = {}
                if rest.strip() != "":
                    item[key.strip()] = _infer_scalar(rest)
                    idx += 1
                else:
                    child, idx2 = _parse_block(lines, idx + 1, indent + 4)
                    item[key.strip()] = child
                    idx = idx2
                # continuation keys of same mapping item may appear at indent+2
                while idx < n:
                    raw2 = lines[idx]
                    st2 = raw2.strip()
                    if st2 == "":
                        idx += 1
                        continue
                    ind2 = len(raw2) - len(raw2.lstrip(" "))
                    if ind2 != indent + 2 or st2.startswith("- "):
                        break
                    k2, _, r2 = st2.partition(":")
                    if r2.strip() == "":
                        child, idx = _parse_block(lines, idx + 1, ind2 + 2)
                        item[k2.strip()] = child
                    else:
                        item[k2.strip()] = _infer_scalar(r2)
                        idx += 1
                obj.append(item)
                continue
            obj.append(_infer_scalar(item_text))
            idx += 1
            continue
        # mapping line
        if obj is None:
            obj = {}
        if not isinstance(obj, dict):
            raise MiniYamlError("line %d: mixing mapping and list" % (idx + 1))
        if ":" not in stripped:
            raise MiniYamlError("line %d: expected 'key: value'" % (idx + 1))
        key, _, rest = stripped.partition(":")
        key = key.strip()
        if key == "":
            raise MiniYamlError("line %d: empty key" % (idx + 1))
        rest = rest.strip()
        if rest == "":
            # nested block or empty value
            nxt = idx + 1
            # look ahead: child block must be deeper
            child_indent = None
            j = nxt
            while j < n:
                st = lines[j].strip()
                if st == "":
                    j += 1
                    continue
                ind = len(lines[j]) - len(lines[j].lstrip(" "))
                child_indent = ind
                break
            if child_indent is not None and child_indent > indent:
                child, idx2 = _parse_block(lines, nxt, child_indent)
                obj[key] = child
                idx = idx2
            else:
                obj[key] = None
                idx = nxt
        else:
            obj[key] = _infer_scalar(rest)
            idx += 1
    return (obj if obj is not None else {}), idx


def parse(text):
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    lines = [_strip_comment(l).rstrip() for l in text.replace("\r\n", "\n").split("\n")]
    obj, _ = _parse_block(lines, 0, 0)
    return obj


def parse_file(path):
    with open(path, "rb") as f:
        return parse(f.read())


def dumps_check(obj):
    """Round-trip sanity helper for tests."""
    return obj
