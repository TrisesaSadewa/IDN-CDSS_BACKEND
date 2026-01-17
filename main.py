import os
import json
import re
import aiohttp # Requires: pip install aiohttp
from typing import List, Optional, Dict, Any
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

# --- IMPORT LOCAL PARSER ---
try:
    import ner_parser
    print("SUCCESS: NER Parser loaded.")
except ImportError as e:
    print(f"WARNING: ner_parser.py not found: {e}")
    ner_parser = None

# --- CONFIGURATION ---
SUPABASE_URL = "https://crywwqleinnwoacithmw.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNyeXd3cWxlaW5ud29hY2l0aG13Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2ODQwODgxMiwiZXhwIjoyMDgzOTg0ODEyfQ.Uk9AFwxRHi7pwgP_lqYIWQ6JD7Ov1d07OzxiHswPNPQ"

app = FastAPI(title="Smart HIS Backend", version="5.2 - OpenFDA DDI")

# --- CORS CONFIGURATION ---
origins = [
    "https://idn-cdss.vercel.app",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "*"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins, 
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

# --- OPENFDA HELPER ---
async def check_openfda_interactions(drug_list: List[str]) -> List[str]:
    """
    Checks OpenFDA for adverse events where the listed drugs are concomitant.
    This is a simplified check for co-occurrence in adverse event reports, 
    which often signals an interaction.
    """
    if len(drug_list) < 2:
        return []

    warnings = []
    # OpenFDA allows searching for drug interactions via the adverse event endpoint
    # Query logic: patient.drug.medicinalproduct:(DrugA+AND+DrugB)
    # We check pairs to keep it simple and effective
    
    async with aiohttp.ClientSession() as session:
        for i in range(len(drug_list)):
            for j in range(i + 1, len(drug_list)):
                drug_a = drug_list[i].split()[0] # Take first word (brand/generic)
                drug_b = drug_list[j].split()[0]
                
                query = f'patient.drug.medicinalproduct:("{drug_a}"+AND+"{drug_b}")'
                url = f"https://api.fda.gov/drug/event.json?search={query}&limit=1"
                
                try:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get('meta', {}).get('results', {}).get('total', 0) > 100: # Threshold for significance
                                warnings.append(f"Potential Interaction: {drug_a} + {drug_b} (Found in {data['meta']['results']['total']} FDA reports)")
                except Exception as e:
                    print(f"OpenFDA Error for {drug_a}+{drug_b}: {e}")
                    
    return warnings

# --- ENDPOINTS ---

@app.post("/api/check-ddi")
async def check_ddi_endpoint(payload: DDIRequest):
    """
    Dedicated endpoint to check Drug-Drug Interactions
    """
    warnings = await check_openfda_interactions(payload.drugs)
    return {"warnings": warnings, "safe": len(warnings) == 0}

@app.post("/api/parse-prescription")
async def parse_prescription_endpoint(payload: ParseRequest):
    if not ner_parser:
        raise HTTPException(status_code=500, detail="NER Parser module not loaded on server.")
    try:
        parsed_data = ner_parser.parse_prescription_text(payload.text)
        return parsed_data
    except Exception as e:
        print(f"Parsing Error: {e}")
        raise HTTPException(status_code=500, detail=f"Parsing failed: {str(e)}")

@app.get("/")
def read_root():
    return {"status": "active", "service": "Smart HIS Backend"}

@app.get("/api/icd/search")
async def search_icd10(q: str):
    if not supabase: return []
    try:
        res = supabase.table("icd10_dictionary").select("code, symptoms").ilike("symptoms", f"%{q}%").limit(8).execute()
        return [{"code": r['code'], "description": r['symptoms']} for r in res.data]
    except Exception: return []

@app.get("/doctor/appointment/{appt_id}")
async def get_appointment_detail(appt_id: str):
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
    try:
        res = supabase.table("appointments").select("*, patients(*), triage_notes(*)")\
            .eq("id", appt_id)\
            .single()\
            .execute()
        return res.data
    except Exception as e:
        if "JSON object must be str" in str(e) or "0 rows" in str(e):
             raise HTTPException(status_code=404, detail="Appointment not found")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/doctor/submit-consultation")
async def submit_consultation(data: ConsultationData):
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
    try:
        # OPTIONAL: Perform DDI Check server-side before saving
        # drug_names = [d['name'] for d in data.prescription_items]
        # ddi_warnings = await check_openfda_interactions(drug_names)
        # Note: Usually we warn frontend first, but we can log warnings here if needed.

        subjective_text = f"CC: {data.chief_complaint}\n\nHPI: {data.history_illness}"
        comorbidities = ", ".join(data.secondary_diagnoses) if data.secondary_diagnoses else "None"
        assessment_text = f"PRIMARY: {data.primary_diagnosis} [{data.icd10_code}]\nSECONDARY: {comorbidities}\nNOTES: {data.clinical_notes}"

        consult_res = supabase.table("consultations").insert({
            "appointment_id": data.appointment_id,
            "doctor_id": data.doctor_id,
            "subjective": subjective_text,
            "objective": "Vitals in Triage",
            "assessment": assessment_text,
            "plan": data.therapy_instructions,
            "prescription_raw_text": "; ".join([f"{d['name']} {d['dosage']}" for d in data.prescription_items])
        }).execute()
        
        if not consult_res.data: raise Exception("Insert failed")
        consult_id = consult_res.data[0]['id']

        items_payload = []
        for item in data.prescription_items:
            items_payload.append({
                "consultation_id": consult_id,
                "drug_name_snapshot": item['name'],
                "quantity": 10,
                "dosage_instruction": f"{item['dosage']} {item['frequency']}",
                "status": "pending"
            })
        if items_payload: supabase.table("prescription_items").insert(items_payload).execute()

        supabase.table("appointments").update({"status": "pharmacy"}).eq("id", data.appointment_id).execute()
        return {"status": "success", "consultation_id": consult_id}
    except Exception as e:
        print(f"Submit Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/doctor/queue")
async def get_doctor_queue(doctor_id: str):
    if not supabase: return []
    try:
        return supabase.table("appointments").select("*, patients(*), triage_notes(*)").eq("doctor_id", doctor_id).in_("status", ["scheduled", "checked_in", "triage", "consultation"]).order("queue_number").execute().data
    except Exception: return []

@app.get("/patient/doctors")
async def get_all_doctors():
    if not supabase: return []
    return supabase.table("profiles").select("id, full_name, specialization").eq("role", "doctor").execute().data

@app.get("/patient/profile")
async def get_patient_profile(user_id: str):
    if not supabase: return {}
    res = supabase.table("patients").select("*").eq("id", user_id).execute()
    return res.data[0] if res.data else {"mrn": "N/A"}

@app.post("/patient/book-appointment")
async def book_appointment(booking: AppointmentBooking):
    if not supabase: raise HTTPException(status_code=500)
    try:
        q_res = supabase.table("appointments").select("queue_number").eq("doctor_id", booking.doctor_id).eq("status", "scheduled").order("queue_number", desc=True).limit(1).execute()
        next_q = 1
        if q_res.data: next_q = q_res.data[0]['queue_number'] + 1
        supabase.table("appointments").insert({
            "patient_id": booking.patient_id,
            "doctor_id": booking.doctor_id,
            "status": "scheduled",
            "queue_number": next_q,
            "scheduled_time": f"{booking.date}T{booking.time}:00"
        }).execute()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/patient/history")
async def get_patient_history(patient_id: str):
    if not supabase: return []
    try:
        return supabase.table("consultations")\
            .select("*, doctors:profiles!doctor_id(full_name), appointments(scheduled_time)")\
            .eq("appointments.patient_id", patient_id)\
            .order("created_at", desc=True)\
            .execute().data
    except Exception as e:
        print(f"History Fetch Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
