"""Create first-run settings without overwriting any existing configuration."""
from pathlib import Path
import os
import secrets

root=Path(__file__).resolve().parents[1]
path=root/'.env'
try:
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
except FileExistsError:
    raise SystemExit('.env already exists; retained your settings.')
with os.fdopen(descriptor,'w') as stream:
    stream.write((root/'.env.example').read_text().replace('FINANCIAL_API_TOKEN=\n','FINANCIAL_API_TOKEN='+secrets.token_urlsafe(32)+'\n'))
print('Created .env with a random access token. Open .env locally to copy the token into the management page.')
