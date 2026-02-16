import os
import json
import re
import aiohttp 
import asyncio
from typing import List, Optional, Dict, Any
from itertools import combinations
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

# --- IMPORT LOCAL MODULES ---
try:
    import ner_parser
    import structured_drug_db 
    print("SUCCESS: Local modules loaded.")
except ImportError as e:
    print(f"WARNING: Modules not found: {e}")
    ner_parser = None
    structured_drug_db = None

# --- CONFIGURATION ---
SUPABASE_URL = "https://crywwqleinnwoacithmw.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNyeXd3cWxlaW5ud29hY2l0aG13Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2ODQwODgxMiwiZXhwIjoyMDgzOTg0ODEyfQ.Uk9AFwxRHi7pwgP_lqYIWQ6JD7Ov1d07OzxiHswPNPQ"

app = FastAPI(title="Smart HIS Backend", version="8.7 - Frontend Compat Fix")

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

class AppointmentBooking(BaseModel):
    patient_id: str
    doctor_id: str
    date: str
    time: str

# --- KNOWLEDGE BASE ---
KNOWN_INTERACTIONS = {
    frozenset(["aspirin", "ibuprofen"]): { "severity": "Major", "description": "Ibuprofen interferes with antiplatelet effect.", "advice": "Avoid concurrent use." },
    frozenset(["acetylsalicylic acid", "ibuprofen"]): { "severity": "Major", "description": "Ibuprofen interferes with antiplatelet effect.", "advice": "Avoid concurrent use." },
    frozenset(["carvedilol", "ibuprofen"]): { "severity": "Moderate", "description": "NSAIDs may diminish antihypertensive effect.", "advice": "Monitor BP." },
    frozenset(["candesartan", "ibuprofen"]): { "severity": "Moderate", "description": "Risk of renal impairment.", "advice": "Monitor BP and renal function." },
    frozenset(["carvedilol", "sucralfate"]): { "severity": "Minor", "description": "Reduced absorption.", "advice": "Separate dosing by 2 hours." },
    frozenset(["nitroglycerin", "aspirin"]): { "severity": "Minor", "description": "Increased serum concentration.", "advice": "Monitor for hypotension." },
    frozenset(["aspirin", "omeprazole"]): { "severity": "Info", "description": "Protective combination.", "advice": "Beneficial." },
}

# --- HELPERS ---
def resolve_active_ingredients(drug_name: str) -> List[str]:
    if not drug_name: return []
    clean_name = drug_name.split()[0].lower()
    ingredients = []
    if structured_drug_db and hasattr(structured_drug_db, 'DRUG_INDEX'):
        drug_obj = structured_drug_db.DRUG_INDEX.get(clean_name)
        if drug_obj:
            source_text = drug_obj.generic_name or drug_obj.brand_name
            parts = re.split(r'[/,+]', source_text)
            for p in parts:
                cleaned = p.strip()
                cleaned = re.sub(r'\s*\d+.*$', '', cleaned) 
                if cleaned: ingredients.append(cleaned)
            return ingredients
    return [clean_name]

def extract_frequency(text: str) -> str:
    """Simple regex to find frequency in original text for frontend display."""
    match = re.search(r'(\d+\s*[xX]\s*[\d\.,/]+)|(\d+\s*dd\s*[\d\.,/]+)|(s\s*\d+\s*dd)', text, re.IGNORECASE)
    if match: return match.group(0)
    return "1 x 1" # Default if not found

# --- ENDPOINTS ---

@app.post("/api/check-ddi")
async def check_ddi_endpoint(payload: DDIRequest):
    # (Same DDI Logic as before, condensed for brevity)
    active_ingredients = set()
    IGNORE_TERMS = {"tab", "caps", "mg", "ml", "g", "inj", "syr"}
    for d in payload.drugs:
        if not d: continue
        resolved = resolve_active_ingredients(d)
        for ing in resolved:
            clean = ing.strip().lower()
            if clean not in IGNORE_TERMS and len(clean) > 2: active_ingredients.add(clean)

    check_items = list(active_ingredients)
    results = []
    if len(check_items) >= 2:
        pairs = list(combinations(check_items, 2))
        for da, db in pairs:
            pair_set = frozenset([da.lower(), db.lower()])
            if pair_set in KNOWN_INTERACTIONS:
                info = KNOWN_INTERACTIONS[pair_set]
                results.append({
                    "pair": [da.title(), db.title()],
                    "severity": info["severity"],
                    "description": info["description"],
                    "advice": info["advice"],
                    "source": "Clinical DB"
                })
    
    severity_order = {"Major": 1, "Moderate": 2, "Minor": 3, "Info": 4}
    results.sort(key=lambda x: severity_order.get(x["severity"], 99))
    return {"interactions": results, "safe": len(results) == 0}

@app.post("/api/parse-prescription")
async def parse_prescription_endpoint(payload: ParseRequest):
    if not ner_parser: raise HTTPException(status_code=500, detail="NER Parser not loaded.")
    try:
        lines = payload.text.split('\n')
        parsed_drugs = ner_parser.extract_drugs(lines)
        
        # --- COMPATIBILITY LAYER ---
        # Convert new parser format to what doctor_logic.js expects
        frontend_drugs = []
        for d in parsed_drugs:
            # Map 'brand_name' -> 'drugName'
            # Extract frequency from original text
            freq = extract_frequency(d.get('original_text', ''))
            dosage = f"{d.get('dose_mg', '')} mg" if d.get('dose_mg') else "Unknown dose"
            
            frontend_drugs.append({
                "drugName": d.get('brand_name', 'Unknown'),
                "dosage": dosage,
                "frequency": freq
            })
            
        return {
            "separate_drugs": frontend_drugs,
            "racikan": [] # New parser doesn't handle racikan yet, send empty list to prevent JS error
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/doctor/submit-consultation")
async def submit_consultation(data: ConsultationData):
    # (Same Supabase logic as before)
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
    try:
        # Construct consultation record
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
        
        # Update Appointment Status
        supabase.table("appointments").update({"status": "pharmacy"}).eq("id", data.appointment_id).execute()
        
        return {"status": "success", "consultation_id": consult_id, "interactions": []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ... (Retain GET endpoints for Queue, History, etc.) ...
@app.get("/doctor/queue")
async def get_doctor_queue(doctor_id: str):
    if not supabase: return []
    return supabase.table("appointments").select("*, patients(*), triage_notes(*)").eq("doctor_id", doctor_id).in_("status", ["scheduled", "checked_in", "triage", "consultation"]).order("queue_number").execute().data

@app.get("/doctor/appointment/{appt_id}")
async def get_appointment_detail(appt_id: str):
    if not supabase: return {}
    res = supabase.table("appointments").select("*, patients(*), triage_notes(*)").eq("id", appt_id).single().execute()
    return res.data

if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
