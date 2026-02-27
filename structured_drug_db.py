import json
import os
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
        
        # SUPABASE CONFIG (Mirroring main.py)
        self.url = "https://crywwqleinnwoacithmw.supabase.co"
        self.key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNyeXd3cWxlaW5ud29hY2l0aG13Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2ODQwODgxMiwiZXhwIjoyMDgzOTg0ODEyfQ.Uk9AFwxRHi7pwgP_lqYIWQ6JD7Ov1d07OzxiHswPNPQ"
        
        self.load_data()

    def load_data(self):
        """Loads drugs from the Supabase knowledge_map table in batches."""
        try:
            supabase: Client = create_client(self.url, self.key)
            print("Fetching drug dictionary from Supabase Knowledge Map...")
            
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
                
                # Safety break if it gets too huge, though we expect ~56k
                if offset > 100000: break 
            
            self.drugs = [
                Drug(
                    brand_name=d.get('local_term', 'Unknown'),
                    generic_name=d.get('openfda_term', 'Unknown'),
                    drug_class='unknown',
                    dose_mg=None,
                    is_pediatric=False
                ) 
                for d in all_rows if d.get('local_term')
            ]
            
            # Create Fast Lookup Index
            for drug in self.drugs:
                if drug.brand_name and drug.brand_name.lower() != "unknown":
                    self.index[drug.brand_name.lower()] = drug
                if drug.generic_name and drug.generic_name.lower() != "unknown":
                    self.index[drug.generic_name.lower()] = drug
            
            print(f"✅ Database loaded from Supabase: {len(self.drugs)} entries active.")
            
        except Exception as e:
            print(f"❌ Error loading database from Supabase: {e}")
            print("Attempting to fallback to local drug_database.json...")
            self._load_local_fallback()

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
                print(f"✅ Loaded {len(self.drugs)} drugs from local fallback.")
            except Exception as e:
                print(f"Fallack failed: {e}")

    def get_all(self):
        return self.drugs

# --- SINGLETON INSTANCE ---
db_instance = DrugDatabase()
DRUGS = db_instance.get_all()
DRUG_INDEX = db_instance.index
