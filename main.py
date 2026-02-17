import os
import json
import re
from typing import List, Optional, Dict, Any
from itertools import combinations
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

# --- IMPORT MODULES ---
ner_engine = None
structured_drug_db = None

try:
    import structured_drug_db
    import ner_parser
    ner_engine = ner_parser.parser 
    print("SUCCESS: Modules loaded.")
except Exception as e:
    print(f"MODULE ERROR: {e}")

# --- CONFIG ---
SUPABASE_URL = "https://crywwqleinnwoacithmw.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNyeXd3cWxlaW5ud29hY2l0aG13Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2ODQwODgxMiwiZXhwIjoyMDgzOTg0ODEyfQ.Uk9AFwxRHi7pwgP_lqYIWQ6JD7Ov1d07OzxiHswPNPQ"
app = FastAPI(title="Smart HIS Backend", version="9.6 - Dose Fix")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

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

# --- MECHANISM-BASED CLASS RULES ---
CLASS_RULES = {
    # MAJOR (Red)
    frozenset(["antiplatelet", "nsaid"]): { "severity": "Major", "description": "Pharmacodynamic Antagonism: NSAID blocks Aspirin's antiplatelet site, negating stroke protection.", "advice": "Avoid concurrent use." },
    frozenset(["hemostatic", "oral_contraceptive"]): { "severity": "Major", "description": "Additive Thrombogenic Effect: High risk of clots.", "advice": "Contraindicated." },
    
    # INTERMEDIATE (Orange)
    frozenset(["beta-blocker", "nsaid"]): { "severity": "Intermediate", "description": "Physiologic Antagonism: NSAIDs reduce antihypertensive efficacy via fluid retention.", "advice": "Monitor BP." },
    frozenset(["ace-inhibitor", "nsaid"]): { "severity": "Intermediate", "description": "Renal Hemodynamics: Additive risk of renal impairment.", "advice": "Monitor renal function." },
    frozenset(["arb", "nsaid"]): { "severity": "Intermediate", "description": "Renal Hemodynamics: Additive risk of renal impairment.", "advice": "Monitor renal function." },
    
    # MINOR (Blue)
    frozenset(["mucosal-protective", "beta-blocker"]): { "severity": "Minor", "description": "Absorption Interference: Sucralfate coating reduces drug uptake.", "advice": "Separate dosing by 2 hours." },
    frozenset(["mucosal-protective", "antiplatelet"]): { "severity": "Minor", "description": "Absorption Interference: Sucralfate coating reduces drug uptake.", "advice": "Separate dosing by 2 hours." },
    frozenset(["nitrate", "antiplatelet"]): { "severity": "Minor", "description": "Additive Hemodynamics: Potential for increased vasodilation.", "advice": "Monitor for hypotension." },
    frozenset(["nitrate", "ppi"]): { "severity": "Minor", "description": "Pharmacokinetic: Minor alteration in absorption.", "advice": "Monitor status." },
    frozenset(["beta-blocker", "antiplatelet"]): { "severity": "Minor", "description": "Additive Hemodynamics: Potential hypotensive effect.", "advice": "Routine monitoring." },
    frozenset(["antiplatelet", "ppi"]): { "severity": "Minor", "description": "Pharmacokinetic: Increased pH may alter enteric-coated tablet dissolution.", "advice": "Monitor efficacy (though often used for protection)." },
}

# --- HELPERS ---
def get_drug_info(drug_name: str):
    if not drug_name: return ("unknown", "unknown")
    clean_name = drug_name.replace("ANS ", "").split()[0].lower()
    
    if structured_drug_db and hasattr(structured_drug_db, 'DRUG_INDEX'):
        drug_obj = structured_drug_db.DRUG_INDEX.get(clean_name)
        if drug_obj:
            return (drug_obj.generic_name.lower(), drug_obj.drug_class.lower())

    if "aspirin" in clean_name or "aspilet" in clean_name or "miniaspi" in clean_name or "thrombo" in clean_name: return ("acetylsalicylic acid", "antiplatelet")
    if "ibuprofen" in clean_name: return ("ibuprofen", "nsaid")
    if "carvedilol" in clean_name or "v-bloc" in clean_name: return ("carvedilol", "beta-blocker")
    if "omeprazole" in clean_name: return ("omeprazole", "ppi")
    if "sucralfate" in clean_name: return ("sucralfate", "mucosal-protective")
    if "nitro" in clean_name or "isdn" in clean_name: return ("nitroglycerin", "nitrate")
    if "candesartan" in clean_name: return ("candesartan", "arb")
        
    return (clean_name, "unknown")

def extract_frequency(text: str) -> str:
    match = re.search(r'(\d+\s*[xX]\s*[\d\.,/]+)|(\d+-\d+-\d+)|(s\s*\d+\s*dd)', text, re.IGNORECASE)
    if match: return match.group(0)
    return "1 x 1"

def extract_dose_text(text: str) -> Optional[str]:
    # Looks for things like "6.25 MG", "500 mg", "0.5 g"
    match = re.search(r'(\d+[.,]?\d*)\s*(mg|g|mcg|ml|iu)', text, re.IGNORECASE)
    if match:
        return f"{match.group(1)} {match.group(2).lower()}"
    return None

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
    if not ner_engine: raise HTTPException(status_code=500, detail="NER Parser not loaded")
    try:
        if "|||" in payload.text:
            lines = [l.strip() for l in payload.text.split("|||") if l.strip()]
        else:
            lines = payload.text.split('\n')

        parsed_drugs = ner_engine.extract_drugs(lines)
        
        frontend_drugs = []
        for d in parsed_drugs:
            original_text = d.get('original_text', '')
            freq = extract_frequency(original_text)
            
            # Logic: Prefer DB dose, fallback to Text extraction, fallback to Unknown
            if d.get('dose_mg'):
                dosage = f"{d.get('dose_mg')} mg"
            else:
                text_dose = extract_dose_text(original_text)
                dosage = text_dose if text_dose else "Unknown dose"
            
            frontend_drugs.append({
                "drugName": d.get('brand_name', 'Unknown'),
                "dosage": dosage,
                "frequency": freq
            })
        return {"separate_drugs": frontend_drugs, "racikan": []}
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
def read_root(): return {"status": "active", "version": "9.6"}

if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
