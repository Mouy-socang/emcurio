from curio.product_v2 import extract, damage_statements, FIELDS

def item(title):
    return dict(item_id='test',platform='ebay',title=title,url='https://www.ebay.de/itm/test',first_seen='2026-10-06T10:00:00+00:00')

def test_negative_damage_lists_and_packaging():
    for text in ['Keine Chips, Risse oder nennenswerter Abrieb.', 'Keine Risse oder Beschädigungen des Porzellans, kein Goldabrieb.', 'Top Zustand, keine Abplatzungen, Risse o.ä.', 'ohne Beschädigungen wie bspw. Chips.', 'Die Farben sind an keiner Stelle beschädigt.', 'Alles unbeschädigt und neuwertig - nur der Originalkarton ist beschädigt.']:
        assert not damage_statements('',text),text
    assert [x[0] for x in damage_statements('','Keine Risse, aber am Rand ein Chip.')]==['磕口']
    assert '修复' in [x[0] for x in damage_statements('','Henkel links restauriert ansonsten keine Beschädigungen')]
    assert not damage_statements('','Keine Abplatzungen, Risse o.ä. Beschädigungen erkennbar')
    assert [x[0] for x in damage_statements('','Bogen müsste geklebt werden')]==['需修理']

def test_grade_and_unknown_are_not_invented():
    r=extract(item('Meissen Tasse 1. Wahl 12 cm'))
    p=r['candidate_product']
    assert len(p)==len(FIELDS)==75
    assert p['factory_grade']=='first' and p['condition_grade'] is None
    assert p['has_restoration'] is None and p['defect_types'] is None
    assert p['height_cm'] is None and p['diameter_cm'] is None
    assert '梅森一等' in r['title_preview_zh']
    assert r['product']['factory_grade']=='unknown'
    second=extract(item('Meissen Vase'),{'description':'2. Wahl, Henkel restauriert.'})
    assert second['title_preview_zh'].startswith('【修复】梅森二等')
    unknown=extract(item('Meissen Vase'))
    assert unknown['candidate_product']['factory_grade']=='unknown'
    assert '年代未知' not in unknown['title_preview_zh']
    mixed=extract(item('Meissen Tassen 1. Wahl'),{'description':'Eine Untertasse 2. Wahl.'})
    assert mixed['candidate_product']['factory_grade']=='unknown'
    assert '一／二等均有记载' in mixed['title_preview_zh']
    assert p['product_title'] is None and r['product']['maker'] is None

def test_explicit_and_ambiguous_components():
    explicit=extract(item('Meissen 2 Tassen + 2 Untertassen'))['candidate_product']
    assert explicit['physical_piece_count']==4
    vague=extract(item('Meissen 3-tlg. Kaffeegedeck'))['candidate_product']
    assert vague['sale_piece_count']==3 and vague['physical_piece_count'] is None and vague['components'] is None
    title=extract(item('Meissen Zwiebelmuster 6 Stück Untertassen'))['title_preview_zh']
    assert '蓝洋葱' in title and 'sahnekrug' not in title and '|' not in title

def test_occasion_year_not_production_date_and_brand_conflict():
    r=extract(item('Meissen Jubiläumsteller 200 Jahre 1710-1910'))
    assert r['candidate_product']['date_start_year'] is None
    r=extract(item('Meissen Teller'),{'aspects':[dict(name='Marke',value='Rosenthal')]})
    assert r['candidate_product']['maker'] is None and '品牌冲突' in r['title_preview_zh']

def test_gilded_rim_and_damage_consistency():
    p=extract(item('Meissen Teller Goldrand DEFEKT'))['candidate_product']
    assert p['has_gilded_rim'] and p['has_gilding']
    assert p['condition_grade'] is None and '破损' in p['defect_types']
    r=extract(item('Meissen Zuckerdose'),{'description':'Höhe ohne Deckel: ca 7 cm. Knaufgriff geklebt.'})
    assert '缺盖' not in r['warnings'] and r['candidate_product']['has_restoration'] is True
