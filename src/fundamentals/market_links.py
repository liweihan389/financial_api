"""Audited company/security identity overrides; never bypass per-share checks."""
import csv
import datetime as dt
import io
import json
import re
import uuid
from .market_store import MarketStore,source_csv,digest_file,atomic_json
from .store import Store

def import_links(source,root=None):
    store=MarketStore(root);old=store.manifest()
    if not old:raise ValueError('import_market_before_identity_links')
    rows=[]
    with source_csv(source) as (stream,member):
        reader=csv.DictReader(io.TextIOWrapper(stream,encoding='utf-8-sig'))
        if not {'gvkey','permno','valid_from','valid_to','evidence'}.issubset(reader.fieldnames or []):raise ValueError('identity_link_fields_required')
        for line,row in enumerate(reader,2):
            if not re.fullmatch(r'\d+',row['gvkey']) or not re.fullmatch(r'\d+',row['permno']):raise ValueError('invalid_link_identifier')
            start=dt.date.fromisoformat(row['valid_from']);end=dt.date.fromisoformat(row['valid_to'])
            if start>end or not row['evidence'].strip():raise ValueError('invalid_link_period_or_evidence')
            rows.append({k:row[k] for k in ['gvkey','permno','valid_from','valid_to','evidence']})
            if len(rows)>100000:raise ValueError('identity_link_row_limit')
    if not rows:raise ValueError('empty_identity_links')
    ordered=sorted(rows,key=lambda r:(r['permno'],r['valid_from']))
    for a,b in zip(ordered,ordered[1:]):
        if a['permno']==b['permno'] and a['valid_to']>=b['valid_from']:raise ValueError('overlapping_security_identity_links')
    with Store(root).connect() as c:
        known={r[0] for r in c.execute('SELECT DISTINCT gvkey FROM metric_inputs').fetchall()}
    if any(r['gvkey'] not in known for r in rows):raise ValueError('unknown_link_company')
    # Serialize pointer mutations with the same lock as market imports.
    import os
    lock=store.root/'import.lock'
    try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:raise ValueError('market_import_already_running')
    os.close(fd)
    try:
        old=store.manifest();fingerprint=digest_file(source)
        if old.get('manual_links_sha256')==fingerprint:return {**old,'already_imported':True}
        folder=store.root/'links';folder.mkdir(exist_ok=True)
        atomic_json(folder/f'{fingerprint}.json',{'rows':rows,'source_name':str(source),'sha256':fingerprint})
        new={**old,'generation':uuid.uuid4().hex,'manual_links_sha256':fingerprint,'manual_link_count':len(rows)}
        atomic_json(store.root/'generations'/f"{new['generation']}.json",new)
        atomic_json(store.root/'active.json',{'generation':new['generation']})
        return new
    finally:lock.unlink(missing_ok=True)

def matching_links(store,manifest,permno,day):
    fingerprint=manifest.get('manual_links_sha256')
    if not fingerprint:return []
    payload=json.loads((store.root/'links'/f'{fingerprint}.json').read_text())
    return [r for r in payload['rows'] if r['permno']==permno and r['valid_from']<=str(day)<=r['valid_to']]
