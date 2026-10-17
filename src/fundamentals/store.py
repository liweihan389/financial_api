import contextlib
import json
import os
from pathlib import Path

import duckdb

DATE_FIELDS = {"datadate", "dldte", "ipodate", "apdedateq", "fdateq", "pdateq", "rdq"}
KEY_FIELDS = ["gvkey", "datadate", "fyearq", "fqtr", "fyr", "indfmt", "datafmt", "consol", "curcdq"]
META_FIELDS = {"costat", "curcdq", "datafmt", "indfmt", "consol", "gvkey", "conm", "tic", "cusip", "cik", "exchg", "fyr", "fic", "add1", "add2", "add3", "add4", "addzip", "busdesc", "city", "conml", "county", "dlrsn", "ein", "fax", "fyrc", "ggroup", "gind", "gsector", "gsubind", "idbflag", "incorp", "loc", "naics", "phone", "prican", "prirow", "priusa", "sic", "spcindcd", "spcseccd", "spcsrc", "state", "stko", "weburl", "acctchgq", "acctstdq", "bsprq", "compstq", "curncdq", "datacqtr", "datafqtr", "finalq", "ogmq", "rp", "scfq", "srcq", "staltq", "updq"}

def ident(name):
    return '"' + name.replace('"', '""') + '"'

def literal(value):
    return "'" + str(value).replace("'", "''") + "'"

def default_root():
    return Path(os.environ.get("FINANCIAL_DATA_DIR", Path(__file__).resolve().parents[3] / "storage"))

class Store:
    def __init__(self, root=None):
        self.root = Path(root or default_root()).resolve()

    def is_initialized(self):
        return (self.root / "active.json").is_file()

    def active_path(self):
        pointer = self.root / "active.json"
        if not pointer.exists():
            raise ValueError("database_not_initialized: run financial-data import first")
        name = json.loads(pointer.read_text())["database"]
        path = (self.root / name).resolve()
        if path.parent != self.root or not path.is_file():
            raise ValueError("invalid_database_pointer")
        return path

    @contextlib.contextmanager
    def connect(self, generation=None):
        # Resolve once per request. Old immutable generations remain available
        # for in-flight requests; no live database is mutated by imports.
        path = self.active_path() if generation is None else (self.root / generation).resolve()
        if path.parent != self.root or not path.is_file() or path.suffix != ".duckdb":
            raise ValueError("unknown_database_generation")
        connection = duckdb.connect(str(path), read_only=True)
        try:
            connection.execute("SET memory_limit='1GB'")
            connection.execute("SET threads=2")
            from .telemetry import track_version
            track_version("financial_generation",path.name)
            yield connection
        finally:
            connection.close()

def dictionaries(connection):
    return {r[0]: json.loads(r[1]) for r in connection.execute("SELECT name, definition FROM field_dictionary").fetchall()}

def typed_projection(fields, dictionary):
    expressions = []
    for field in fields:
        kind = dictionary[field]["type"]
        if kind == "DATE":
            expressions.append(f"try_cast({ident(field)} AS DATE) AS {ident(field)}")
        elif kind == "DOUBLE":
            expressions.append(f"CASE WHEN isfinite(try_cast({ident(field)} AS DOUBLE)) THEN try_cast({ident(field)} AS DOUBLE) END AS {ident(field)}")
        else:
            expressions.append(ident(field))
    return ", ".join(expressions)

def revision_sql(batch_id=None, gvkeys=None):
    # Latest imported batch containing each business key, not a wholesale
    # replacement of the prior universe. Conflicting variants are retained.
    conditions=[]
    args=[]
    if batch_id is not None:
        conditions.append("b.sequence <= (SELECT sequence FROM imports WHERE batch_id=?)")
        args.append(batch_id)
    if gvkeys:
        conditions.append("r.gvkey IN ("+",".join("?" for _ in gvkeys)+")")
        args.extend(gvkeys)
    where="WHERE "+" AND ".join(conditions) if conditions else ""
    sql = f"""WITH eligible AS (
        SELECT r.*, b.sequence AS _sequence FROM records r JOIN imports b ON r._batch_id=b.batch_id {where}
    ), ranked AS (
        SELECT *, max(_sequence) OVER(PARTITION BY _business_key) AS _latest FROM eligible
    ) SELECT * EXCLUDE (_latest) FROM ranked WHERE _sequence=_latest"""
    return sql,args

def validate_batch(connection, batch_id):
    if batch_id is not None and not connection.execute("SELECT 1 FROM imports WHERE batch_id=?", [batch_id]).fetchone():
        raise ValueError("unknown_batch_id")
