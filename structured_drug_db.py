import re
import sys
import io

# Check for safe import to prevent crashes if DB format is wrong
try:
    from structured_drug_db import DRUGS, Drug
except ImportError:
    DRUGS = []
    # Mock Drug class for manual injection if needed
    class Drug:
        def __init__(self, brand_name, generic_name, drug_class, dose_mg=None):
            self.brand_name = brand_name
            self.generic_name = generic_name
            self.drug_class = drug_class
            self.dose_mg = dose_mg

class IndonesianDrugParser:
    def __init__(self):
        self.BRAND_MAP = {}
        self.MAX_WORD_LENGTH = 0
        
        # Load from the dynamic database (which now fetches from Supabase)
        print(f"Building NER Map from {len(DRUGS)} entries...")
        for drug in DRUGS:
            if not hasattr(drug, 'brand_name'):
                continue
            self._map_drug(drug)

    def _map_drug(self, drug):
        # Map Brand Name
        if drug.brand_name and drug.brand_name.lower() != "unknown":
            key = drug.brand_name.lower()
            self.BRAND_MAP[key] = drug
            self._update_max_length(key)
        
        # Map Generic Name
        if drug.generic_name and len(drug.generic_name) > 3:
            key = drug.generic_name.lower()
            if key not in self.BRAND_MAP:
                self.BRAND_MAP[key] = drug
                self._update_max_length(key)

    def _update_max_length(self, text):
        word_count = len(text.split())
        if word_count > self.MAX_WORD_LENGTH:
            self.MAX_WORD_LENGTH = word_count

    def clean_text(self, raw_text):
        """Removes Indonesian prescription noise and identifies equipment to ignore."""
        if not raw_text: return ""
        text = str(raw_text).lower().strip()
        
        # 1. HARD IGNORE: If it's clearly equipment, we return an empty string
        # This boosts Specificity by ensuring these aren't processed as drugs.
        # Fixed: r'^ans\s+' was too broad and swallowed "ANS drug_name" lines.
        equipment_ignore = [
            r'^jarum\b', r'^spuit\b', r'^infus\b', r'^abocath\b', r'^alkohol\b'
        ]
        for pattern in equipment_ignore:
            if re.search(pattern, text):
                return "" # Tell parser to ignore this entire line

        # 2. DOSAGE & ADMINISTRATIVE PATTERNS (Noise Removal)
        patterns = [
            r'^ans\s+',                 # Strip inventory prefix
            r'\b\d+\s*x\s*[\d\.,/]+',   # 3 x 1
            r'\b\d+\s*dd\s*[\d\.,/]+',  # 3 dd 1
            r'\bs\s*\d+\s*dd',          # S 3 dd
            r'\bno\s*[xivlc]+',         # No XII
            r'\bno\s*\d+',              # No 10
            r'\btab\b|\bcaps\b|\bcap\b|\bsyr\b|\bcth\b|\bbungkus\b|\bsachet\b|\bfls\b|\btube\b|\binj\b'
        ]
        
        for p in patterns:
            text = re.sub(p, ' ', text)
            
        # Keep hyphens for drugs like v-bloc
        text = re.sub(r'[^\w\s-]', ' ', text) 
        return " ".join(text.split())

    def extract_drugs(self, prescription_list):
        detected_drugs = []
        if not prescription_list: return []
        
        for line in prescription_list:
            if not line: continue
            cleaned_line = self.clean_text(line)
            words = cleaned_line.split()
            n = len(words)
            i = 0
            
            # Greedy N-Gram Matcher
            while i < n:
                match_found = False
                window_limit = min(self.MAX_WORD_LENGTH, n - i)
                for length in range(window_limit, 0, -1):
                    phrase = " ".join(words[i : i + length])
                    
                    if phrase in self.BRAND_MAP:
                        drug_obj = self.BRAND_MAP[phrase]
                        detected_drugs.append({
                            "brand_name": drug_obj.brand_name,
                            "generic": drug_obj.generic_name,
                            "class": drug_obj.drug_class,
                            "dose_mg": drug_obj.dose_mg,
                            "original_text": line
                        })
                        i += length 
                        match_found = True
                        break
                
                if not match_found:
                    i += 1
            
        return detected_drugs

# Create the parser instance
parser = IndonesianDrugParser()

