"""Fail if tracked publication includes runtime datasets or contact credentials."""
from pathlib import Path
import json
import subprocess

root=Path(__file__).resolve().parents[1]
tracked=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard'],cwd=root,text=True).splitlines()
for name in tracked:
    path=Path(name)
    if any(part in {'data','storage','reports','incoming','exports','.venv','__pycache__'} for part in path.parts):raise SystemExit('Dataset/runtime directory included: '+name)
    if path.suffix.lower() in {'.duckdb','.sqlite','.sqlite3','.parquet','.pdf','.zip','.tar','.gz'}:raise SystemExit('Dataset/artifact included: '+name)
    if path.suffix=='.csv' and not path.name.endswith('.template.csv'):raise SystemExit('Non-template CSV included: '+name)
    if path.name in {'.env','settings.json'}:raise SystemExit('Runtime configuration included: '+name)
base=root/'src/fundamentals'
for filename,forbidden in [('price_currency_evidence.json',{'price'}),('share_basis_evidence.json',{'local_value','official_native_value','comparison_value','source_row','source_batch_id','adrrq','currtrq'})]:
    document=json.loads((base/filename).read_text())
    assert all(not forbidden.intersection(r) for r in document['records'])
assert len(json.loads((base/'compustat_schema.json').read_text()))==679
assert not json.loads((base/'sec_policy.json').read_text())['companies']
print('Publication checked: code/schema/fingerprints only; no datasets or runtime secrets.')
