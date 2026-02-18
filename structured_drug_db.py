import json
import os
from dataclasses import dataclass
from typing import Optional, List, Dict

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
        self.json_path = os.path.join(os.path.dirname(__file__), json_path)
        self.load_data()

    def load_data(self):
        """Loads drugs from the external JSON file."""
        if not os.path.exists(self.json_path):
            print(f"CRITICAL WARNING: {self.json_path} not found. DB is empty.")
            return

        try:
            with open(self.json_path, 'r', encoding='utf-8') as f:
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
            
            # Create Fast Lookup Index
            for drug in self.drugs:
                if drug.brand_name and drug.brand_name != "Unknown":
                    self.index[drug.brand_name.lower()] = drug
                if drug.generic_name:
                    self.index[drug.generic_name.lower()] = drug
            
            print(f"✅ Database loaded successfully: {len(self.drugs)} drugs active.")
            
        except Exception as e:
            print(f"❌ Error loading database: {e}")

    def get_all(self):
        return self.drugs

# --- SINGLETON INSTANCE ---
db_instance = DrugDatabase()
DRUGS = db_instance.get_all()
DRUG_INDEX = db_instance.index
