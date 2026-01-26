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
    import structured_drug_db # Critical for ingredient lookup
    print("SUCCESS: Local modules loaded.")
except ImportError as e:
    print(f"WARNING: Modules not found: {e}")
    ner_parser = None
    structured_drug_db = None

# --- CONFIGURATION ---
SUPABASE_URL = "https://crywwqleinnwoacithmw.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNyeXd3cWxlaW5ud29hY2l0aG13Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2ODQwODgxMiwiZXhwIjoyMDgzOTg0ODEyfQ.Uk9AFwxRHi7pwgP_lqYIWQ6JD7Ov1d07OzxiHswPNPQ"

app = FastAPI(title="Smart HIS Backend", version="6.4 - History Fix")

# --- CORS CONFIGURATION ---
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

# --- CURATED DDI KNOWLEDGE BASE ---
KNOWN_INTERACTIONS = {
    # Major
    frozenset(["aspirin", "ibuprofen"]): "Major",
    frozenset(["acetylsalicylic acid", "ibuprofen"]): "Major",
    
    # Moderate
    frozenset(["carvedilol", "ibuprofen"]): "Moderate",
    
    # Minor
    frozenset(["carvedilol", "sucralfate"]): "Minor",
    frozenset(["carvedilol", "aspirin"]): "Minor",
    frozenset(["carvedilol", "acetylsalicylic acid"]): "Minor",
    frozenset(["nitroglycerin", "aspirin"]): "Minor",
    frozenset(["nitroglycerin", "acetylsalicylic acid"]): "Minor",
    frozenset(["nitroglycerin", "omeprazole"]): "Minor",
    frozenset(["aspirin", "omeprazole"]): "Minor",
    frozenset(["acetylsalicylic acid", "omeprazole"]): "Minor",
}

# --- INTELLIGENT DDI CHECKER ---

def resolve_active_ingredients(drug_name: str) -> List[str]:
    """
    Converts a Brand Name (e.g. 'Miniaspi') into its list of active ingredients
    (e.g. ['Aspirin']) using the local DB.
    """
    clean_name = drug_name.split()[0].lower()
    ingredients = []

    if structured_drug_db and hasattr(structured_drug_db, 'DRUG_INDEX'):
        drug_obj = structured_drug_db.DRUG_INDEX.get(clean_name)
        
        if drug_obj:
            source_text = drug_obj.contents or drug_obj.generic or drug_obj.name
            parts = re.split(r'[/,+]', source_text)
            for p in parts:
                cleaned = p.strip()
                cleaned = re.sub(r'\s*\d+.*$', '', cleaned) # Remove dosage
                if cleaned:
                    ingredients.append(cleaned)
            return ingredients

    return [clean_name]

async def check_openfda_interactions(drug_list: List[str]) -> Dict[str, List[str]]:
    """
    Checks interactions using a Hybrid Approach:
    1. Check Local Curated DB (Fast & Accurate).
    2. Check OpenFDA API (Fallback).
    """
    IGNORE_TERMS = {
        "tab", "tablet", "cap", "capsule", "caps", "inj", "injection", "injeksi",
        "syr", "syrup", "sirup", "susp", "suspension", "drops", "drop", "gtts",
        "ml", "mg", "g", "mcg", "iu", "unit", "amp", "vial", "btl", "tube", "sachet",
        "supp", "suppository", "kapsul", "bungkus", "puyer", "racikan", "compound",
        "cream", "krim", "oint", "ointment", "salep", "gel", "lotion",
        "spuit", "infus", "set", "kasa", "needle", "syringe", "disp"
    }

    active_ingredients = set()
    for d in drug_list:
        if not d: continue
        resolved = resolve_active_ingredients(d)
        for ing in resolved:
            clean_ing = ing.strip().lower()
            if clean_ing in IGNORE_TERMS or clean_ing.replace('.', '', 1).isdigit(): continue
            if len(clean_ing) < 3: continue
            active_ingredients.add(clean_ing)

    check_items = list(active_ingredients)
    
    categorized_warnings = { "high": [], "medium": [], "low": [] }
    
    if len(check_items) < 2: return categorized_warnings

    pairs = list(combinations(check_items, 2))
    
    async with aiohttp.ClientSession() as session:
        for drug_a, drug_b in pairs:
            # 1. CHECK LOCAL KNOWLEDGE BASE FIRST
            pair_set = frozenset([drug_a, drug_b])
            if pair_set in KNOWN_INTERACTIONS:
                severity = KNOWN_INTERACTIONS[pair_set]
                msg = f"{drug_a.title()} x {drug_b.title()} (Known Interaction)"
                if severity == "Major": categorized_warnings["high"].append(msg)
                elif severity == "Moderate": categorized_warnings["medium"].append(msg)
                elif severity == "Minor": categorized_warnings["low"].append(msg)
                continue # Skip API check if found locally

            # 2. CHECK OPENFDA (FALLBACK)
            # Only query if NOT found locally
            query = f'patient.drug.medicinalproduct:("{drug_a}"+AND+"{drug_b}")'
            
            try:
                # Use 'count' endpoint to get outcome distribution for severity assessment
                # If we rely purely on counts, common drugs (Omeprazole) flag everything.
                # We need stricter thresholds for unknown pairs.
                count_url = f"https://api.fda.gov/drug/event.json?search={query}&limit=1"
                
                async with session.get(count_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        total = data.get('meta', {}).get('results', {}).get('total', 0)
                        
                        # MUCH HIGHER Thresholds for auto-flagging to reduce noise
                        # 500 reports is common for widely used drugs. 
                        # We only auto-flag extremely high signals or specific keywords if we parsed them.
                        # For now, simply raising the bar.
                        if total > 1000:
                            # Instead of "High", default unknown high-volume to "Medium" caution
                            # unless we confirm "Death" outcomes.
                            categorized_warnings["medium"].append(f"{drug_a.title()} + {drug_b.title()} ({total} reports - Review)")
            except Exception as e:
                print(f"OpenFDA Error for {drug_a}/{drug_b}: {e}")
                
    return categorized_warnings

# --- ENDPOINTS ---

@app.post("/api/check-ddi")
async def check_ddi_endpoint(payload: DDIRequest):
    print(f"Checking DDI for: {payload.drugs}")
    warnings = await check_openfda_interactions(payload.drugs)
    # Flatten for simple frontend check
    safe = (len(warnings["high"]) + len(warnings["medium"]) + len(warnings["low"])) == 0
    return {"warnings": warnings, "safe": safe}

@app.post("/api/parse-prescription")
async def parse_prescription_endpoint(payload: ParseRequest):
    if not ner_parser: raise HTTPException(status_code=500, detail="NER Parser module not loaded.")
    try:
        return ner_parser.parse_prescription_text(payload.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/doctor/submit-consultation")
async def submit_consultation(data: ConsultationData):
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
    try:
        # 1. Gather all drugs for DDI Check
        check_list = []
        for item in data.prescription_items:
            # Handle Racikan ingredients
            if item.get('ingredients') and isinstance(item['ingredients'], list):
                for ing in item['ingredients']:
                    if isinstance(ing, dict) and 'name' in ing:
                        check_list.append(ing['name'])
            else:
                check_list.append(item['name'])

        # 2. Run Check
        ddi_results = await check_openfda_interactions(check_list)
        
        # Flatten warnings for saving to text field
        all_warnings = []
        if ddi_results["high"]:
            all_warnings.append("High Severity (Urgent Review):")
            all_warnings.extend([f" - {w}" for w in ddi_results["high"]])
        if ddi_results["medium"]:
            all_warnings.append("Moderate Severity (Caution):")
            all_warnings.extend([f" - {w}" for w in ddi_results["medium"]])
        if ddi_results["low"]:
            all_warnings.append("Low Severity (Advisory):")
            all_warnings.extend([f" - {w}" for w in ddi_results["low"]])

        # 3. Construct Record
        subjective_text = f"CC: {data.chief_complaint}\n\nHPI: {data.history_illness}"
        comorbidities = ", ".join(data.secondary_diagnoses) if data.secondary_diagnoses else "None"
        assessment_text = f"PRIMARY: {data.primary_diagnosis} [{data.icd10_code}]\nSECONDARY: {comorbidities}\nNOTES: {data.clinical_notes}"

        plan_text = data.therapy_instructions
        if all_warnings:
            plan_text += "\n\n[SYSTEM DDI ALERTS]\n" + "\n".join(all_warnings)

        consult_res = supabase.table("consultations").insert({
            "appointment_id": data.appointment_id,
            "doctor_id": data.doctor_id,
            "subjective": subjective_text,
            "objective": "Vitals in Triage",
            "assessment": assessment_text,
            "plan": plan_text,
            "prescription_raw_text": "; ".join([f"{d['name']} {d['dosage']}" for d in data.prescription_items])
        }).execute()
        
        consult_id = consult_res.data[0]['id']

        items_payload = []
        for item in data.prescription_items:
            dose_instr = f"{item['dosage']} {item['frequency']}"
            if item['name'] == "Compound (Racikan)" and 'ingredients' in item:
                 dose_instr += f" (Contains: {len(item['ingredients'])} items)"

            items_payload.append({
                "consultation_id": consult_id,
                "drug_name_snapshot": item['name'],
                "quantity": 10,
                "dosage_instruction": dose_instr,
                "status": "pending"
            })
        
        if items_payload: 
            supabase.table("prescription_items").insert(items_payload).execute()

        supabase.table("appointments").update({"status": "pharmacy"}).eq("id", data.appointment_id).execute()
        
        return {
            "status": "success", 
            "consultation_id": consult_id,
            "warnings": all_warnings # Return flattened list for simple frontend alert
        }
    except Exception as e:
        print(f"Submit Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ... (Standard Getters) ...
@app.get("/")
def read_root(): return {"status": "active"}

@app.get("/api/icd/search")
async def search_icd10(q: str):
    if not supabase: return []
    res = supabase.table("icd10_dictionary").select("code, symptoms").ilike("symptoms", f"%{q}%").limit(8).execute()
    return [{"code": r['code'], "description": r['symptoms']} for r in res.data]

@app.get("/doctor/appointment/{appt_id}")
async def get_appointment_detail(appt_id: str):
    res = supabase.table("appointments").select("*, patients(*), triage_notes(*)")\
        .eq("id", appt_id).single().execute()
    return res.data

@app.get("/doctor/queue")
async def get_doctor_queue(doctor_id: str):
    return supabase.table("appointments").select("*, patients(*), triage_notes(*)").eq("doctor_id", doctor_id).in_("status", ["scheduled", "checked_in", "triage", "consultation"]).order("queue_number").execute().data

@app.get("/patient/doctors")
async def get_all_doctors():
    return supabase.table("profiles").select("id, full_name, specialization").eq("role", "doctor").execute().data

@app.get("/patient/profile")
async def get_patient_profile(user_id: str):
    res = supabase.table("patients").select("*").eq("id", user_id).execute()
    return res.data[0] if res.data else {"mrn": "N/A"}

@app.post("/patient/book-appointment")
async def book_appointment(booking: AppointmentBooking):
    q_res = supabase.table("appointments").select("queue_number").eq("doctor_id", booking.doctor_id).eq("status", "scheduled").order("queue_number", desc=True).limit(1).execute()
    next_q = q_res.data[0]['queue_number'] + 1 if q_res.data else 1
    supabase.table("appointments").insert({
        "patient_id": booking.patient_id,
        "doctor_id": booking.doctor_id,
        "status": "scheduled",
        "queue_number": next_q,
        "scheduled_time": f"{booking.date}T{booking.time}:00"
    }).execute()
    return {"status": "success"}

@app.get("/patient/history")
async def get_patient_history(patient_id: str):
    if not supabase: return []
    try:
        # Step 1: Find appointment IDs for this patient
        # We query the appointments table first to get the IDs
        appts_res = supabase.table("appointments").select("id").eq("patient_id", patient_id).execute()
        if not appts_res.data:
            return []
            
        appt_ids = [a['id'] for a in appts_res.data]
        
        # Step 2: Fetch consultations linked to those appointment IDs
        # This avoids the complex nested join filtering that causes 500 errors
        consultations = supabase.table("consultations")\
            .select("*, doctors:profiles!doctor_id(full_name), appointments(scheduled_time)")\
            .in_("appointment_id", appt_ids)\
            .order("created_at", desc=True)\
            .execute()
            
        return consultations.data
    except Exception as e:
        print(f"History Error: {e}")
        return [] # Return empty list on error to prevent frontend crash
