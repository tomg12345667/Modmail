"""
A small, MongoDB-compatible document store backed by JSON files.

It mimics the subset of the Motor (async PyMongo) API that Modmail and its
plugins use, so the rest of the bot works without any changes:

    db = JSONDatabase("data")
    await db.logs.insert_one({...})
    await db.logs.find({"recipient.id": "123"}).to_list(None)
    await db.config.update_one({"bot_id": 1}, {"$set": {...}}, upsert=True)
    db.plugins["MyCog"]          # plugin partitions

Storage layout: every collection is a folder, every document is one
``.json`` file inside it, e.g. ``data/logs/<key>.json``. All documents are
also kept in memory, so reads are fast; a write only rewrites the single
document that changed (atomically, via a temp file + rename).
"""

import asyncio
import copy
import datetime
import hashlib
import json
import os
import re
import secrets
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "JSONDatabase",
    "JSONCollection",
    "InsertOneResult",
    "InsertManyResult",
    "UpdateResult",
    "DeleteResult",
]

_MISSING = object()
ASCENDING = 1
DESCENDING = -1


# --------------------------------------------------------------------------- #
# Result objects (same attribute names as pymongo.results)
# --------------------------------------------------------------------------- #
class InsertOneResult:
    def __init__(self, inserted_id):
        self.inserted_id = inserted_id
        self.acknowledged = True


class InsertManyResult:
    def __init__(self, inserted_ids):
        self.inserted_ids = inserted_ids
        self.acknowledged = True


class UpdateResult:
    def __init__(self, matched_count=0, modified_count=0, upserted_id=None):
        self.matched_count = matched_count
        self.modified_count = modified_count
        self.upserted_id = upserted_id
        self.acknowledged = True

    @property
    def raw_result(self):
        return {"n": self.matched_count, "nModified": self.modified_count, "upserted": self.upserted_id}


class DeleteResult:
    def __init__(self, deleted_count=0):
        self.deleted_count = deleted_count
        self.acknowledged = True


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _new_id() -> str:
    """24-char hex id, same shape as a Mongo ObjectId."""
    return secrets.token_hex(12)


def _json_default(obj):
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, datetime.timedelta):
        return obj.total_seconds()
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    return str(obj)


def _normalize(value):
    """Round-trip through JSON so in-memory data always equals what's on disk."""
    return json.loads(json.dumps(value, default=_json_default))


def _type_rank(v):
    # Mongo-like cross-type ordering for sorting.
    if v is None or v is _MISSING:
        return 0
    if isinstance(v, bool):
        return 5
    if isinstance(v, (int, float)):
        return 1
    if isinstance(v, str):
        return 2
    if isinstance(v, dict):
        return 3
    if isinstance(v, list):
        return 4
    return 6


def _get_values(doc, path: str) -> List[Any]:
    """Resolve a dotted path, fanning out over arrays like Mongo does.
    Returns every candidate value (arrays are returned as-is *and* expanded)."""
    parts = path.split(".")
    current = [doc]
    for part in parts:
        nxt = []
        for item in current:
            if isinstance(item, dict):
                if part in item:
                    nxt.append(item[part])
            elif isinstance(item, list):
                if part.isdigit() and int(part) < len(item):
                    nxt.append(item[int(part)])
                else:
                    for el in item:
                        if isinstance(el, dict) and part in el:
                            nxt.append(el[part])
        current = nxt
        if not current:
            return []
    return current


def _expand(values):
    out = []
    for v in values:
        out.append(v)
        if isinstance(v, list):
            out.extend(v)
    return out


def _cmp_ok(a, b, op) -> bool:
    if a is None or b is None:
        return False
    num = (int, float)
    if isinstance(a, bool) or isinstance(b, bool):
        if not (isinstance(a, bool) and isinstance(b, bool)):
            return False
    elif isinstance(a, num) and isinstance(b, num):
        pass
    elif type(a) is not type(b):
        return False
    try:
        if op == "$gt":
            return a > b
        if op == "$gte":
            return a >= b
        if op == "$lt":
            return a < b
        if op == "$lte":
            return a <= b
    except TypeError:
        return False
    return False


def _eq(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def _regex(pattern, options=""):
    flags = 0
    for ch in options or "":
        flags |= {"i": re.I, "m": re.M, "s": re.S, "x": re.X}.get(ch, 0)
    if isinstance(pattern, re.Pattern):
        return pattern
    return re.compile(pattern, flags)


def _iter_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _iter_strings(v)


def _text_match(doc, spec) -> bool:
    """Emulates Mongo's $text search (case-insensitive, quoted = exact phrase)."""
    search = spec.get("$search", "") if isinstance(spec, dict) else str(spec)
    phrases = re.findall(r'"([^"]*)"', search)
    rest = re.sub(r'"[^"]*"', " ", search)
    terms = [t for t in rest.split() if not t.startswith("-")]
    negs = [t[1:] for t in rest.split() if t.startswith("-") and len(t) > 1]

    haystack = "\n".join(_iter_strings(doc)).lower()
    if any(n.lower() in haystack for n in negs):
        return False
    if phrases and not all(p.lower() in haystack for p in phrases):
        return False
    if terms and not any(t.lower() in haystack for t in terms):
        return False
    return bool(phrases or terms)


def _match_condition(values: List[Any], cond, exists: bool) -> bool:
    """Does any candidate value satisfy the condition?"""
    if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
        for op, arg in cond.items():
            if op == "$options":
                continue
            if not _match_operator(values, op, arg, exists, cond):
                return False
        return True
    if isinstance(cond, re.Pattern):
        return any(isinstance(v, str) and cond.search(v) for v in _expand(values))
    # Plain equality
    if cond is None:
        return not exists or any(v is None for v in _expand(values))
    return any(_eq(v, cond) for v in _expand(values))


def _match_operator(values, op, arg, exists, cond) -> bool:
    flat = _expand(values)
    if op == "$eq":
        return _match_condition(values, arg, exists)
    if op == "$ne":
        return not _match_condition(values, arg, exists)
    if op in ("$gt", "$gte", "$lt", "$lte"):
        return any(_cmp_ok(v, arg, op) for v in flat)
    if op == "$in":
        return any(_match_condition(values, a, exists) for a in arg)
    if op == "$nin":
        return not any(_match_condition(values, a, exists) for a in arg)
    if op == "$exists":
        return exists == bool(arg)
    if op == "$not":
        return not _match_condition(values, arg, exists)
    if op == "$regex":
        rx = _regex(arg, cond.get("$options", ""))
        return any(isinstance(v, str) and rx.search(v) for v in flat)
    if op == "$size":
        return any(isinstance(v, list) and len(v) == arg for v in values)
    if op == "$all":
        return all(any(_eq(v, a) for v in flat) for a in arg)
    if op == "$elemMatch":
        for v in values:
            if isinstance(v, list):
                for el in v:
                    if isinstance(el, dict) and match(el, arg):
                        return True
                    if not isinstance(el, dict) and _match_condition([el], arg, True):
                        return True
        return False
    if op == "$type":
        names = {"string": str, "bool": bool, "object": dict, "array": list, "null": type(None)}
        t = names.get(arg)
        return t is not None and any(isinstance(v, t) for v in values)
    if op == "$mod":
        div, rem = arg
        return any(isinstance(v, (int, float)) and v % div == rem for v in flat)
    raise NotImplementedError(f"Query operator {op} is not supported by the JSON database.")


def match(doc: dict, query: Optional[dict]) -> bool:
    if not query:
        return True
    for key, cond in query.items():
        if key == "$and":
            if not all(match(doc, q) for q in cond):
                return False
        elif key == "$or":
            if not any(match(doc, q) for q in cond):
                return False
        elif key == "$nor":
            if any(match(doc, q) for q in cond):
                return False
        elif key == "$text":
            if not _text_match(doc, cond):
                return False
        elif key == "$where" or key == "$comment":
            continue
        else:
            values = _get_values(doc, key)
            if not _match_condition(values, cond, bool(values)):
                return False
    return True


# ---- update helpers ------------------------------------------------------- #
def _split(path):
    return path.split(".")


def _resolve_positional(path: str, doc: dict, query: dict) -> str:
    """Replace a `$` positional element with the index of the first matched array element."""
    if ".$" not in path and not path.startswith("$"):
        return path
    parts = _split(path)
    idx = parts.index("$")
    prefix = ".".join(parts[:idx])
    arr = _get_values(doc, prefix)
    arr = arr[0] if arr and isinstance(arr[0], list) else []
    sub = {}
    for k, v in (query or {}).items():
        if k.startswith(prefix + "."):
            sub[k[len(prefix) + 1 :]] = v
        elif k == prefix and isinstance(v, dict) and "$elemMatch" in v:
            sub.update(v["$elemMatch"])
    position = None
    for i, el in enumerate(arr):
        if (isinstance(el, dict) and match(el, sub)) or (not isinstance(el, dict) and not sub):
            position = i
            break
    if position is None:
        raise ValueError("The positional operator did not find the match needed from the query.")
    parts[idx] = str(position)
    return ".".join(parts)


def _get_parent(doc, path, create=True):
    parts = _split(path)
    cur = doc
    for p in parts[:-1]:
        if isinstance(cur, list):
            i = int(p)
            cur = cur[i]
            continue
        if p not in cur or not isinstance(cur[p], (dict, list)):
            if not create:
                return None, parts[-1]
            cur[p] = {}
        cur = cur[p]
    return cur, parts[-1]


def _set(doc, path, value):
    parent, last = _get_parent(doc, path)
    if isinstance(parent, list):
        i = int(last)
        while len(parent) <= i:
            parent.append(None)
        parent[i] = value
    else:
        parent[last] = value


def _get(doc, path, default=_MISSING):
    cur = doc
    for p in _split(path):
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        elif isinstance(cur, list) and p.isdigit() and int(p) < len(cur):
            cur = cur[int(p)]
        else:
            return default
    return cur


def _unset(doc, path):
    parent, last = _get_parent(doc, path, create=False)
    if parent is None:
        return
    if isinstance(parent, list):
        i = int(last)
        if i < len(parent):
            parent[i] = None
    else:
        parent.pop(last, None)


def apply_update(doc: dict, update: dict, query: dict, *, inserting=False) -> dict:
    """Apply a Mongo update document in place. Returns the document."""
    if not any(k.startswith("$") for k in update):
        # Replacement document
        _id = doc.get("_id")
        doc.clear()
        doc.update(copy.deepcopy(update))
        if _id is not None:
            doc["_id"] = _id
        return doc

    for op, fields in update.items():
        if op == "$setOnInsert" and not inserting:
            continue
        for raw_path, value in (fields or {}).items():
            path = _resolve_positional(raw_path, doc, query)
            value = copy.deepcopy(value)
            if op in ("$set", "$setOnInsert"):
                _set(doc, path, value)
            elif op == "$unset":
                _unset(doc, path)
            elif op == "$inc":
                _set(doc, path, (_get(doc, path, 0) or 0) + value)
            elif op == "$mul":
                _set(doc, path, (_get(doc, path, 0) or 0) * value)
            elif op == "$min":
                cur = _get(doc, path)
                if cur is _MISSING or _cmp_ok(value, cur, "$lt"):
                    _set(doc, path, value)
            elif op == "$max":
                cur = _get(doc, path)
                if cur is _MISSING or _cmp_ok(value, cur, "$gt"):
                    _set(doc, path, value)
            elif op == "$rename":
                cur = _get(doc, path)
                if cur is not _MISSING:
                    _unset(doc, path)
                    _set(doc, value, cur)
            elif op == "$currentDate":
                _set(doc, path, datetime.datetime.now(datetime.timezone.utc).isoformat())
            elif op in ("$push", "$addToSet"):
                arr = _get(doc, path)
                if arr is _MISSING or arr is None:
                    arr = []
                    _set(doc, path, arr)
                if not isinstance(arr, list):
                    raise TypeError(f"Cannot apply {op} to non-array field {path}")
                items = value["$each"] if isinstance(value, dict) and "$each" in value else [value]
                for item in items:
                    if op == "$addToSet" and item in arr:
                        continue
                    if isinstance(value, dict) and "$position" in value:
                        arr.insert(value["$position"], item)
                    else:
                        arr.append(item)
                if isinstance(value, dict) and "$slice" in value:
                    s = value["$slice"]
                    arr[:] = arr[s:] if s < 0 else arr[:s]
            elif op == "$pull":
                arr = _get(doc, path)
                if isinstance(arr, list):
                    if isinstance(value, dict):
                        arr[:] = [
                            el
                            for el in arr
                            if not (match(el, value) if isinstance(el, dict) else _match_condition([el], value, True))
                        ]
                    else:
                        arr[:] = [el for el in arr if el != value]
            elif op == "$pullAll":
                arr = _get(doc, path)
                if isinstance(arr, list):
                    arr[:] = [el for el in arr if el not in value]
            elif op == "$pop":
                arr = _get(doc, path)
                if isinstance(arr, list) and arr:
                    arr.pop(0 if value == -1 else -1)
            else:
                raise NotImplementedError(f"Update operator {op} is not supported by the JSON database.")
    return doc


def apply_projection(doc: dict, projection) -> dict:
    if not projection:
        return doc
    if isinstance(projection, (list, tuple)):
        projection = {k: 1 for k in projection}

    out = copy.deepcopy(doc)
    slices = {k: v["$slice"] for k, v in projection.items() if isinstance(v, dict) and "$slice" in v}
    plain = {k: v for k, v in projection.items() if k not in slices}
    includes = [k for k, v in plain.items() if v and k != "_id"]
    excludes = [k for k, v in plain.items() if not v]

    if includes:
        new = {}
        if plain.get("_id", 1) and "_id" in out:
            new["_id"] = out["_id"]
        for k in includes + list(slices):
            v = _get(out, k)
            if v is not _MISSING:
                _set(new, k, v)
        out = new
    else:
        for k in excludes:
            _unset(out, k)

    for k, s in slices.items():
        arr = _get(out, k)
        if isinstance(arr, list):
            if isinstance(s, list):
                skip, n = s
                _set(out, k, arr[skip : skip + n] if skip >= 0 else arr[skip:][:n])
            else:
                _set(out, k, arr[s:] if s < 0 else arr[:s])
    return out


def _sort_key_spec(key_or_list, direction=None) -> List[Tuple[str, int]]:
    if key_or_list is None:
        return []
    if isinstance(key_or_list, str):
        return [(key_or_list, direction if direction is not None else ASCENDING)]
    if isinstance(key_or_list, dict):
        return list(key_or_list.items())
    return [(k, d) for k, d in key_or_list]


def _sort_docs(docs: List[dict], spec: List[Tuple[str, int]]) -> List[dict]:
    for key, direction in reversed(spec):

        def k(d, key=key):
            v = _get(d, key, None)
            return (_type_rank(v), v if _type_rank(v) in (1, 2, 5) else 0)

        docs.sort(key=k, reverse=direction == DESCENDING)
    return docs


# --------------------------------------------------------------------------- #
# Cursor
# --------------------------------------------------------------------------- #
class JSONCursor:
    def __init__(self, collection: "JSONCollection", query, projection):
        self._collection = collection
        self._query = query or {}
        self._projection = projection
        self._sort: List[Tuple[str, int]] = []
        self._skip = 0
        self._limit = 0
        self._results: Optional[List[dict]] = None
        self._iter_index = 0

    def sort(self, key_or_list, direction=None):
        self._sort = _sort_key_spec(key_or_list, direction)
        return self

    def skip(self, n: int):
        self._skip = n
        return self

    def limit(self, n: int):
        self._limit = n
        return self

    def batch_size(self, _n):
        return self

    def _materialize(self) -> List[dict]:
        if self._results is None:
            docs = [d for d in self._collection._docs.values() if match(d, self._query)]
            docs = _sort_docs(list(docs), self._sort)
            if self._skip:
                docs = docs[self._skip :]
            if self._limit:
                docs = docs[: self._limit]
            self._results = [apply_projection(copy.deepcopy(d), self._projection) for d in docs]
        return self._results

    async def to_list(self, length: Optional[int] = None) -> List[dict]:
        results = self._materialize()[self._iter_index :]
        if length:
            results = results[:length]
        self._iter_index += len(results)
        return results

    def __aiter__(self):
        return self

    async def __anext__(self):
        results = self._materialize()
        if self._iter_index >= len(results):
            raise StopAsyncIteration
        self._iter_index += 1
        return results[self._iter_index - 1]

    async def next(self):
        return await self.__anext__()

    def __iter__(self):
        return iter(self._materialize())


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


class JSONCollection:
    def __init__(self, database: "JSONDatabase", name: str):
        self.database = database
        self.name = name
        self.full_name = f"{database.name}.{name}"
        self._dir = os.path.join(database.path, name)
        self._docs: Dict[str, dict] = {}
        self._files: Dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._load()

    # ---- sub-collections (db.plugins["Name"] / db.plugins.Name) --------- #
    def __getitem__(self, name: str) -> "JSONCollection":
        return self.database.get_collection(f"{self.name}.{name}")

    def __getattr__(self, name: str) -> "JSONCollection":
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    # ---- storage --------------------------------------------------------- #
    @staticmethod
    def _key(_id) -> str:
        return json.dumps(_id, sort_keys=True, default=str)

    def _filename(self, _id) -> str:
        raw = str(_id)
        safe = _SAFE.sub("_", raw)[:80]
        if safe != raw or not isinstance(_id, str) or not safe:
            safe = f"{safe}-{hashlib.sha1(self._key(_id).encode()).hexdigest()[:10]}"
        return os.path.join(self._dir, safe + ".json")

    def _load(self):
        if not os.path.isdir(self._dir):
            return
        for fname in os.listdir(self._dir):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(self._dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as fp:
                    doc = json.load(fp)
            except (OSError, ValueError) as e:
                self.database.log(f"Skipping unreadable JSON document {path}: {e}")
                continue
            if not isinstance(doc, dict) or "_id" not in doc:
                continue
            k = self._key(doc["_id"])
            self._docs[k] = doc
            self._files[k] = path

    def _write(self, doc: dict):
        os.makedirs(self._dir, exist_ok=True)
        k = self._key(doc["_id"])
        path = self._files.get(k) or self._filename(doc["_id"])
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(doc, fp, ensure_ascii=False, indent=self.database.indent, default=_json_default)
            fp.flush()
            if self.database.fsync:
                os.fsync(fp.fileno())
        os.replace(tmp, path)
        self._files[k] = path

    def _remove(self, k: str):
        path = self._files.pop(k, None)
        self._docs.pop(k, None)
        if path:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    def _store(self, doc: dict):
        doc = _normalize(doc)
        k = self._key(doc["_id"])
        self._docs[k] = doc
        self._write(doc)
        return doc

    def _find_matching(self, query, sort=None) -> List[dict]:
        docs = [d for d in self._docs.values() if match(d, query or {})]
        if sort:
            docs = _sort_docs(docs, _sort_key_spec(sort))
        return docs

    # ---- reads ----------------------------------------------------------- #
    def find(self, filter=None, projection=None, *args, **kwargs) -> JSONCursor:
        projection = kwargs.pop("projection", projection)
        cursor = JSONCursor(self, filter, projection)
        if kwargs.get("sort"):
            cursor.sort(kwargs["sort"])
        if kwargs.get("skip"):
            cursor.skip(kwargs["skip"])
        if kwargs.get("limit"):
            cursor.limit(kwargs["limit"])
        return cursor

    async def find_one(self, filter=None, projection=None, *args, **kwargs) -> Optional[dict]:
        if filter is not None and not isinstance(filter, dict):
            filter = {"_id": filter}
        kwargs.pop("limit", None)
        results = await self.find(filter, projection, *args, limit=1, **kwargs).to_list(1)
        return results[0] if results else None

    async def count_documents(self, filter=None, **kwargs) -> int:
        n = len(self._find_matching(filter))
        if kwargs.get("skip"):
            n = max(0, n - kwargs["skip"])
        if kwargs.get("limit"):
            n = min(n, kwargs["limit"])
        return n

    async def estimated_document_count(self, **kwargs) -> int:
        return len(self._docs)

    async def distinct(self, key, filter=None, **kwargs) -> list:
        out = []
        for d in self._find_matching(filter):
            for v in _expand(_get_values(d, key)):
                if not isinstance(v, list) and v not in out:
                    out.append(copy.deepcopy(v))
        return out

    # ---- writes ---------------------------------------------------------- #
    async def insert_one(self, document: dict, *args, **kwargs) -> InsertOneResult:
        async with self._lock:
            if "_id" not in document:
                document["_id"] = _new_id()  # pymongo also mutates the passed dict
            if self._key(document["_id"]) in self._docs:
                raise DuplicateKeyError(f"Duplicate _id {document['_id']!r} in {self.name}")
            self._store(copy.deepcopy(document))
            return InsertOneResult(document["_id"])

    async def insert_many(self, documents: Iterable[dict], *args, **kwargs) -> InsertManyResult:
        ids = []
        for d in documents:
            ids.append((await self.insert_one(d)).inserted_id)
        return InsertManyResult(ids)

    def _upsert_doc(self, filter, update) -> dict:
        base = {}
        for k, v in (filter or {}).items():
            if k.startswith("$"):
                continue
            if isinstance(v, dict) and any(x.startswith("$") for x in v):
                if "$eq" in v:
                    _set(base, k, copy.deepcopy(v["$eq"]))
                continue
            _set(base, k, copy.deepcopy(v))
        if not any(k.startswith("$") for k in update):
            base = copy.deepcopy(update)
        else:
            apply_update(base, update, filter, inserting=True)
        if "_id" not in base:
            base["_id"] = _new_id()
        return base

    async def _update(self, filter, update, *, many, upsert, sort=None) -> Tuple[UpdateResult, Optional[dict], Optional[dict]]:
        if isinstance(update, list):
            raise NotImplementedError("Aggregation-pipeline updates are not supported by the JSON database.")
        async with self._lock:
            matches = self._find_matching(filter, sort)
            if not many:
                matches = matches[:1]
            if not matches:
                if upsert:
                    doc = self._store(self._upsert_doc(filter, update))
                    return UpdateResult(0, 0, doc["_id"]), None, copy.deepcopy(doc)
                return UpdateResult(), None, None
            modified = 0
            before = after = None
            for doc in matches:
                original = copy.deepcopy(doc)
                new = apply_update(copy.deepcopy(doc), update, filter)
                if "_id" not in new:
                    new["_id"] = original["_id"]
                if new != original:
                    if new["_id"] != original["_id"]:
                        raise WriteError("Performing an update on the path '_id' would modify the immutable field '_id'")
                    self._store(new)
                    modified += 1
                before = before or original
                after = after or copy.deepcopy(self._docs[self._key(new["_id"])])
            return UpdateResult(len(matches), modified), before, after

    async def update_one(self, filter, update, upsert=False, *args, **kwargs) -> UpdateResult:
        return (await self._update(filter, update, many=False, upsert=upsert, sort=kwargs.get("sort")))[0]

    async def update_many(self, filter, update, upsert=False, *args, **kwargs) -> UpdateResult:
        return (await self._update(filter, update, many=True, upsert=upsert))[0]

    async def replace_one(self, filter, replacement, upsert=False, *args, **kwargs) -> UpdateResult:
        if any(k.startswith("$") for k in replacement):
            raise ValueError("replacement can not include $ operators")
        return (await self._update(filter, replacement, many=False, upsert=upsert))[0]

    async def find_one_and_update(
        self, filter, update, projection=None, sort=None, upsert=False, return_document=False, **kwargs
    ) -> Optional[dict]:
        _, before, after = await self._update(filter, update, many=False, upsert=upsert, sort=sort)
        doc = after if return_document else before
        return apply_projection(doc, projection) if doc is not None else None

    async def find_one_and_replace(
        self, filter, replacement, projection=None, sort=None, upsert=False, return_document=False, **kwargs
    ) -> Optional[dict]:
        return await self.find_one_and_update(
            filter, replacement, projection, sort, upsert, return_document, **kwargs
        )

    async def find_one_and_delete(self, filter, projection=None, sort=None, **kwargs) -> Optional[dict]:
        async with self._lock:
            matches = self._find_matching(filter, sort)
            if not matches:
                return None
            doc = copy.deepcopy(matches[0])
            self._remove(self._key(doc["_id"]))
            return apply_projection(doc, projection)

    async def delete_one(self, filter, *args, **kwargs) -> DeleteResult:
        async with self._lock:
            matches = self._find_matching(filter)[:1]
            for d in matches:
                self._remove(self._key(d["_id"]))
            return DeleteResult(len(matches))

    async def delete_many(self, filter, *args, **kwargs) -> DeleteResult:
        async with self._lock:
            matches = self._find_matching(filter)
            for d in matches:
                self._remove(self._key(d["_id"]))
            return DeleteResult(len(matches))

    async def drop(self, *args, **kwargs):
        async with self._lock:
            for k in list(self._docs):
                self._remove(k)

    async def bulk_write(self, requests, *args, **kwargs):
        raise NotImplementedError("bulk_write is not supported by the JSON database.")

    async def aggregate_list(self, *a, **k):
        raise NotImplementedError("aggregate is not supported by the JSON database.")

    def aggregate(self, pipeline, *args, **kwargs):
        raise NotImplementedError("aggregate is not supported by the JSON database.")

    # ---- indexes are irrelevant for an in-memory store --------------------- #
    async def create_index(self, keys, **kwargs):
        if isinstance(keys, str):
            return f"{keys}_1"
        return "_".join(f"{k}_{v}" for k, v in keys)

    async def create_indexes(self, indexes, **kwargs):
        return []

    async def index_information(self) -> dict:
        return {"_id_": {"key": [("_id", 1)]}}

    async def drop_index(self, *args, **kwargs):
        return None

    async def drop_indexes(self, *args, **kwargs):
        return None

    def __repr__(self):
        return f"<JSONCollection {self.full_name!r} ({len(self._docs)} documents)>"


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
class DuplicateKeyError(Exception):
    pass


class WriteError(Exception):
    pass


class JSONDatabase:
    def __init__(self, path: str, name: str = "modmail_bot", *, indent: Optional[int] = 2, fsync=False, logger=None):
        self.path = os.path.abspath(path)
        self.name = name
        self.indent = indent
        self.fsync = fsync
        self._logger = logger
        self._collections: Dict[str, JSONCollection] = {}
        os.makedirs(self.path, exist_ok=True)

    def log(self, msg):
        if self._logger:
            self._logger.warning(msg)

    def get_collection(self, name: str) -> JSONCollection:
        if name not in self._collections:
            self._collections[name] = JSONCollection(self, name)
        return self._collections[name]

    def __getitem__(self, name: str) -> JSONCollection:
        return self.get_collection(name)

    def __getattr__(self, name: str) -> JSONCollection:
        if name.startswith("_"):
            raise AttributeError(name)
        return self.get_collection(name)

    async def list_collection_names(self, *args, **kwargs) -> List[str]:
        names = set(self._collections)
        for entry in os.listdir(self.path):
            if os.path.isdir(os.path.join(self.path, entry)):
                names.add(entry)
        return sorted(names)

    async def command(self, command, *args, **kwargs):
        # Used by Modmail to "ping" the database; nothing to do for files.
        if command in ("ping", "buildinfo", {"ping": 1}):
            return {"ok": 1.0, "version": "json"}
        return {"ok": 1.0}

    async def drop_collection(self, name):
        await self.get_collection(name if isinstance(name, str) else name.name).drop()

    def __repr__(self):
        return f"<JSONDatabase {self.path!r}>"
