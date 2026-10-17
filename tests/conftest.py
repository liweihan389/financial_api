"""Synthetic evidence only; no issuer financial observations are shipped."""
import pytest
@pytest.fixture
def synthetic_share_evidence():
    rows=[]
    for ticker,field,kind,basis in [('BHP','epsfiq','half_year_EPS','ADS_equivalent_supported'),('PBR','cshoq','quarter_shares','company_wide_multiclass_ADS_equivalent_not_actual_ADRs')]:
        rows.append(dict(gvkey='900001' if ticker=='BHP' else '900002',ticker=ticker,field=field,datadate='2024-12-31',source_row_hash='synthetic-'+ticker,source_batch_id='synthetic',source_row=1,local_currency='USD',local_value=2 if field=='epsfiq' else 100,adrrq=2,period_type=kind,basis_status=basis,numeric_status='matches_disclosed_precision'))
    return {(r['gvkey'],r['datadate'],r['source_row_hash']):[r] for r in rows},'synthetic'
