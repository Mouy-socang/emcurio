"""Phase-one configured total: auction bid + fee; shipping/tax unknown."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

def money(value):
    if value is None or value=='':return None
    try:d=Decimal(str(value))
    except InvalidOperation:raise ValueError('价格无效') from None
    if not d.is_finite() or d<0:raise ValueError('价格无效')
    return float(d.quantize(Decimal('.01'),rounding=ROUND_HALF_UP))

def prices(item,fee_rate):
    listed=money(item.get('price'))
    if listed is None:return None,None
    auction=item.get('platform')=='ebay' and 'AUCTION' in item.get('buying','')
    rate=Decimal(str(fee_rate)) if auction else Decimal(0)
    return listed,money(Decimal(str(listed))*(Decimal(1)+rate))
