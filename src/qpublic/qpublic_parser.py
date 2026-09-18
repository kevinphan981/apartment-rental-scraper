from bs4 import BeautifulSoup
import re
import json
 
 
def _clean(s):
    if s is None:
        return None
    s = s.strip()
    return s if s else None
 
 
def _get_text(soup, elem_id):
    el = soup.find(id=elem_id)
    return _clean(el.get_text(strip=True)) if el else None
 
 
def parse_parcel_info(soup):
    """Parcel Number, Location Address, Project Name, Legal Info, Property Class (current)."""
    result = {}
    summary = soup.find(id='ctlBodyPane_ctl00_ctl01_dynamicSummary_divSummary')
    if not summary:
        return result
 
    rows = summary.find_all('tr')
    for row in rows:
        label_el = row.find('strong')
        value_el = row.find('td', style=lambda v: v and 'text-align: left' in v)
        if not label_el or not value_el:
            continue
        label = _clean(label_el.get_text(strip=True))
        # value cell may contain nested <span>/<br>/<a> -- take the first span's direct text
        span = value_el.find('span')
        if span:
            # Strip out any nested <a>/<b> boilerplate (e.g. Property Class DPP link text)
            value = _clean(span.get_text(separator=' ', strip=True))
        else:
            value = _clean(value_el.get_text(separator=' ', strip=True))
        if label:
            result[label] = value
 
    # "Property Class" value includes trailing boilerplate ("Property Class refers to...
    # DPP link text"); trim it back to just the class name.
    if result.get('Property Class'):
        result['Property Class'] = result['Property Class'].split(
            'Property Class refers to'
        )[0].strip()
 
    return result
 
 
def _money_to_float(s):
    if s is None:
        return None
    s = s.strip().replace('$', '').replace(',', '')
    neg = s.startswith('(') and s.endswith(')')
    s = s.strip('()')
    if s == '' or s == '-':
        return 0.0
    try:
        val = float(s)
    except ValueError:
        return None
    return -val if neg else val
 
 
def parse_assessment_history(soup):
    """
    Returns list of dicts, one per tax year, with property class (tax status)
    and dedicated use value (dedications), from both the 'current' and
    'historical' valuation tables (deduped by year, current table's row wins).
    """
    years = {}
 
    def parse_table(table_id):
        table = soup.find(id=table_id)
        if not table:
            return
        rows = table.find_all('tr')
        if not rows:
            return
        header_cells = [c.get_text(strip=True) for c in rows[0].find_all(['th', 'td'])]
        for row in rows[1:]:
            cells = [c.get_text(strip=True) for c in row.find_all('td')]
            if len(cells) < 4:
                continue
            data = dict(zip(header_cells, cells))
            year = data.get('Tax Year')
            if not year:
                continue
            years[year] = {
                'tax_year': year,
                'property_class': _clean(data.get('Property Class')),
                'dedicated_use_value': _money_to_float(data.get('DedicatedUse Value')
                                                        or data.get('Dedicated Use Value')),
                'assessed_land_value': _money_to_float(data.get('AssessedLandValue')
                                                        or data.get('Assessed Land Value')),
                'assessed_building_value': _money_to_float(data.get('AssessedBuildingValue')
                                                            or data.get('Assessed Building Value')),
                'total_assessed_value': _money_to_float(data.get('TotalProperty AssessedValue')
                                                         or data.get('Total Property Assessed Value')),
                'total_exemption': _money_to_float(data.get('TotalProperty Exemption')
                                                    or data.get('Total Property Exemption')),
                'total_net_taxable_value': _money_to_float(data.get('Total NetTaxableValue')
                                                            or data.get('Total Net Taxable Value')),
            }
 
    # Current year table first, then historical (won't overwrite current year's entry
    # since dict key by year and historical table also includes the current year row
    # with identical data -- fine either way)
    parse_table('ctlBodyPane_ctl03_ctl01_gvValuation')
    parse_table('ctlBodyPane_ctl03_ctl01_gvValuationHistorical')
 
    # Sort by year descending
    ordered = sorted(years.values(), key=lambda d: d['tax_year'], reverse=True)
 
    # Determine years where property class ("tax status") changed vs the prior (older) year
    for i in range(len(ordered) - 1):
        cur_class = ordered[i]['property_class']
        prev_class = ordered[i + 1]['property_class']
        ordered[i]['property_class_changed_from_prior_year'] = (
            cur_class != prev_class if cur_class and prev_class else None
        )
    if ordered:
        ordered[-1]['property_class_changed_from_prior_year'] = None  # no prior data available
 
    return ordered
 
 
def parse_condo_units(soup):
    """
    Parses the 'Condominium/Apartment Unit Information' grid that appears on a
    Condo Master's own qpublic report page (e.g. KeyValue=260210260000). Each
    row is one individual condo/apartment unit -- its own parcel number (TMK)
    and unit number. This is the master-to-units index. Returns [] for a
    non-master parcel (the grid/module simply isn't on the page).
    """
    table = soup.find(id='ctlBodyPane_ctl02_ctl01_gvwCondos')
    if not table:
        return []
    tbody = table.find('tbody', recursive=False) or table
    rows = tbody.find_all('tr', recursive=False)

    units = []
    for row in rows:
        cells = row.find_all('td', recursive=False)
        if len(cells) < 2:
            continue
        link = cells[0].find('a')
        parcel_number = _clean(link.get_text(strip=True)) if link else _clean(cells[0].get_text(strip=True))
        if not parcel_number:
            continue
        units.append({
            'parcel_number': parcel_number,
            'unit_number': _clean(cells[1].get_text(strip=True)) if len(cells) > 1 else None,
        })
    return units


def is_condo_master(soup):
    """
    True if this parcel's own report page identifies itself as a Condo Master
    (the notice appears in the Parcel Number summary module). False/None if
    the page is a normal parcel or the notice text isn't found.
    """
    return bool(soup.find(string=re.compile(r'This is a Condo Master')))


def parse_dedications_notice(soup):
    """
    Detects the page's own notice about modules with no data (e.g. Dedications,
    Appeal Information, etc.) -- useful to positively confirm 'no dedication'
    rather than just an empty section.
    """
    text_node = soup.find(string=re.compile(r'No data available for the following modules'))
    if not text_node:
        return {'no_data_modules': [], 'dedications_has_no_data': None}
    # The module list itself lives in the next text node in the DOM (a sibling of the
    # <strong>...:</strong> label, not inside it), so walk forward to find it.
    list_text = text_node.find_next(string=True)
    modules = []
    if list_text:
        cleaned = list_text.strip().rstrip('.')
        modules = [m.strip() for m in cleaned.split(',') if m.strip()]
    return {
        'no_data_modules': modules,
        'dedications_has_no_data': any('Dedication' in m for m in modules),
    }
 
 
def parse_historical_tax(soup, include_period_detail=False):
    """
    Parses the Historical Tax Information section:
      - per-year summary totals (tax, payments/credits, penalty, interest, other, amount due)
      - per-year payment records (payment sequence, effective date, amounts) -- these are
        the actual real-world payment transactions
      - OPTIONAL per-tax-period accrual-ledger line items (beginning tax, beginning WBID,
        payment, payment WBID, ...). This is internal bookkeeping detail that nets out to
        the same totals already in the year summary and the payments list -- it roughly
        6x's the output size per year, so it's left out by default. Pass
        include_period_detail=True if you specifically need it.
    Returns a list of dicts, one per year, newest first.
    """
    main_table = soup.find(id='ctlBodyPane_ctl19_ctl01_gvwHistoricalTax')
    if not main_table:
        return []
 
    tbody = main_table.find('tbody', recursive=False) or main_table
    top_rows = tbody.find_all('tr', recursive=False)
 
    years_out = []
    i = 0
    header_row_cells = None
    while i < len(top_rows):
        row = top_rows[i]
        cells = row.find_all('td', recursive=False)
        cell_texts = [c.get_text(strip=True) for c in cells]
 
        # Skip header-ish or empty rows
        if not cell_texts or not cell_texts[0] or not re.match(r'^\d{4}$', cell_texts[0]):
            i += 1
            continue
 
        year = cell_texts[0]
        summary = {
            'tax_year': year,
            'tax': _money_to_float(cell_texts[1]) if len(cell_texts) > 1 else None,
            'payments_and_credits': _money_to_float(cell_texts[2]) if len(cell_texts) > 2 else None,
            'penalty': _money_to_float(cell_texts[3]) if len(cell_texts) > 3 else None,
            'interest': _money_to_float(cell_texts[4]) if len(cell_texts) > 4 else None,
            'other': _money_to_float(cell_texts[5]) if len(cell_texts) > 5 else None,
            'amount_due': _money_to_float(cell_texts[6]) if len(cell_texts) > 6 else None,
            'payments': [],
        }
        if include_period_detail:
            summary['tax_period_detail'] = []
 
        # Next row (if present) holds the nested detail/payments tables
        if i + 1 < len(top_rows):
            nested_row = top_rows[i + 1]
            nested_tables = nested_row.find_all('table')
            for nt in nested_tables:
                nt_id = nt.get('id', '')
                nt_rows = nt.find_all('tr')
                if not nt_rows:
                    continue
                nt_header = [c.get_text(strip=True) for c in nt_rows[0].find_all(['th', 'td'])]
 
                if nt_id.endswith('_Detail'):
                    if not include_period_detail:
                        continue
                    for r in nt_rows[1:]:
                        rc = [c.get_text(strip=True) for c in r.find_all('td')]
                        if not rc or not rc[0]:
                            continue
                        d = dict(zip(nt_header, rc))
                        if d.get('Tax Period', '').lower().startswith('totals'):
                            continue
                        summary['tax_period_detail'].append({
                            'tax_period': _clean(d.get('Tax Period')),
                            'description': _clean(d.get('Description')),
                            'tax': _money_to_float(d.get('Tax')),
                            'payments_credits': _money_to_float(d.get('Payments/Credits')),
                            'penalty': _money_to_float(d.get('Penalty')),
                            'interest': _money_to_float(d.get('Interest')),
                            'other': _money_to_float(d.get('Other')),
                        })
                elif nt_id.endswith('_Payments'):
                    for r in nt_rows[1:]:
                        rc = [c.get_text(strip=True) for c in r.find_all('td')]
                        if not rc or not rc[0] or rc[0].lower().startswith('totals'):
                            continue
                        d = dict(zip(nt_header, rc))
                        summary['payments'].append({
                            'payment_sequence': _clean(d.get('Payment Sequence')),
                            'effective_date': _clean(d.get('Effective Date')),
                            'tax': _money_to_float(d.get('Tax')),
                            'penalty': _money_to_float(d.get('Penalty')),
                            'interest': _money_to_float(d.get('Interest')),
                            'other': _money_to_float(d.get('Other')),
                        })
            i += 2
        else:
            i += 1
 
        years_out.append(summary)
 
    return years_out
 
 
def parse_qpublic_html(html: str, include_period_detail: bool = False) -> dict:
    """
    include_period_detail=False (default) omits the per-tax-period accrual ledger
    (Beginning Tax / WBID / Payment line items) that bloats output ~4-6x per year
    without adding information beyond the year summary + payments list. Set True
    only if you specifically need that ledger-level breakdown.
    """
    soup = BeautifulSoup(html, 'lxml')
 
    parcel_info = parse_parcel_info(soup)
    dedications_notice = parse_dedications_notice(soup)
 
    record = {
        'parcel_number': parcel_info.get('Parcel Number'),
        'location_address': parcel_info.get('Location Address'),
        'project_name': parcel_info.get('Project Name'),
        'legal_information': parcel_info.get('Legal Information'),
        'current_property_class': parcel_info.get('Property Class'),
        'tax_status_by_year': parse_assessment_history(soup),
        'dedications_notice': dedications_notice,
        'historical_tax': parse_historical_tax(soup, include_period_detail=include_period_detail),
    }
    return record
 
 
if __name__ == '__main__':
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else '/mnt/user-data/uploads/qpublic-unit.html'
    with open(path, 'r', errors='ignore') as f:
        html = f.read()
    record = parse_qpublic_html(html)
    print(json.dumps(record, indent=2))