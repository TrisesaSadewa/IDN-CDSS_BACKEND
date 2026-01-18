import re

# Safe Import
try:
    import structured_drug_db
except ImportError:
    structured_drug_db = None

def parse_prescription_text(text):
    """
    Parses prescription text into drugs, compounds, and equipment.
    Optimized for speed.
    """
    if not text:
        return {"separate_drugs": [], "racikan": [], "equipment": []}

    # Normalize delimiters
    normalized = text.replace('\n', ';').replace(',', ';')
    items = [x.strip() for x in normalized.split(';') if x.strip()]
    
    parsed_data = {
        "separate_drugs": [],
        "racikan": [],
        "equipment": []
    }
    
    for item in items:
        # 1. Check for Equipment first (if DB available)
        if structured_drug_db:
            eq_name = structured_drug_db.find_equipment_match(item)
            if eq_name:
                parsed_data["equipment"].append({"name": eq_name, "original": item})
                continue

        # 2. Check for Racikan (Compound) keywords
        if "mf" in item.lower() or "racikan" in item.lower():
            parsed_data["racikan"].append(parse_racikan(item))
            continue
            
        # 3. Parse as Drug
        drug_data = _parse_drug_item(item)
        if drug_data:
            parsed_data["separate_drugs"].append(drug_data)
            
    return parsed_data

def _parse_drug_item(item):
    # Try to identify Drug Name via DB (Fast Lookup)
    drug_name = None
    if structured_drug_db:
        drug_name = structured_drug_db.find_drug_fast(item)
    
    # Heuristic Parsing for Dosage/Freq
    parts = item.split()
    dosage = ""
    frequency = ""
    
    freq_pattern = re.compile(r'(\d+x\d+|\d+-\d+-\d+|\d+\s*x)', re.IGNORECASE)
    dosage_pattern = re.compile(r'(\d+\s*(mg|g|ml|mcg|iu|%))', re.IGNORECASE)
    
    name_parts = []
    
    for part in parts:
        if freq_pattern.search(part):
            frequency = part
        elif dosage_pattern.search(part):
            dosage = part
        elif not drug_name:
            name_parts.append(part)
            
    if not drug_name and name_parts:
        drug_name = " ".join(name_parts)
    elif not drug_name:
        drug_name = "Unknown"

    return {
        "drugName": drug_name,
        "dosage": dosage,
        "frequency": frequency
    }

def parse_racikan(text):
    """
    Simple parser for compound drugs.
    """
    return {
        "components": text,
        "frequency": "See instructions",
        "quantity": 1
    }
