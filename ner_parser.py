import re

# 1. Safe Import for Structured DB
# If structured_drug_db fails (e.g. Memory Error on small instances), we don't want to crash.
try:
    from structured_drug_db import get_drug_by_name, get_equipment_by_name, EQUIPMENT
except ImportError:
    print("WARNING: Could not import structured_drug_db. NER will lack DB features.")
    get_drug_by_name = lambda x: None
    get_equipment_by_name = lambda x: None
    EQUIPMENT = []

# 2. Safe Import for FuzzyWuzzy
# If this library is missing, we disable fuzzy matching instead of crashing.
try:
    from fuzzywuzzy import process
except ImportError:
    print("WARNING: fuzzywuzzy not installed. Fuzzy matching disabled.")
    process = None

def _fuzzy_match_name(name, db_dict, threshold=80):
    """
    Finds the best fuzzy match for a name in a given database dictionary.
    Returns the key of the best match if the score is above the threshold, otherwise returns None.
    """
    if not name or not db_dict or not process:
        return None
    
    # Process the name against all keys in the dictionary
    best_match = process.extractOne(name, db_dict.keys())
    
    if best_match and best_match[1] >= threshold:
        return best_match[0]
    return None

def _format_contents(contents):
    """Formats the contents from the database correctly."""
    if isinstance(contents, list):
        return ', '.join(contents)
    if isinstance(contents, str):
        return contents 
    return 'N/A'

def _map_form(form_text):
    """Maps common form abbreviations to full names."""
    if form_text:
        form_text = form_text.lower()
        if form_text in ['tab', 'tablet']: return 'Tablet'
        if form_text in ['capsul', 'cap']: return 'Capsule'
        if form_text in ['syr', 'syrup']: return 'Syrup'
        if form_text in ['inj', 'injeksi']: return 'Injection'
        if form_text in ['inf', 'infus']: return 'Infusion'
    return form_text

def _parse_equipment(entry):
    """Parses an entry to see if it matches known medical equipment."""
    potential_name = entry.split(':')[0].strip()
    equipment_match = get_equipment_by_name(potential_name)
    
    if equipment_match:
        qty_match = re.search(r':([\d\.]+):', entry)
        quantity = qty_match.group(1) if qty_match else "1"
        
        specs = "N/A"
        specs_part = entry.replace(equipment_match.name, '').replace(f":{quantity}:", '').strip()
        if specs_part:
            specs = specs_part.strip('; ')

        return {
            "equipmentName": equipment_match.name,
            "type": equipment_match.type,
            "quantity": float(quantity),
            "specs": specs
        }
    return None

def _parse_separate_drug(entry):
    """Parses a single drug entry string."""
    drug_data = {
        "drugName": "N/A",
        "dosage": "N/A",
        "quantity": "N/A",
        "frequency": "N/A",
        "instructions": "N/A"
    }

    # Extract Quantity
    qty_match = re.search(r':([\d\.]+):', entry)
    if qty_match:
        drug_data['quantity'] = float(qty_match.group(1))
        entry = entry.replace(qty_match.group(0), '')

    # Extract Frequency
    freq_match = re.search(r'(\d+\s*dd\s*[a-zA-Z0-9\.]*)|(\d+\s*x\s*\d+)|(\d+-\d+-\d+)', entry, re.IGNORECASE)
    if freq_match:
        drug_data['frequency'] = freq_match.group(0)
        entry = entry.replace(freq_match.group(0), '') 

    # Extract Drug Name & Dosage
    parts = entry.split()
    best_drug = None
    best_len = 0
    
    # Try first few words
    for i in range(1, min(len(parts) + 1, 6)): 
        candidate_name = " ".join(parts[:i])
        match = get_drug_by_name(candidate_name)
        if match:
            if len(candidate_name) > best_len:
                best_drug = match
                best_len = len(candidate_name)
    
    if best_drug:
        drug_data['drugName'] = best_drug.name
        drug_data['dosage'] = _format_contents(best_drug.contents)
    else:
        drug_data['drugName'] = " ".join(parts[:2])

    return drug_data

def _parse_racikan(entry):
    """Parses a racikan entry (compound drug)."""
    if "mf" not in entry.lower() and "racikan" not in entry.lower():
        return None
    return [{
        "drugName": "Racikan (Compound)",
        "components": entry, 
        "quantity": 1,
        "frequency": "See instructions"
    }]

def parse_prescription_text(prescription_text):
    """Main function to parse a full prescription string."""
    parsed_data = {
        'separate_drugs': [],
        'racikan': [],
        'equipment': []
    }

    if not prescription_text:
        return parsed_data

    entries = [e.strip() for e in prescription_text.split(';') if e.strip()]

    for entry in entries:
        equip = _parse_equipment(entry)
        if equip:
            parsed_data['equipment'].append(equip)
            continue
        
        racikan = _parse_racikan(entry)
        if racikan:
            parsed_data['racikan'].extend(racikan)
            continue

        drug = _parse_separate_drug(entry)
        if drug:
            parsed_data['separate_drugs'].append(drug)

    return parsed_data
