"""
Low-level helpers for reading an Adminer/mysqldump .sql export without a
database server: a streaming row parser for `INSERT INTO ... VALUES (...)`
statements, and a minimal PHP unserialize() for WordPress serialized meta.

Adminer writes one row tuple per line, e.g.:
    INSERT INTO `wp_posts` (`ID`, `post_author`, ...) VALUES
    (1,	1,	'2024-04-06 20:32:22',	...),
    (2,	1,	'2024-04-06 20:32:22',	...);
Embedded newlines in text fields are escaped as literal backslash-n, so each
row really does live on exactly one line - that's what makes a fast,
low-memory line-based parser possible on a multi-GB dump.
"""
import re

INSERT_HEADER_RE = re.compile(r"^INSERT INTO `([a-zA-Z0-9_]+)` \((.*)\) VALUES$")

_ESCAPES = {
    'n': '\n', 'r': '\r', 't': '\t', '0': '\0', 'Z': '\x1a',
    '\\': '\\', "'": "'", '"': '"',
}


def split_row_values(inner):
    """Parse the comma-separated values inside one '(...)' row tuple."""
    vals = []
    i, n = 0, len(inner)
    while i < n:
        while i < n and inner[i] in ' \t':
            i += 1
        if i >= n:
            break
        if inner[i] == "'":
            j = i + 1
            buf = []
            while j < n:
                c = inner[j]
                if c == '\\' and j + 1 < n:
                    nc = inner[j + 1]
                    buf.append(_ESCAPES.get(nc, nc))
                    j += 2
                    continue
                if c == "'":
                    if j + 1 < n and inner[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    j += 1
                    break
                buf.append(c)
                j += 1
            vals.append(''.join(buf))
            i = j
        elif inner[i:i + 4] == 'NULL':
            vals.append(None)
            i += 4
        else:
            j = i
            while j < n and inner[j] != ',':
                j += 1
            vals.append(inner[i:j].strip())
            i = j
        while i < n and inner[i] in ' \t':
            i += 1
        if i < n and inner[i] == ',':
            i += 1
    return vals


def parse_row_line(line):
    """Return (values, is_last_row_of_statement) for a single row line, or
    (None, None) if the line isn't a '(...)' row tuple."""
    s = line.rstrip('\n').rstrip('\r').rstrip()
    if not s.startswith('('):
        return None, None
    if s.endswith(');'):
        body, end = s[1:-2], True
    elif s.endswith('),'):
        body, end = s[1:-2], False
    elif s.endswith(')'):
        body, end = s[1:-1], False
    else:
        return None, None
    return split_row_values(body), end


def iter_insert_rows(path, wanted_tables):
    """
    Stream a .sql dump and yield (table, {column: value}) for every row
    belonging to a table in wanted_tables. Rows for other tables are skipped
    with an O(1) check per line (no value parsing), so this stays fast even
    when the tables we don't care about dwarf the ones we do.
    """
    wanted_tables = set(wanted_tables)
    current_table = None
    current_cols = None
    skipping = False
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            if skipping:
                if line.rstrip().endswith(';'):
                    skipping = False
                continue
            if current_table is None:
                m = INSERT_HEADER_RE.match(line.rstrip('\n').rstrip('\r'))
                if not m:
                    continue
                table = m.group(1)
                if table not in wanted_tables:
                    skipping = True
                    continue
                current_table = table
                current_cols = [c.strip().strip('`') for c in m.group(2).split(',')]
                continue
            values, end = parse_row_line(line)
            if values is None:
                # Not a row line where we expected one - statement didn't
                # look like Adminer's format, bail out of it safely.
                current_table = None
                current_cols = None
                continue
            yield current_table, dict(zip(current_cols, values))
            if end:
                current_table = None
                current_cols = None


def php_unserialize(data):
    """Minimal PHP unserialize() covering the subset WordPress meta uses:
    arrays, strings, ints, floats, bools and null. Returns None on anything
    it can't parse rather than raising, since callers treat missing
    structured data as 'no info available'."""
    if not data:
        return None
    try:
        value, _ = _punserialize(data.encode('utf-8'), 0)
        return value
    except Exception:
        return None


def _punserialize(b, i):
    t = b[i:i + 1]
    if t == b'N':
        return None, i + 2
    if t == b'b':
        return b[i + 2:i + 3] == b'1', i + 4
    if t == b'i':
        end = b.index(b';', i)
        return int(b[i + 2:end]), end + 1
    if t == b'd':
        end = b.index(b';', i)
        return float(b[i + 2:end]), end + 1
    if t == b's':
        colon = b.index(b':', i + 2)
        length = int(b[i + 2:colon])
        start = colon + 2  # skip ':"'
        val = b[start:start + length].decode('utf-8', errors='replace')
        return val, start + length + 2  # skip '";'
    if t == b'a':
        colon = b.index(b':', i + 2)
        count = int(b[i + 2:colon])
        j = colon + 2  # skip ':{'
        result = {}
        for _ in range(count):
            key, j = _punserialize(b, j)
            val, j = _punserialize(b, j)
            result[key] = val
        return result, j + 1  # skip trailing '}'
    raise ValueError(f'unsupported PHP serialize type: {t!r}')
