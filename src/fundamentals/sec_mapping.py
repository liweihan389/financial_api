"""Explicit, versioned standard XBRL rules. Amounts/shares use Compustat millions.
No name similarity, no automatic exchange conversion, no quarterly EPS subtraction.
"""
import hashlib
import json

VERSION = 'sec-standard-1'
CUTOFF = '2026-02-28'
from pathlib import Path
POLICY=json.loads(Path(__file__).with_name('sec_policy.json').read_text())
RULES = json.loads(Path(__file__).with_name('sec_rules.json').read_text())
for field,entry in RULES.items():
    if entry['quality'] not in {'trusted','inferred'} or entry['unit'] not in {'money','eps','shares'} or entry['kind'] not in {'instant','duration'}:
        raise ValueError('invalid_sec_mapping_config')
    if any(tag.split(':')[0] not in {'us-gaap','ifrs-full','dei','srt'} for tag in entry['tags']):
        raise ValueError('custom_sec_tags_not_supported')


def rules_version(rules=None):
    content=json.dumps({'rules':rules or RULES,'policy':POLICY},sort_keys=True,separators=(',',':'))
    return VERSION+'-'+hashlib.sha256(content.encode()).hexdigest()[:16]


def registry(dictionary):
    out=[]
    for name,definition in dictionary.items():
        r=RULES.get(name)
        status='trusted_mapping' if r and r['quality']=='trusted' else 'rule_inferred' if r else 'not_applicable' if name in {'prccq','prchq','prclq','ajexq','adjex','adrrq'} else 'pending_review'
        out.append(dict(field=name,status=status,definition=definition,rule=r,
                        reason=None if r else 'supplier_price_or_adjustment_not_a_sec_fact' if status=='not_applicable' else 'no_validated_standard_mapping_in_this_ruleset'))
    return out
