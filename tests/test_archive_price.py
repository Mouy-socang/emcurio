from unittest.mock import patch,Mock
from curio.archive_price import archive_price
XML=b'<root><Cube time="2026-10-14"><Cube currency="CNY" rate="7.8"/><Cube currency="USD" rate="1.2"/></Cube></root>'
def test_snapshot_rate_preserves_original_and_actual_date(tmp_path):
 with patch('curio.archive_price.requests.get',return_value=Mock(content=XML)) as call:
  result=archive_price(100,'EUR',tmp_path,'2026-10-15')
  assert result['original_amount']==100 and result['amount_cny']=='780.00'
  assert result['rate_date']=='2026-10-14' and result['status']=='converted_latest_published'
  assert archive_price(120,'USD',tmp_path,'2026-10-15')['amount_cny']=='780.00'
  assert call.call_count==1

def test_missing_rate_does_not_guess(tmp_path):
 with patch('curio.archive_price.requests.get',return_value=Mock(content=XML)):
  assert archive_price(100,'XXX',tmp_path,'2026-10-15')['amount_cny'] is None
  assert archive_price(None,'EUR',tmp_path,'2026-10-15')['status']=='missing_price'
