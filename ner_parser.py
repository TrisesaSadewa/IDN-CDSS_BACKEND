import re

# 1. OPTIONAL DATABASE IMPORT
try:
    import structured_drug_db
    DB_AVAILABLE = True
except ImportError:
    structured_drug_db = None
    DB_AVAILABLE = False

def parse_prescription_text(text):
    """
    Parses prescription text. 
    Supports:
    1. Colon Format: "Drug :Qty:Sig"
    2. Hash Format: "Drug #Qty#Sig" (NEW)
    3. Unstructured Fallback
    """
    if not text:
        return {"separate_drugs": [], "racikan": [], "equipment": []}

    # Normalize entry delimiters
    # The new format uses "|||" as a separator between drugs
    text = text.replace('|||', ';').replace('\n', ';')
    
    # Split into entries
    entries = [e.strip() for e in text.split(';') if e.strip()]
    
    parsed_data = {
        "separate_drugs": [],
        "racikan": [],
        "equipment": []
    }
    
    for entry in entries:
        # 1. Check for Equipment
        if DB_AVAILABLE:
            eq_name = structured_drug_db.find_equipment_match(entry)
            if eq_name:
                parsed_data["equipment"].append({"name": eq_name, "original": entry})
                continue

        # 2. Check for Racikan
        is_racikan = bool(re.search(r'\b(m\.?f\.?|racikan|puyer|dtd)\b', entry, re.IGNORECASE))
        if is_racikan:
            racikan_data = _parse_racikan_entry(entry)
            if racikan_data:
                parsed_data["racikan"].append(racikan_data)
            continue

        # 3. FAST PATH: Check for Structured Formats (: or #)
        if ":" in entry or "#" in entry:
            fast_drug = _parse_structured_format(entry)
            if fast_drug:
                parsed_data["separate_drugs"].append(fast_drug)
            continue

        # 4. FALLBACK: Unstructured
        fallback_drug = _parse_unstructured(entry)
        if fallback_drug:
             parsed_data["separate_drugs"].append(fallback_drug)

    return parsed_data

def _clean_drug_name(name):
    if not name: return ""
    # Remove common noise words/codes
    noise = r'\b(ANS|TAB|TABLET|KAPSUL|CAPSUL|CAPS|INJ|INJEKSI|SYR|DROPS|VIAL|AMPUL|SACHET|BTL|TUB|GR|GRAM|MG|ML|IU|MCG)\b'
    name = re.sub(noise, ' ', name, flags=re.IGNORECASE)
    name = re.sub(r'[*]+', '', name) 
    name = re.sub(r'^\d+\s+', '', name) 
    return " ".join(name.split())

def _parse_structured_format(entry):
    """
    Parses structured formats.
    Type A: "METRONIDAZOL 500 MG TAB :45.00:3 dd tab 1 pc"
    Type B: "ANS V-BLOC 6.25 MG TABLET #30.00#1-0-0"
    """
    # Determine delimiter
    delimiter = '#' if '#' in entry else ':'
    
    parts = entry.split(delimiter)
    
    # We expect at least 3 parts: [Name, Qty, Freq]
    if len(parts) < 2:
        return None
        
    raw_name_part = parts[0].strip()
    qty = parts[1].strip() if len(parts) > 1 else "0"
    freq = parts[2].strip() if len(parts) > 2 else ""

    # Extract Dosage from Name Part
    dosage = ""
    # Matches: 500 MG, 0,8 mg, 6.25 MG
    dosage_match = re.search(r'(\d+([.,]\d+)?\s*(?:MG|G|ML|IU|MCG|%))', raw_name_part, re.IGNORECASE)
    
    clean_name_source = raw_name_part
    if dosage_match:
        dosage = dosage_match.group(1)
        clean_name_source = raw_name_part.replace(dosage, "")

    final_name = _clean_drug_name(clean_name_source)
    
    # Try DB match to normalize name (e.g. "V-BLOC" -> "V-Bloc")
    if DB_AVAILABLE:
        db_match = structured_drug_db.find_drug_match(final_name)
        if db_match: final_name = db_match

    # Clean Qty
    try:
        if "." in qty: qty = str(int(float(qty)))
    except: pass

    return {
        "drugName": final_name,
        "dosage": dosage,
        "frequency": freq,
        "quantity": qty
    }

def _extract_ingredients(recipe_text):
    ingredients = []
    dose_pat = re.compile(
        r'((?:\d+\s*/\s*\d+|\d+(?:[.,]\d+)?)\s*(?:mg|g|ml|mcg|iu|%|tab|cap|tablet|kapsul|bungkus|sachet|amp|vial)?)', 
        re.IGNORECASE
    )
    
    parts = dose_pat.split(recipe_text)
    current_name = ""
    
    for i in range(0, len(parts)-1, 2):
        name_part = parts[i].strip()
        dose_part = parts[i+1].strip()
        
        if name_part: 
            cleaned = _clean_drug_name(name_part)
            if cleaned: current_name = cleaned
        
        if current_name:
            ingredients.append({"name": current_name, "strength": dose_part})
            current_name = ""
            
    return ingredients

def _parse_racikan_entry(entry):
    # Determine delimiter for racikan too
    delimiter = '#' if '#' in entry else ':'
    parts = entry.split(delimiter)
    
    full_recipe = parts[0].strip()
    split_match = re.search(r'\b(m\.?f\.?|racikan|puyer|dtd)\b', full_recipe, re.IGNORECASE)
    
    if split_match:
        ingredients_text = full_recipe[:split_match.start()].strip()
        compounding_instr = full_recipe[split_match.start():].strip()
    else:
        ingredients_text = full_recipe
        compounding_instr = ""

    ingredients_list = _extract_ingredients(ingredients_text)
    
    qty = parts[1].strip() if len(parts) > 1 else "1"
    freq = parts[2].strip() if len(parts) > 2 else "See instructions"

    try:
        if "." in qty: qty = str(int(float(qty)))
    except: pass

    return {
        "is_compound": True,
        "ingredients": ingredients_list,
        "recipe_text": full_recipe,
        "compounding_instruction": compounding_instr,
        "frequency": freq,
        "quantity": qty
    }

def _parse_unstructured(entry):
    parts = entry.split()
    if not parts: return None
    return {
        "drugName": _clean_drug_name(parts[0]),
        "dosage": "",
        "frequency": " ".join(parts[1:]) if len(parts)>1 else "",
        "quantity": "1"
    }
