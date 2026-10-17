"""Versioned currency evidence, independent of security/ADR share-unit audits."""
from functools import lru_cache
import hashlib
from importlib.resources import files
import json
import math

CURRENCY_POLICIES = {"infer_curcdq", "verified_only"}
SHARE_POLICIES = {"infer_compustat", "verified_only"}
VALUATION_POLICY_VERSION = "curcdq-adr-default-v2"


def value_fingerprint(value):
    try:return hashlib.sha256(float(value).hex().encode()).hexdigest()
    except (ValueError,TypeError):return None


@lru_cache(maxsize=1)
def share_evidence():
    raw = files("fundamentals").joinpath("share_basis_evidence.json").read_bytes()
    payload = json.loads(raw)
    index = {}
    for record in payload["records"]:
        index.setdefault((record["gvkey"], record["datadate"], record["source_row_hash"]), []).append(record)
    return index, hashlib.sha256(raw).hexdigest()


def matching_share_evidence(row):
    if not row:
        return []
    records = share_evidence()[0].get((row["gvkey"], str(row["datadate"]), row.get("_row_hash")), [])
    return [r for r in records if (r.get("adr_ratio_hash")==value_fingerprint(row.get("adrrq")) if "adr_ratio_hash" in r else r.get("adrrq")==row.get("adrrq"))
            and r["local_currency"] == row.get("curcdq")
            and (r["field"] not in row or (r.get("local_value_hash")==value_fingerprint(row[r["field"]]) if "local_value_hash" in r else r.get("local_value")==row[r["field"]]))]


def is_adr(row):
    return bool(row) and (row.get("adrrq") is not None or row.get("priusa") == "90")


@lru_cache(maxsize=1)
def price_evidence():
    raw = files("fundamentals").joinpath("price_currency_evidence.json").read_bytes()
    payload = json.loads(raw)
    index = {(r["gvkey"], r["period"]): r for r in payload["records"]}
    return index, hashlib.sha256(raw).hexdigest()


def evidence_status():
    index, digest = price_evidence()
    return {"valuation_policy_version": VALUATION_POLICY_VERSION,
            "price_currency_evidence_version": digest,
            "price_currency_verified_records": len(index),
            "share_basis_evidence_version": share_evidence()[1],
            "share_basis_comparison_records": sum(map(len, share_evidence()[0].values()))}


def matching_price_evidence(row):
    if not row:
        return None
    record = price_evidence()[0].get((row["gvkey"], str(row["datadate"])))
    # Evidence is for an exact observation, not an inferred historical interval.
    if record and row.get("curcdq") == record["currency"] and (value_fingerprint(row.get("prccq"))==record.get("price_hash") if "price_hash" in record else row.get("prccq")==record.get("price")):
        return record
    return None


def share_basis_reason(row, policy="infer_compustat"):
    # Accepted research convention: EPS and shares are already ADS-equivalent.
    # ADRRQ is used for consistency checks, never to convert the values again.
    if is_adr(row):
        if policy == "verified_only":
            return "unverified_adr_share_basis"
        ratio = row.get("adrrq")
        if ratio is None or not math.isfinite(float(ratio)) or float(ratio) <= 0:
            return "missing_or_invalid_adr_ratio"
        return None
    if not row.get("priusa") and not row.get("prican"):
        return "unverified_security_share_basis"
    return None


def share_inputs_reason(rows, policy="infer_compustat"):
    reason = next((reason for r in rows if (reason := share_basis_reason(r, policy))), None)
    if reason:
        return reason
    if len({is_adr(r) for r in rows}) > 1:
        return "security_share_basis_changed_across_inputs"
    if rows and is_adr(rows[0]) and len({float(r["adrrq"]) for r in rows}) > 1:
        return "adr_ratio_changed_across_inputs"
    return None


def nonquarter_eps(row, field):
    # A matched disclosure establishes the observation's actual period,
    # including the alternative basic/diluted and extraordinary EPS choices.
    return field in {"epsfiq", "epspiq", "epsfxq", "epspxq"} and any(r["period_type"] == "half_year_EPS"
               for r in matching_share_evidence(row))


def mixed_share_classes(row):
    return any(r["field"] == "cshoq" and
               r["basis_status"] == "company_wide_multiclass_ADS_equivalent_not_actual_ADRs"
               for r in matching_share_evidence(row))
