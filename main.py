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

# --- IMPORT LOCAL MODULES WITH ERROR HANDLING ---
ner_engine = None
structured_drug_db = None

print("--- STARTING SERVER ---")
try:
    import structured_drug_db
    print(f"DB Loaded. Index size: {len(getattr(structured_drug_db, 'DRUG_INDEX', {}))}")
    import ner_parser
    ner_engine = ner_parser.parser 
    print("NER Engine Loaded.")
except Exception as e:
    print(f"CRITICAL MODULE ERROR: {e}")

# --- CONFIGURATION ---
SUPABASE_URL = "https://crywwqleinnwoacithmw.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNyeXd3cWxlaW5ud29hY2l0aG13Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2ODQwODgxMiwiZXhwIjoyMDgzOTg0ODEyfQ.Uk9AFwxRHi7pwgP_lqYIWQ6JD7Ov1d07OzxiHswPNPQ"

app = FastAPI(title="Smart HIS Backend", version="9.3 - Stable")

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

# --- LOGIC RULES ---
CLASS_RULES = {
    # MAJOR
    frozenset(["antiplatelet", "nsaid"]): { "severity": "Major", "description": "NSAIDs competitively inhibit the antiplatelet effect of Aspirin.", "advice": "Avoid concurrent use. Take NSAID 8 hours before or 30 mins after." },
    frozenset(["hemostatic", "oral_contraceptive"]): { "severity": "Major", "description": "Additive thrombogenic effect. High risk of clots.", "advice": "Contraindicated." },
    # MODERATE
    frozenset(["beta-blocker", "nsaid"]): { "severity": "Intermediate", "description": "NSAIDs reduce antihypertensive efficacy.", "advice": "Monitor BP." },
    frozenset(["ace-inhibitor", "nsaid"]): { "severity": "Intermediate", "description": "Risk of renal impairment.", "advice": "Monitor renal function." },
    frozenset(["arb", "nsaid"]): { "severity": "Intermediate", "description": "Risk of renal impairment.", "advice": "Monitor renal function." },
    # MINOR
    frozenset(["mucosal-protective", "beta-blocker"]): { "severity": "Minor", "description": "Absorption interference.", "advice": "Separate dosing by 2 hours." },
    frozenset(["mucosal-protective", "antiplatelet"]): { "severity": "Minor", "description": "Absorption interference.", "advice": "Separate dosing by 2 hours." },
    frozenset(["nitrate", "antiplatelet"]): { "severity": "Minor", "description": "Potential additive hypotension.", "advice": "Monitor for headache/hypotension." },
    frozenset(["nitrate", "ppi"]): { "severity": "Minor", "description": "Minor pharmacokinetic interaction.", "advice": "Monitor status." },
    # INFO
    frozenset(["antiplatelet", "ppi"]): { "severity": "Info", "description": "Protective against GI bleeding.", "advice": "Beneficial." },
    frozenset(["nsaid", "ppi"]): { "severity": "Info", "description": "Protective against GI bleeding.", "advice": "Beneficial." }
}

# --- HELPERS ---
def get_drug_info(drug_name: str):
    if not drug_name: return ("unknown", "unknown")
    clean_name = drug_name.split()[0].lower()
    
    # 1. DB Lookup
    if structured_drug_db and hasattr(structured_drug_db, 'DRUG_INDEX'):
        drug_obj = structured_drug_db.DRUG_INDEX.get(clean_name)
        if drug_obj:
            return (drug_obj.generic_name.lower(), drug_obj.drug_class.lower())

    # 2. Heuristic Fallback
    if "aspirin" in clean_name or "aspilet" in clean_name: return ("acetylsalicylic acid", "antiplatelet")
    if "ibuprofen" in clean_name: return ("ibuprofen", "nsaid")
    if "carvedilol" in clean_name: return ("carvedilol", "beta-blocker")
    if "omeprazole" in clean_name: return ("omeprazole", "ppi")
    if "sucralfate" in clean_name: return ("sucralfate", "mucosal-protective")
    if "nitro" in clean_name: return ("nitroglycerin", "nitrate")
        
    return (clean_name, "unknown")

def extract_frequency(text: str) -> str:
    match = re.search(r'(\d+\s*[xX]\s*[\d\.,/]+)|(\d+\s*dd\s*[\d\.,/]+)|(s\s*\d+\s*dd)', text, re.IGNORECASE)
    if match: return match.group(0)
    return "1 x 1"

# --- ENDPOINTS ---
@app.post("/api/check-ddi")
async def check_ddi_endpoint(payload: DDIRequest):
    drugs = [d for d in payload.drugs if d]
    results = []
    
    if len(drugs) < 2: return {"interactions": [], "safe": True}
    
    pairs = list(combinations(drugs, 2))
    for da, db in pairs:
        gen_a, class_a = get_drug_info(da)
        gen_b, class_b = get_drug_info(db)
        
        # Check Rules
        mech_key = frozenset([class_a, class_b])
        if mech_key in CLASS_RULES:
            rule = CLASS_RULES[mech_key]
            results.append({
                "pair": [da.title(), db.title()],
                "severity": rule["severity"],
                "description": rule["description"],
                "advice": rule["advice"],
                "source": "Mechanism Logic"
            })
            
    severity_order = {"Major": 1, "Intermediate": 2, "Moderate": 2, "Minor": 3, "Info": 4}
    results.sort(key=lambda x: severity_order.get(x["severity"], 99))
    return {"interactions": results, "safe": len(results) == 0}

@app.post("/api/parse-prescription")
async def parse_prescription_endpoint(payload: ParseRequest):
    if not ner_engine:
        raise HTTPException(status_code=500, detail="NER Parser failed to load on server boot. Check logs.")
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
        return {"separate_drugs": frontend_drugs, "racikan": []}
    except Exception as e:
        print(f"Parse Error: {e}")
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
        print(f"History Error: {e}")
        return []

# ... Standard endpoints ...
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
            "objective": "Triage Data",
            "assessment": assessment,
            "plan": data.therapy_instructions,
            "prescription_raw_text": str(data.prescription_items)
        }).execute()
        
        # Update Appointment
        supabase.table("appointments").update({"status": "pharmacy"}).eq("id", data.appointment_id).execute()
        
        return {"status": "success", "consultation_id": res.data[0]['id'], "interactions": []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
def read_root(): return {"status": "active", "version": "9.3"}

if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
