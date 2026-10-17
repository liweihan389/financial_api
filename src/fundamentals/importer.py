import csv
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import resource
import shutil
import sys
import tempfile
import time
import unicodedata
import uuid
import zipfile

import duckdb
from pypdf import PdfReader

from .store import DATE_FIELDS, KEY_FIELDS, META_FIELDS, Store, dictionaries, ident, literal, revision_sql, typed_projection

METRIC_FIELDS = ["gvkey", "datadate", "conm", "tic", "exchg", "fic", "loc", "priusa", "prican", "costat", "curcdq", "adrrq", "fqtr", "fyearq", "fyr", "apdedateq", "rdq", "fdateq", "pdateq", "ajexq", "adjex", "prccq", "cshoq", "ceqq", "seqq", "atq", "ltq", "niq", "niy", "saleq", "saley", "revtq", "revty", "cogsq", "oiadpq", "epsfiq", "epspiq", "epsfxq", "epspxq", "epsfi12", "epspi12", "epsf12", "epsx12", "epsfiy", "epspiy", "epsfxy", "epspxy"]

def sha_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def make_dictionary(headers, pdf=None):
    described = json.loads(Path(__file__).with_name("compustat_schema.json").read_text())
    if pdf:
        for page_no, page in enumerate(PdfReader(pdf).pages, 1):
            text = unicodedata.normalize("NFKC", page.extract_text())
            for match in re.finditer(r"(?m)^([a-z][a-z0-9]*)\s+(Char|Int|Decimal|Date)\s*\(\1\)\s*([^\n]+)", text):
                name, kind, description = match.groups()
                described[name] = {"type": {"Char": "VARCHAR", "Int": "DOUBLE", "Decimal": "DOUBLE", "Date": "DATE"}[kind], "description": description.strip(), "source_page": page_no}
    result = {}
    for name in headers:
        fallback = "DATE" if name in DATE_FIELDS else "DOUBLE" if name in {"exchg","fyr","fyrc","spcindcd","spcseccd","stko"} else "VARCHAR" if name in META_FIELDS else "DOUBLE"
        result[name] = {"name": name, "type": fallback, "description": name, "definition_source": "inferred_from_compustat_layout", **described.get(name, {})}
        if name in described:
            result[name]["definition_source"] = "Fundamentals Quarterly.pdf" if pdf else "bundled_compustat_field_schema"
        if name in {"gvkey", "cik", "cusip", "tic"}:
            result[name]["type"] = "VARCHAR"
        if name.startswith("eps") or name == "prccq":
            result[name]["unit"] = "currency_per_share; quote currency requires security verification"
        elif name in {"niq", "niy", "saleq", "saley", "revtq", "revty", "cogsq", "oiadpq", "ceqq", "seqq", "atq", "ltq"}:
            result[name]["unit"] = "millions_of_reporting_currency"
        elif name.startswith("csh"):
            result[name]["unit"] = "millions_of_shares"
        else:
            result[name]["unit"] = "source_native; do not assume millions"
    return result

def refresh_inputs(connection):
    dictionary = dictionaries(connection)
    fields = [f for f in METRIC_FIELDS if f in dictionary]
    missing = [f"NULL AS {ident(f)}" for f in METRIC_FIELDS if f not in dictionary]
    revision, args = revision_sql()
    projection = typed_projection(fields, dictionary)
    projection += (", " + ", ".join(missing)) if missing else ""
    connection.execute("DROP TABLE IF EXISTS metric_inputs")
    connection.execute(f"""CREATE TABLE metric_inputs AS WITH current AS ({revision})
        SELECT {projection}, _batch_id, _source_row, _row_hash, _business_key,
        count(*) OVER(PARTITION BY _business_key) AS _conflict,
        count(*) OVER(PARTITION BY gvkey, datadate) AS _period_conflict
        FROM current WHERE _valid_key""", args)
    connection.execute("CREATE INDEX metric_company ON metric_inputs(gvkey)")

def create_clean_views(connection):
    dictionary=dictionaries(connection)
    projection=typed_projection(list(dictionary),dictionary)
    metadata="_batch_id,_source_row,_row_hash,_business_key,_valid_key"
    connection.execute(f"CREATE OR REPLACE VIEW financial_versions AS SELECT {projection},{metadata} FROM records")
    revision,args=revision_sql()
    connection.execute(f"CREATE OR REPLACE VIEW financial_current AS WITH current AS ({revision}) SELECT {projection},{metadata} FROM current")

def initialize(connection):
    connection.execute("CREATE TABLE IF NOT EXISTS imports(batch_id VARCHAR PRIMARY KEY, sequence BIGINT, source_name VARCHAR, source_sha256 VARCHAR UNIQUE, imported_at TIMESTAMP, report JSON)")
    connection.execute("CREATE TABLE IF NOT EXISTS field_dictionary(name VARCHAR PRIMARY KEY, definition JSON)")
    connection.execute("CREATE TABLE IF NOT EXISTS quality_issues(batch_id VARCHAR, source_row BIGINT, field VARCHAR, raw_value VARCHAR, reason VARCHAR)")
    connection.execute("CREATE TABLE IF NOT EXISTS duplicate_provenance(batch_id VARCHAR, source_row BIGINT, retained_source_row BIGINT, row_hash VARCHAR)")
    connection.execute("CREATE TABLE IF NOT EXISTS security_mappings(gvkey VARCHAR, security_id VARCHAR, price_currency VARCHAR, valid_from DATE, valid_to DATE, evidence VARCHAR, split_basis VARCHAR)")

def load_mappings(connection, path):
    with open(path, newline="", encoding="utf-8-sig") as source:
        rows = list(csv.DictReader(source))
    values = []
    for row in rows:
        if not all(row.get(f) for f in ["gvkey", "security_id", "price_currency", "valid_from", "valid_to", "evidence", "split_basis"]):
            raise ValueError("security mapping requires company, security, currency, date interval, evidence and split_basis")
        if row["split_basis"] != "unadjusted_prccq_ajexq":
            raise ValueError("unsupported_split_basis")
        start, end = dt.date.fromisoformat(row["valid_from"]), dt.date.fromisoformat(row["valid_to"])
        if start > end or row["price_currency"] not in {"USD", "CAD"}:
            raise ValueError("invalid_security_mapping")
        values.append(tuple(row[f] for f in ["gvkey", "security_id", "price_currency", "valid_from", "valid_to", "evidence", "split_basis"]))
    connection.execute("DELETE FROM security_mappings")
    if values:
        connection.executemany("INSERT INTO security_mappings VALUES (?,?,?,?,?,?,?)", values)
    overlaps = connection.execute("""WITH numbered AS (SELECT *,row_number() OVER() AS mapping_id FROM security_mappings)
        SELECT count(*) FROM numbered a JOIN numbered b
        ON a.gvkey=b.gvkey AND a.valid_from<=b.valid_to AND b.valid_from<=a.valid_to
        WHERE a.mapping_id<b.mapping_id""").fetchone()[0]
    if overlaps or len(set(values)) != len(values):
        raise ValueError("overlapping_or_duplicate_security_mappings")

def publish(root, filename):
    pending = root / f".active-{uuid.uuid4().hex}.json"
    with pending.open("w") as file:
        json.dump({"database": filename}, file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(pending, root / "active.json")

def import_file(source, root=None, pdf=None, memory="2GB", mappings=None, progress=print):
    source = Path(source).resolve()
    root = Store(root).root
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".import.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("another_import_is_running") from error
        start = time.monotonic()
        digest = sha_file(source)
        current = None
        if (root / "active.json").exists():
            current = Store(root).active_path()
            with duckdb.connect(str(current), read_only=True) as con:
                found = con.execute("SELECT report FROM imports WHERE source_sha256=?", [digest]).fetchone()
                if found:
                    if mappings:
                        raise ValueError("use verify-securities to update mappings on an already imported file")
                    previous=json.loads(found[0])
                    saved=root/f"quality-{previous['batch_id']}.json"
                    return {**(json.loads(saved.read_text()) if saved.exists() else previous), "already_imported": True}
        batch = uuid.uuid4().hex
        filename = f"fundamentals-{batch}.duckdb"
        target = root / filename
        con = None
        try:
            if current:
                shutil.copyfile(current, target)
            con = duckdb.connect(str(target))
            con.execute(f"SET memory_limit={literal(memory)}")
            con.execute("SET threads=2")
            with tempfile.TemporaryDirectory(prefix="import-", dir=root) as working:
                con.execute(f"SET temp_directory={literal(Path(working) / 'spill')}")
                if source.suffix.lower() == ".zip":
                    with zipfile.ZipFile(source) as archive:
                        names = [n for n in archive.namelist() if n.lower().endswith(".csv") and not n.startswith("__MACOSX/") and not Path(n).name.startswith("._")]
                        if len(names) != 1:
                            raise ValueError("archive_must_contain_exactly_one_main_csv")
                        required = archive.getinfo(names[0]).file_size + (current.stat().st_size if current else 0) + 5 * 1024**3
                        if shutil.disk_usage(root).free < required:
                            raise ValueError("insufficient_disk_for_staging_and_5GB_reserve")
                        csv_path = Path(working) / "source.csv"
                        progress("Extracting CSV as a bounded-memory stream...")
                        with archive.open(names[0]) as incoming, csv_path.open("wb") as outgoing:
                            shutil.copyfileobj(incoming, outgoing, 4 * 1024 * 1024)
                else:
                    csv_path = source
                with csv_path.open(newline="", encoding="utf-8-sig") as stream:
                    headers = next(csv.reader(stream))
                if len(set(headers)) != len(headers) or any(not re.fullmatch(r"[a-z][a-z0-9_]*", h) for h in headers):
                    raise ValueError("invalid_or_duplicate_column_names")
                if not {"gvkey", "datadate"}.issubset(headers):
                    raise ValueError("missing_company_or_period_column")
                initialize(con)
                dictionary = make_dictionary(headers, pdf)
                existing = dictionaries(con)
                if existing and set(existing) != set(headers):
                    raise ValueError("schema_changed: all existing columns must be preserved; migrate explicitly")
                for name, definition in dictionary.items():
                    if existing and definition["definition_source"].startswith("inferred"):
                        definition = existing[name]
                    con.execute("INSERT OR REPLACE INTO field_dictionary VALUES (?,?)", [name, json.dumps(definition)])
                sequence = con.execute("SELECT coalesce(max(sequence),0)+1 FROM imports").fetchone()[0]
                con.execute("INSERT INTO imports VALUES (?,?,?,?,?,?)", [batch, sequence, source.name, digest, dt.datetime.now(dt.timezone.utc).replace(tzinfo=None), "{}"])
                progress("Reading all source fields, preserving original values...")
                types = "{" + ",".join(f"{literal(h)}:'VARCHAR'" for h in headers) + "}"
                con.execute(f"CREATE TABLE incoming AS SELECT *, row_number() OVER ()+1 AS _source_row FROM read_csv({literal(csv_path)}, auto_detect=false, delim=',', quote='\"', escape='\"', header=true, columns={types}, parallel=false, strict_mode=true, nullstr='', max_line_size=16777216)")
                source_count = con.execute("SELECT count(*) FROM incoming").fetchone()[0]
                all_fields = ",".join(ident(h) for h in headers)
                key_expressions=[]
                for h in KEY_FIELDS:
                    if h not in headers:
                        key_expressions.append("NULL::VARCHAR")
                    elif h in {"fyearq","fqtr","fyr"}:
                        value=ident(h)
                        key_expressions.append(f"CASE WHEN try_cast({value} AS DOUBLE)=floor(try_cast({value} AS DOUBLE)) THEN cast(try_cast({value} AS BIGINT) AS VARCHAR) ELSE {value} END")
                    elif h=="datadate":
                        key_expressions.append("coalesce(cast(try_cast(datadate AS DATE) AS VARCHAR),datadate)")
                    else:
                        key_expressions.append(f"trim({ident(h)})")
                key_parts=",".join(key_expressions)
                con.execute(f"""CREATE TEMP TABLE hashed AS SELECT _source_row, sha256(to_json(list_value({all_fields}))) AS _row_hash,
                    sha256(to_json(list_value({key_parts}))) AS _business_key,
                    gvkey IS NOT NULL AND regexp_full_match(gvkey,'[0-9]+') AND try_cast(datadate AS DATE) IS NOT NULL AS _valid_key
                    FROM incoming""")
                con.execute("CREATE TEMP TABLE marked AS SELECT *, min(_source_row) OVER(PARTITION BY _row_hash) AS _retained FROM hashed")
                duplicates = con.execute("SELECT count(*) FROM marked WHERE _source_row!=_retained").fetchone()[0]
                con.execute("INSERT INTO duplicate_provenance SELECT ?, _source_row, _retained, _row_hash FROM marked WHERE _source_row!=_retained", [batch])
                con.execute(f"CREATE TEMP VIEW retained AS SELECT i.*,m._row_hash,m._business_key,m._valid_key,{literal(batch)}::VARCHAR AS _batch_id FROM incoming i JOIN marked m USING (_source_row) WHERE m._source_row=m._retained")
                if not current:
                    con.execute("CREATE TABLE records AS SELECT * FROM retained")
                else:
                    con.execute("INSERT INTO records BY NAME SELECT * FROM retained")
                invalid_key = con.execute("SELECT count(*) FROM retained WHERE NOT _valid_key").fetchone()[0]
                con.execute("INSERT INTO quality_issues SELECT ?,_source_row,'gvkey/datadate',coalesce(gvkey,'')||'/'||coalesce(datadate,''),'invalid_business_identifier' FROM retained WHERE NOT _valid_key", [batch])
                progress("Auditing typed conversions and conflicts...")
                checks = []
                for field, definition in dictionaries(con).items():
                    if definition["type"] not in {"DOUBLE", "DATE"}:
                        continue
                    value = ident(field)
                    bad = f"{value} IS NOT NULL AND try_cast({value} AS {definition['type']}) IS NULL"
                    if definition["type"] == "DOUBLE":
                        bad = f"{value} IS NOT NULL AND (try_cast({value} AS DOUBLE) IS NULL OR NOT isfinite(try_cast({value} AS DOUBLE)))"
                    checks.append((field, bad))
                audit_sql = ",".join(f"count(*) FILTER(WHERE {bad}) AS {ident(field)}" for field, bad in checks)
                audit_row = con.execute(f"SELECT {audit_sql} FROM retained").fetchone() if checks else []
                conversion_counts = dict(zip((f for f, _ in checks), audit_row))
                for field, bad in checks:
                    if conversion_counts[field]:
                        con.execute(f"INSERT INTO quality_issues SELECT ?,_source_row,?,{ident(field)},'invalid_typed_value' FROM retained WHERE {bad}", [batch, field])
                nonnull = con.execute("SELECT " + ",".join(f"count({ident(h)})" for h in headers) + " FROM incoming").fetchone()
                if mappings:
                    load_mappings(con, mappings)
                progress("Building typed metric inputs and company indexes...")
                refresh_inputs(con)
                create_clean_views(con)
                conflicts = con.execute("SELECT count(*) FROM metric_inputs WHERE _conflict>1 OR _period_conflict>1").fetchone()[0]
                periods = con.execute("SELECT count(DISTINCT gvkey), min(try_cast(datadate AS DATE)),max(try_cast(datadate AS DATE)) FROM records").fetchone()
                currencies = dict(con.execute("SELECT curcdq,count(*) FROM incoming GROUP BY curcdq").fetchall()) if "curcdq" in headers else {}
                report = {"batch_id": batch, "source_name": source.name, "source_sha256": digest, "source_rows": source_count, "columns": len(headers), "retained_rows": source_count-duplicates, "duplicate_rows": duplicates, "invalid_identifier_rows": invalid_key, "valid_identifier_rows": source_count-duplicates-invalid_key, "reconciliation_ok": source_count == (source_count-duplicates-invalid_key)+invalid_key+duplicates, "companies_all_versions": periods[0], "date_min": str(periods[1]), "date_max": str(periods[2]), "currency_counts": currencies, "invalid_value_counts": {k:v for k,v in conversion_counts.items() if v}, "nonnull_counts": dict(zip(headers,nonnull)), "conflicting_current_period_rows": conflicts, "verified_security_mappings": con.execute("SELECT count(*) FROM security_mappings").fetchone()[0], "duration_seconds": round(time.monotonic()-start,3), "memory_limit": memory, "temporal_guarantee": "revised_snapshot_not_point_in_time", "quote_currency_policy": "valuation_requires_explicit_verified_security_mapping"}
                report["process_peak_rss_bytes"]=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=="darwin" else 1024)
                con.execute("UPDATE imports SET report=? WHERE batch_id=?", [json.dumps(report),batch])
                con.execute("DROP VIEW retained")
                con.execute("DROP TABLE incoming")
                con.execute("CHECKPOINT")
                con.close()
                con = None
            report["database_bytes"] = target.stat().st_size
            from .audit import audit_coverage
            report["metric_coverage"]=audit_coverage(root,filename,report["date_max"],progress)
            (root / f"quality-{batch}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
            publish(root,filename)
            progress(f"Published immutable batch {batch}: {source_count:,} rows")
            return report
        except BaseException:
            if con:
                con.close()
            target.unlink(missing_ok=True)
            Path(str(target)+".wal").unlink(missing_ok=True)
            (root/f"coverage-{filename}.json").unlink(missing_ok=True)
            (root/f"quality-{batch}.json").unlink(missing_ok=True)
            raise

def verify_securities(path, root=None):
    store = Store(root)
    with (store.root / ".import.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        filename = f"fundamentals-mapping-{uuid.uuid4().hex}.duckdb"
        target = store.root / filename
        shutil.copyfile(store.active_path(), target)
        try:
            with duckdb.connect(str(target)) as con:
                load_mappings(con,path)
                create_clean_views(con)
                con.execute("CHECKPOINT")
                count = con.execute("SELECT count(*) FROM security_mappings").fetchone()[0]
            publish(store.root,filename)
            return {"verified_mappings":count,"database":filename}
        except BaseException:
            target.unlink(missing_ok=True)
            raise
