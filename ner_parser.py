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
    Parses prescription text into structured data.
    Handles Single Drugs, Racikan (Compounds), and Equipment.
    """
    if not text:
        return {"separate_drugs": [], "racikan": [], "equipment": []}

    # Normalize delimiters: Newlines -> Semicolons
    text = text.replace('\n', ';')
    
    # Split into individual entries
    entries = [e.strip() for e in text.split(';') if e.strip()]
    
    parsed_data = {
        "separate_drugs": [],
        "racikan": [],
        "equipment": []
    }
    
    for entry in entries:
        # 1. Check for Equipment (using DB if available)
        if DB_AVAILABLE:
            eq_name = structured_drug_db.find_equipment_match(entry)
            if eq_name:
                parsed_data["equipment"].append({"name": eq_name, "original": entry})
                continue

        # 2. Check for Racikan/Compound Keywords (m.f., dtd, racikan)
        is_racikan = bool(re.search(r'\b(m\.?f\.?|racikan|puyer|dtd)\b', entry, re.IGNORECASE))

        if is_racikan:
            racikan_data = _parse_racikan_entry(entry)
            if racikan_data:
                parsed_data["racikan"].append(racikan_data)
            continue

        # 3. FAST PATH: Colon Format (Name : Qty : Sig)
        if ":" in entry:
            fast_drug = _parse_colon_drug(entry)
            if fast_drug:
                parsed_data["separate_drugs"].append(fast_drug)
            continue

        # 4. FALLBACK: Unstructured
        fallback_drug = _parse_unstructured(entry)
        if fallback_drug:
             parsed_data["separate_drugs"].append(fallback_drug)

    return parsed_data

def _clean_drug_name(name):
    """
    Aggressively cleans drug names by removing forms, codes, and noise.
    """
    if not name: return ""
    
    # 1. Remove specific words (Case Insensitive)
    noise_pattern = r'\b(ANS|TAB|TABLET|KAPSUL|CAPSUL|CAPS|INJ|INJEKSI|SYR|DROPS|VIAL|AMPUL|SACHET|BTL|TUB|GR|GRAM|MG|ML|IU|MCG)\b'
    name = re.sub(noise_pattern, ' ', name, flags=re.IGNORECASE)
    
    # 2. Remove symbols (*, -, numbers at start)
    name = re.sub(r'[*]+', '', name) 
    name = re.sub(r'^\d+\s+', '', name) 
    
    return " ".join(name.split())

def _parse_colon_drug(entry):
    parts = entry.split(':')
    raw_name_part = parts[0].strip()
    qty = parts[1].strip() if len(parts) > 1 else "0"
    freq = parts[2].strip() if len(parts) > 2 else ""

    dosage = ""
    dosage_match = re.search(r'(\d+([.,]\d+)?\s*(?:MG|G|ML|IU|MCG|%))', raw_name_part, re.IGNORECASE)
    
    clean_name_source = raw_name_part
    if dosage_match:
        dosage = dosage_match.group(1)
        clean_name_source = raw_name_part.replace(dosage, "")

    final_name = _clean_drug_name(clean_name_source)

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
    """
    Extracts individual ingredients from a racikan string.
    Example: "Tremenza 1/5 tablet Lasal 0,8mg"
    Returns: [{'name': 'Tremenza', 'dose': '1/5 tablet'}, {'name': 'Lasal', 'dose': '0,8mg'}]
    """
    ingredients = []
    
    # Regex to find Dosage/Amount patterns
    # Matches: fractions (1/5), decimals (0,8), integers (10) followed by optional units/forms
    # Group 1 captures the whole dosage string
    dosage_pattern = re.compile(
        r'((?:\d+\s*/\s*\d+|\d+(?:[.,]\d+)?)\s*(?:mg|g|ml|mcg|iu|%|tab|cap|tablet|kapsul|bungkus|sachet)?)', 
        re.IGNORECASE
    )
    
    # Split text by these dosage patterns
    # re.split with capturing group returns: [Name, Dosage, Name, Dosage...]
    parts = dosage_pattern.split(recipe_text)
    
    current_name = ""
    
    for i in range(0, len(parts) - 1, 2):
        name_part = parts[i].strip()
        dosage_part = parts[i+1].strip()
        
        # If this is the first item or previous loop set the name
        if name_part:
            current_name = _clean_drug_name(name_part)
        
        if current_name:
            ingredients.append({
                "name": current_name,
                "strength": dosage_part
            })
            # Reset name for next iteration (unless the next part is just another dosage for same drug)
            current_name = "" 
            
    return ingredients

def _parse_racikan_entry(entry):
    """
    Parses: "Tremenza 1/5 tablet... m.f.pulv... :10.00:3 dd 1"
    """
    parts = entry.split(':')
    
    # The whole first part contains ingredients + compounding instructions
    full_recipe = parts[0].strip()
    
    # Separate Ingredients from Instructions (look for m.f., racikan, etc)
    split_match = re.search(r'\b(m\.?f\.?|racikan|puyer|dtd)\b', full_recipe, re.IGNORECASE)
    
    if split_match:
        ingredients_text = full_recipe[:split_match.start()].strip()
        compounding_instr = full_recipe[split_match.start():].strip()
    else:
        ingredients_text = full_recipe
        compounding_instr = ""

    # Extract structured ingredients
    ingredients_list = _extract_ingredients(ingredients_text)
    
    qty = parts[1].strip() if len(parts) > 1 else "1"
    freq = parts[2].strip() if len(parts) > 2 else "See instructions"

    try:
        if "." in qty: qty = str(int(float(qty)))
    except: pass

    return {
        "is_compound": True,
        "ingredients": ingredients_list, # Structured list
        "recipe_text": full_recipe,      # Full original text
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
