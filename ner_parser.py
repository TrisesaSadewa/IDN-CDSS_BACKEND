import re
try:
    import structured_drug_db
except ImportError:
    structured_drug_db = None

def parse_prescription_text(text):
    """
    High-Performance Parser using Regex and Hash Map Lookups.
    """
    if not text:
        return {"separate_drugs": [], "racikan": []}

    # Normalize delimiters
    normalized = text.replace('\n', ';').replace(',', ';')
    items = [x.strip() for x in normalized.split(';') if x.strip()]
    
    parsed_drugs = []
    
    for item in items:
        drug_data = _parse_item_fast(item)
        if drug_data:
            parsed_drugs.append(drug_data)
            
    return {"separate_drugs": parsed_drugs, "racikan": []}

def _parse_item_fast(item):
    """
    Splits a string like 'Amox 500mg 3x1' and identifies parts instantly.
    """
    parts = item.split()
    
    drug_name_parts = []
    dosage = ""
    frequency = ""
    
    # Pre-compile regex for speed
    freq_pattern = re.compile(r'(\d+x\d+|\d+-\d+-\d+|\d+\s*x)', re.IGNORECASE)
    dosage_pattern = re.compile(r'(\d+\s*(mg|g|ml|mcg|iu|%))', re.IGNORECASE)
    
    identified_drug_name = None

    # Single Pass Loop O(N)
    for part in parts:
        # 1. Check if this part is a Drug Name in our DB
        if structured_drug_db:
            # Check the single word
            match = structured_drug_db.find_drug_fast(part)
            if match:
                identified_drug_name = match
                continue
                
        # 2. Check Frequency
        if freq_pattern.search(part):
            frequency = part
            continue
            
        # 3. Check Dosage
        if dosage_pattern.search(part):
            dosage = part
            continue
        
        # 4. Accumulate potential name parts
        drug_name_parts.append(part)
    
    # Logic to assemble the final name
    if identified_drug_name:
        final_name = identified_drug_name
    elif drug_name_parts:
        final_name = " ".join(drug_name_parts)
    else:
        final_name = "Unknown"

    return {
        "drugName": final_name,
        "dosage": dosage,
        "frequency": frequency
    }
