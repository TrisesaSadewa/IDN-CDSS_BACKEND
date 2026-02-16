import os
import json
import re
import aiohttp 
import asyncio
from typing import List, Optional, Dict, Any
from itertools import combinations
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

# --- IMPORT LOCAL MODULES ---
ner_engine = None
structured_drug_db = None

try:
    import ner_parser
    ner_engine = ner_parser.parser 
    import structured_drug_db
    print("SUCCESS: Local modules loaded.")
except ImportError as e:
    print(f"WARNING: Modules not found: {e}")

# --- CONFIGURATION ---
SUPABASE_URL = "https://crywwqleinnwoacithmw.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNyeXd3cWxlaW5ud29hY2l0aG13Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2ODQwODgxMiwiZXhwIjoyMDgzOTg0ODEyfQ.Uk9AFwxRHi7pwgP_lqYIWQ6JD7Ov1d07OzxiHswPNPQ"

app = FastAPI(title="Smart HIS Backend", version="9.0 - Logic Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"Supabase Connection Failed: {e}")
    supabase = None

# --- MODELS ---
class ParseRequest(BaseModel):
    text: str

class DDIRequest(BaseModel):
    drugs: List[str]

class ConsultationData(BaseModel):
    doctor_id: str
    appointment_id: str
    chief_complaint: str
    history_illness: str
    primary_diagnosis: str
    icd10_code: str
    secondary_diagnoses: List[str]
    clinical_notes: str
    therapy_instructions: str
    prescription_items: List[Dict[str, Any]]

# --- 1. THE LOGIC MATRIX (CLASS-BASED RULES) ---
# This is how a Real CDSS works. We define rules based on biological mechanisms.
# Any drug falling into these classes will trigger the rule.

INTERACTION_RULES = {
    # --- MAJOR ---
    frozenset(["antiplatelet", "nsaid"]): {
        "severity": "Major",
        "description": "NSAIDs competitively inhibit the antiplatelet effect of Aspirin-like drugs, increasing cardiovascular risk.",
        "advice": "Avoid concurrent use. If necessary, take NSAID 8 hours before or 30 mins after Antiplatelet."
    },
    frozenset(["hemostatic", "oral_contraceptive"]): {
        "severity": "Major",
        "description": "Thrombogenic effect is additive. Increased risk of clots/stroke.",
        "advice": "Contraindicated."
    },

    # --- MODERATE ---
    frozenset(["beta-blocker", "nsaid"]): {
        "severity": "Moderate",
        "description": "NSAIDs cause fluid retention and prostaglandin inhibition, antagonizing the antihypertensive effect.",
        "advice": "Monitor BP closely. Adjust dosage if needed."
    },
    frozenset(["arb", "nsaid"]): {
        "severity": "Moderate",
        "description": "NSAIDs reduce glomerular filtration. Combined with ARBs, this increases risk of renal failure.",
        "advice": "Monitor renal function and BP."
    },
    frozenset(["ace-inhibitor", "nsaid"]): {
        "severity": "Moderate",
        "description": "NSAIDs reduce glomerular filtration. Combined with ACEi, this increases risk of renal failure.",
        "advice": "Monitor renal function and BP."
    },

    # --- MINOR ---
    frozenset(["beta-blocker", "mucosal-protective"]): { # e.g. Carvedilol + Sucralfate
        "severity": "Minor",
        "description": "Mucosal protective agents may reduce absorption of other drugs.",
        "advice": "Separate dosing by at least 2 hours."
    },
    frozenset(["antiplatelet", "nitrate"]): { # e.g. Aspirin + Nitroglycerin
        "severity": "Minor",
        "description": "Antiplatelets may increase serum concentration of Nitrates.",
        "advice": "Monitor for hypotension or headache."
    },
    frozenset(["nitrate", "ppi"]): { # e.g. Nitroglycerin + Omeprazole
        "severity": "Minor",
        "description": "Minor potential for altered absorption.",
        "advice": "No specific action required."
    },
    
    # --- INFO / PROTECTIVE ---
    frozenset(["antiplatelet", "ppi"]): { # e.g. Aspirin + Omeprazole
        "severity": "Info",
        "description": "PPIs are often prescribed to prevent gastric bleeding from antiplatelet therapy.",
        "advice": "Beneficial combination for high-risk GI patients."
    },
    frozenset(["nsaid", "ppi"]): { # e.g. Ibuprofen + Omeprazole
        "severity": "Info",
        "description": "PPIs protect against NSAID-induced gastric injury.",
        "advice": "Beneficial combination."
    }
}

# --- HELPERS ---

def get_drug_class(drug_name: str) -> str:
    """
    Looks up the therapeutic class of a drug from the database.
    """
    if not drug_name: return "unknown"
    clean_name = drug_name.split()[0].lower()
    
    if structured_drug_db and hasattr(structured_drug_db, 'DRUG_INDEX'):
        drug_obj = structured_drug_db.DRUG_INDEX.get(clean_name)
        if drug_obj and drug_obj.drug_class:
            return drug_obj.drug_class.lower()
            
    # Fallback: Simple heuristic if DB lookup fails
    if "aspirin" in clean_name or "aspilet" in clean_name: return "antiplatelet"
    if "ibuprofen" in clean_name: return "nsaid"
    if "carvedilol" in clean_name: return "beta-blocker"
    if "omeprazole" in clean_name: return "ppi"
    
    return "unknown"

def extract_frequency(text: str) -> str:
    match = re.search(r'(\d+\s*[xX]\s*[\d\.,/]+)|(\d+\s*dd\s*[\d\.,/]+)|(s\s*\d+\s*dd)', text, re.IGNORECASE)
    if match: return match.group(0)
    return "1 x 1"

# --- ENDPOINTS ---

@app.post("/api/check-ddi")
async def check_ddi_endpoint(payload: DDIRequest):
    """
    Real CDSS Engine:
    1. Identify Drug Classes.
    2. Check Class-vs-Class Rules.
    """
    drugs = [d for d in payload.drugs if d]
    results = []
    
    if len(drugs) < 2:
        return {"interactions": [], "safe": True}

    # Generate all pairs
    pairs = list(combinations(drugs, 2))
    
    for da_name, db_name in pairs:
        # 1. Get Classes
        class_a = get_drug_class(da_name)
        class_b = get_drug_class(db_name)
        
        # 2. Check Rule Matrix
        pair_key = frozenset([class_a, class_b])
        
        # DEBUG PRINT (Check server logs if logic fails)
        print(f"Checking: {da_name}({class_a}) + {db_name}({class_b})")
        
        if pair_key in INTERACTION_RULES:
            rule = INTERACTION_RULES[pair_key]
            results.append({
                "pair": [da_name.title(), db_name.title()],
                "severity": rule["severity"],
                "description": rule["description"],
                "advice": rule["advice"],
                "source": "Clinical Guidelines (PIONAS)"
            })
            
    # Sort by severity
    severity_order = {"Major": 1, "Moderate": 2, "Minor": 3, "Info": 4}
    results.sort(key=lambda x: severity_order.get(x["severity"], 99))
    
    return {"interactions": results, "safe": len(results) == 0}

@app.post("/api/parse-prescription")
async def parse_prescription_endpoint(payload: ParseRequest):
    if not ner_engine: 
        raise HTTPException(status_code=500, detail="NER Parser not loaded. Check server logs.")
    try:
        lines = payload.text.split('\n')
        parsed_drugs = ner_engine.extract_drugs(lines)
        
        frontend_drugs = []
        for d in parsed_drugs:
            freq = extract_frequency(d.get('original_text', ''))
            dosage = f"{d.get('dose_mg', '')} mg" if d.get('dose_mg') else "Unknown dose"
            
            frontend_drugs.append({
                "drugName": d.get('brand_name', 'Unknown'),
                "dosage": dosage,
                "frequency": freq
            })
            
        return {
            "separate_drugs": frontend_drugs,
            "racikan": []
        }
    except Exception as e:
        print(f"Parse Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/doctor/submit-consultation")
async def submit_consultation(data: ConsultationData):
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
    try:
        subjective = f"CC: {data.chief_complaint}\n\nHPI: {data.history_illness}"
        assessment = f"PRIMARY: {data.primary_diagnosis} [{data.icd10_code}]\nNOTES: {data.clinical_notes}"
        
        res = supabase.table("consultations").insert({
            "appointment_id": data.appointment_id,
            "doctor_id": data.doctor_id,
            "subjective": subjective,
            "objective": "Recorded in Triage",
            "assessment": assessment,
            "plan": data.therapy_instructions,
            "prescription_raw_text": str(data.prescription_items)
        }).execute()
        
        consult_id = res.data[0]['id']
        supabase.table("appointments").update({"status": "pharmacy"}).eq("id", data.appointment_id).execute()
        
        return {"status": "success", "consultation_id": consult_id, "interactions": []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/patient/history")
async def get_patient_history(patient_id: str):
    if not supabase: return []
    try:
        appts_res = supabase.table("appointments").select("id").eq("patient_id", patient_id).execute()
        if not appts_res.data: return []
        appt_ids = [a['id'] for a in appts_res.data]
        consultations = supabase.table("consultations")\
            .select("*, doctors:profiles!doctor_id(full_name), appointments(scheduled_time)")\
            .in_("appointment_id", appt_ids)\
            .order("created_at", desc=True)\
            .execute()
        return consultations.data
    except Exception as e:
        return []

# ... (Standard GET Endpoints for Queue, Profile, etc.) ...
@app.get("/doctor/queue")
async def get_doctor_queue(doctor_id: str):
    if not supabase: return []
    return supabase.table("appointments").select("*, patients(*), triage_notes(*)").eq("doctor_id", doctor_id).in_("status", ["scheduled", "checked_in", "triage", "consultation"]).order("queue_number").execute().data

@app.get("/doctor/appointment/{appt_id}")
async def get_appointment_detail(appt_id: str):
    if not supabase: return {}
    res = supabase.table("appointments").select("*, patients(*), triage_notes(*)").eq("id", appt_id).single().execute()
    return res.data

@app.get("/patient/profile")
async def get_patient_profile(user_id: str):
    res = supabase.table("patients").select("*").eq("id", user_id).execute()
    return res.data[0] if res.data else {"mrn": "N/A"}

@app.get("/")
def read_root(): return {"status": "active", "version": "9.0 - Logic Engine"}

if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
