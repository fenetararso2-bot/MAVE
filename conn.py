"""Driver-neutral connection wrapper: ``?`` placeholders, rows readable by name or position."""
import psycopg


class Row(tuple):
    """A result row readable by position and by column name (like ``sqlite3.Row``); ``dict(row)`` works."""

    def __new__(cls, values, index: dict):
        self = super().__new__(cls, values)
        self._index = index
        return self

    def __getitem__(self, key):
        if isinstance(key, str):
            return tuple.__getitem__(self, self._index[key])
        return tuple.__getitem__(self, key)

    def keys(self) -> list[str]:
        return list(self._index)


def _row_factory(cursor):
    cols = [c.name for c in cursor.description or ()]
    index = {name: i for i, name in enumerate(cols)}
    return lambda values: Row(values, index)


_SQL_CACHE: dict[str, str] = {}


def to_pyformat(sql: str) -> str:
    """``?`` -> ``%s`` (outside string literals) and ``%`` -> ``%%``, for statements that carry parameters."""
    cached = _SQL_CACHE.get(sql)
    if cached is not None:
        return cached
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":  # copy a quoted literal verbatim ('' is an escaped quote)
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i : j + 1].replace("%", "%%"))
            i = j + 1
            continue
        out.append("%s" if ch == "?" else "%%" if ch == "%" else ch)
        i += 1
    result = "".join(out)
    if len(_SQL_CACHE) < 2000:
        _SQL_CACHE[sql] = result
    return result


def _clean(params):
    """PostgreSQL text cannot hold NUL (0x00) characters, which web pages and user input may contain
    (SQLite stored them): drop them from every string parameter instead of failing the whole request."""
    return tuple(p.replace("\x00", "") if isinstance(p, str) and "\x00" in p else p for p in params)


class Conn:
    """Thin wrapper over a psycopg connection that understands ``?`` placeholders."""

    def __init__(self, raw: psycopg.Connection):
        self.raw = raw

    def execute(self, sql: str, params=None):
        if not params:
            return self.raw.execute(sql)
        return self.raw.execute(to_pyformat(sql), _clean(params))

    def executemany(self, sql: str, seq):
        rows = [_clean(r) for r in seq]
        cur = self.raw.cursor()
        if rows:
            cur.executemany(to_pyformat(sql), rows)
        return cur

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def transaction(self):
        """``with con.transaction():`` -> a SAVEPOINT inside the current transaction: an error in the block
        is undone without aborting the surrounding transaction."""
        return self.raw.transaction()
