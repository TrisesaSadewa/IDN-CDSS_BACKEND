import re

# 1. OPTIONAL DATABASE IMPORT
# We make this optional so the parser works even if the DB file is missing or slow.
try:
    import structured_drug_db
    DB_AVAILABLE = True
except ImportError:
    structured_drug_db = None
    DB_AVAILABLE = False

def parse_prescription_text(text):
    """
    Parses prescription text. 
    PRIORITY: Checks for 'Name :Qty:Sig' format first (Instant).
    FALLBACK: Uses simple heuristics if format is unstructured.
    """
    if not text:
        return {"separate_drugs": [], "racikan": [], "equipment": []}

    # Normalize delimiters (newlines to semicolons)
    text = text.replace('\n', ';')
    
    # Split into entries
    entries = [e.strip() for e in text.split(';') if e.strip()]
    
    parsed_data = {
        "separate_drugs": [],
        "racikan": [],
        "equipment": []
    }
    
    for entry in entries:
        # 1. FAST PATH: Check for Colon Format (e.g., "Drug :Qty:Sig")
        # This is O(1) and skips the heavy database lookups
        if ":" in entry:
            fast_drug = _parse_fast_colon_format(entry)
            if fast_drug:
                parsed_data["separate_drugs"].append(fast_drug)
                continue

        # 2. SLOW PATH: Fallback for unstructured text
        # (Only runs if the line doesn't match the fast format)
        fallback_drug = _parse_unstructured(entry)
        if fallback_drug:
             parsed_data["separate_drugs"].append(fallback_drug)

    return parsed_data

def _parse_fast_colon_format(entry):
    """
    Parses formats like: 'METRONIDAZOL 500 MG TAB :45.00:3 dd tab 1 pc'
    Returns structured drug object or None.
    """
    parts = entry.split(':')
    
    # We expect at least 3 parts: [Name+Dose, Qty, Freq]
    # Sometimes Qty might be missing or format varies slightly, handle gracefully
    if len(parts) < 2:
        return None
        
    # Part 0: Name + Dosage + Form ("METRONIDAZOL 500 MG TAB ")
    drug_part = parts[0].strip()
    
    # Part 1: Quantity ("45.00")
    qty_part = parts[1].strip() if len(parts) > 1 else ""
    
    # Part 2: Frequency/Sig ("3 dd tab 1 pc")
    freq_part = parts[2].strip() if len(parts) > 2 else ""
    
    # Extract Dosage from drug_part (e.g. 500 MG) using Regex
    # Looks for number followed by unit
    dosage_match = re.search(r'(\d+\s*(?:MG|G|ML|IU|MCG|%))', drug_part, re.IGNORECASE)
    
    if dosage_match:
        dosage = dosage_match.group(1)
        # Name is typically everything before the dosage
        # e.g. "METRONIDAZOL " from "METRONIDAZOL 500 MG TAB"
        name = drug_part[:dosage_match.start()].strip()
    else:
        dosage = ""
        name = drug_part
        
    # Clean up Qty (remove .00)
    try:
        qty_float = float(qty_part)
        qty = int(qty_float) if qty_float.is_integer() else qty_float
    except ValueError:
        qty = qty_part

    return {
        "drugName": name,
        "dosage": dosage,
        "quantity": str(qty),
        "frequency": freq_part,
        "original": entry
    }

def _parse_unstructured(entry):
    """
    Fallback for text without colons.
    Simple heuristic to avoid slow fuzzy matching.
    """
    # 1. Check for Equipment (if DB available)
    if DB_AVAILABLE:
        eq_name = structured_drug_db.find_equipment_match(entry)
        if eq_name:
            return {"name": eq_name, "type": "equipment", "original": entry}

    # 2. Simple Splitter
    parts = entry.split()
    if not parts: return None
    
    # Guessing: First word is name, rest is details
    return {
        "drugName": parts[0],
        "dosage": " ".join(parts[1:]) if len(parts)>1 else "",
        "frequency": "",
        "original": entry
    }
