import json
import os
import sys
import io
from dataclasses import dataclass
from typing import Optional, List, Dict
from supabase import create_client, Client

@dataclass
class Drug:
    brand_name: str
    generic_name: str
    drug_class: str
    dose_mg: Optional[float] = None
    is_pediatric: bool = False

class DrugDatabase:
    def __init__(self, json_path='drug_database.json'):
        self.drugs: List[Drug] = []
        self.index: Dict[str, Drug] = {} 
        
        self.url = "https://crywwqleinnwoacithmw.supabase.co"
        self.key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNyeXd3cWxlaW5ud29hY2l0aG13Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2ODQwODgxMiwiZXhwIjoyMDgzOTg0ODEyfQ.Uk9AFwxRHi7pwgP_lqYIWQ6JD7Ov1d07OzxiHswPNPQ"
        
        self.load_data()

    def load_data(self):
        """Loads drugs from the Supabase knowledge_map table in batches in the background."""
        # 1. First, synchronously load the complete local fallback database so we are instantly fully functional
        self._load_local_fallback()
        
        # 2. Trigger background fetch from Supabase to get the absolute latest changes
        import threading
        threading.Thread(target=self._fetch_supabase_background, daemon=True).start()

    def _fetch_supabase_background(self):
        """Asynchronously loads the drug database from Supabase and atomically updates in-place references."""
        try:
            supabase: Client = create_client(self.url, self.key)
            
            print("Background: Fetching Therapeutic Class maps from Supabase...")
            class_res = supabase.table("generic_classes").select("generic_name, drug_class").execute()
            generic_to_class = {c['generic_name'].lower(): c['drug_class'].lower() for c in class_res.data}
            
            print("Background: Fetching drug dictionary from Supabase Knowledge Map...")
            all_rows = []
            page_size = 1000
            offset = 0
            
            while True:
                res = supabase.table("knowledge_map") \
                    .select("local_term, openfda_term") \
                    .range(offset, offset + page_size - 1) \
                    .execute()
                
                rows = res.data
                if not rows:
                    break
                    
                all_rows.extend(rows)
                offset += page_size
                if offset > 100000: break 
            
            new_drugs = []
            for d in all_rows:
                local_name_raw = d.get('local_term')
                local_name = local_name_raw if local_name_raw else 'Unknown'
                generic_candidate_raw = d.get('openfda_term')
                generic_candidate = generic_candidate_raw.lower() if generic_candidate_raw else 'unknown'
                
                infallible_class = generic_to_class.get(generic_candidate, "unknown")
                if infallible_class == "unknown":
                    infallible_class = generic_to_class.get(local_name.lower(), "unknown")
                
                new_drugs.append(Drug(
                    brand_name=local_name,
                    generic_name=generic_candidate,
                    drug_class=infallible_class,
                    dose_mg=None,
                    is_pediatric=False
                ))
            
            new_index = {}
            for drug in new_drugs:
                if drug.brand_name and drug.brand_name.lower() != "unknown":
                    new_index[drug.brand_name.lower()] = drug
                if drug.generic_name and drug.generic_name.lower() != "unknown":
                    new_index[drug.generic_name.lower()] = drug
            
            # Atomic update of in-memory references to preserve imported objects
            self.drugs.clear()
            self.drugs.extend(new_drugs)
            
            self.index.clear()
            self.index.update(new_index)
            
            print(f"SUCCESS: Background database updated. {len(self.drugs)} entries, {len(generic_to_class)} classes mapped.")
            
            # Rebuild NER parser's BRAND_MAP to incorporate the updated background entries
            try:
                import ner_parser
                if ner_parser.parser:
                    print("Background: Rebuilding NER Map...")
                    ner_parser.parser.BRAND_MAP.clear()
                    ner_parser.parser.MAX_WORD_LENGTH = 0
                    for drug in self.drugs:
                        if not hasattr(drug, 'brand_name'):
                            continue
                        ner_parser.parser._map_drug(drug)
                    print(f"SUCCESS: Background NER Map rebuilt with {len(ner_parser.parser.BRAND_MAP)} brands.")
            except Exception as pe:
                print(f"Background: Rebuilding NER Map warning: {pe}")
            
        except Exception as e:
            print(f"ERROR: Background loading database from Supabase: {e}")

    def _load_local_fallback(self):
        json_path = os.path.join(os.path.dirname(__file__), 'drug_database.json')
        if os.path.exists(json_path):
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    raw_list = json.load(f)
                self.drugs = [
                    Drug(
                        brand_name=d.get('brand_name', 'Unknown'),
                        generic_name=d.get('generic_name', 'Unknown'),
                        drug_class=d.get('drug_class', 'unknown'),
                        dose_mg=d.get('dose_mg'),
                        is_pediatric=d.get('is_pediatric', False)
                    ) 
                    for d in raw_list
                ]
                for drug in self.drugs:
                    if drug.brand_name and drug.brand_name != "Unknown":
                        self.index[drug.brand_name.lower()] = drug
                    if drug.generic_name:
                        self.index[drug.generic_name.lower()] = drug
                print(f"SUCCESS: Loaded {len(self.drugs)} drugs from local fallback.")
            except Exception as e:
                print(f"Fallback failed: {e}")

    def get_all(self):
        return self.drugs

# --- SINGLETON INSTANCE ---
db_instance = DrugDatabase()
DRUGS = db_instance.get_all()
DRUG_INDEX = db_instance.index

