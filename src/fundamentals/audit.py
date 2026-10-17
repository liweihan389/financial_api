from collections import Counter
import datetime as dt
import json
import resource
import sys
import time

from .metrics import METRICS
from .currency import evidence_status
from .service import FinancialService

def audit_coverage(root=None,generation=None,as_of=None,progress=print):
    service=FinancialService(root)
    status=service.get_data_status(generation)
    generation=status["database_generation"]
    as_of=as_of or max(r["report"]["date_max"] for r in status["imports"])
    start=time.monotonic()
    progress(f"Auditing metric coverage at {as_of} (all markets)...")
    query=dict(as_of=as_of,market="ALL",generation=generation,mode="historical_approx")
    first=service.screen_companies(**query,limit=1000)
    counts=Counter()
    reasons={m:Counter() for m in METRICS}
    estimated=0
    for offset in range(0,first["total"],1000):
        page=first if offset==0 else service.screen_companies(**query,limit=1000,offset=offset)
        for row in page["rows"]:
            estimated+=int(row["release_date_source"].startswith("estimated"))
            for metric in METRICS:
                if row[metric] is not None:
                    counts[metric]+=1
                else:
                    reasons[metric][row["unavailable_metrics"].get(metric,"unknown")]+=1
    rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform!="darwin":
        rss*=1024
    report={"database_generation":generation,"as_of":str(as_of),"mode":"historical_approx","market":"ALL","audited_at":dt.datetime.now(dt.timezone.utc).isoformat(),"universe_count":first["universe_count"],"eligible_companies":first["total"],"excluded_counts":first["excluded_counts"],"estimated_release_companies":estimated,"metric_available_counts":{m:counts[m] for m in sorted(METRICS)},"metric_unavailable_reasons":{m:dict(reasons[m]) for m in sorted(METRICS)},"duration_seconds":round(time.monotonic()-start,3),"process_peak_rss_bytes":rss,"warnings":first["warnings"]}
    report.update({"currency_policy":"infer_curcdq","share_policy":"infer_compustat",**evidence_status()})
    (service.store.root/f"coverage-{generation}.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
    progress(f"Coverage audit complete: {first['total']:,} eligible companies")
    return report
