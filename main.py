import os
import json
import re
from typing import List, Optional, Dict, Any
from datetime import datetime
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

# --- CONFIGURATION ---
SUPABASE_URL = "https://wasadrygnoevtkckqqrv.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Indhc2Fkcnlnbm9ldnRrY2txcXJ2Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2ODM5NzI4MCwiZXhwIjoyMDgzOTczMjgwfQ.TN83oe-OR0k9KzZRsVi23sRSjuLqemjAStTRDmAgR4I" 

app = FastAPI(title="Doctor's Module API", version="2.0")

# Enable CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://idn-cdss.vercel.app/"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Client
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"Supabase Init Error: {e}")
    print("Please check if your SUPABASE_KEY is a valid JWT (starts with 'eyJ...')")

# --- NER ENGINE (Your Logic) ---
try:
    from ner_parser import parse_prescription_text
    NER_AVAILABLE = True
except ImportError:
    NER_AVAILABLE = False
    print("WARNING: 'ner_parser.py' not found. Using Regex Fallback.")

def simple_regex_parser(text: str) -> List[Dict]:
    """Fallback parser if ML model is missing."""
    drugs = []
    raw_items = [x.strip() for x in text.split(';') if x.strip()]
    for item in raw_items:
        drug_data = {
            "drugName": item.split()[0] if item else "Unknown",
            "dosage": "Standard",
            "frequency": "1x1",
            "quantity": 10
        }
        # Attempt to extract dose (mg/g)
        dose_match = re.search(r'(\d+\s*(?:mg|g|ml|mcg|iu))', item, re.IGNORECASE)
        if dose_match: drug_data['dosage'] = dose_match.group(1)
        # Attempt to extract freq (3x1)
        freq_match = re.search(r'(\d+\s*[xX]\s*\d+)', item, re.IGNORECASE)
        if freq_match: drug_data['frequency'] = freq_match.group(0)
        
        drugs.append(drug_data)
    return drugs

# --- MODELS ---

class DrugCheckRequest(BaseModel):
    drug_name: str
    dosage: str
    frequency: str
    patient_id: Optional[str] = None
    existing_drugs: List[str] = [] # List of names already prescribed

class ConsultationSubmit(BaseModel):
    doctor_id: str
    appointment_id: str
    # SOAP
    subjective: str
    objective: str
    assessment: str
    plan: str
    # Prescription
    prescription_items: List[Dict[str, Any]] # List of {name, dosage, frequency, instructions}

# --- ENDPOINTS ---

@app.get("/doctor/queue")
async def get_queue(doctor_id: str = "default_doc"):
    """Fetches list of patients with status 'checked_in' or 'triage'"""
    # In production, filter by doctor_id. For now, show all waiting.
    try:
        # Fetch Appointments + Patient Info + Triage Notes
        response = supabase.table("appointments")\
            .select("id, status, queue_number, scheduled_time, patients(full_name, mrn, dob, gender), triage_notes(chief_complaint, systolic, diastolic)")\
            .in_("status", ["checked_in", "triage"])\
            .order("queue_number")\
            .execute()
        return response.data
    except Exception as e:
        print(f"Queue Error: {e}")
        return []

@app.get("/doctor/patient/{appointment_id}")
async def get_patient_details(appointment_id: str):
    """Fetches full details for the Consultation Page"""
    try:
        # 1. Get Appointment & Patient
        appt = supabase.table("appointments")\
            .select("*, patients(*), triage_notes(*)")\
            .eq("id", appointment_id)\
            .single()\
            .execute()
        
        if not appt.data:
            raise HTTPException(status_code=404, detail="Appointment not found")

        # 2. Get Medical History (Mock or Previous Consultations)
        history = supabase.table("consultations")\
            .select("created_at, assessment")\
            .eq("appointment_id", appointment_id) \
            .execute() # In real app, query by patient_id, not appointment_id
            
        return {
            "appointment": appt.data,
            "history": history.data or []
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/doctor/parse-text")
async def smart_parse(payload: dict):
    """
    Called when user types "Amox 500 3x1" in the Name field and hits Enter/Blur.
    Returns structured data to auto-fill the form.
    """
    raw_text = payload.get("text", "")
    if NER_AVAILABLE:
        try:
            parsed = parse_prescription_text(raw_text)
            # Flatten the structure (separate_drugs + racikan)
            items = parsed.get('separate_drugs', []) + parsed.get('racikan', [])
            if items:
                return items[0] # Return the first identified drug
        except:
            pass
    
    # Fallback
    parsed = simple_regex_parser(raw_text)
    return parsed[0] if parsed else {}

@app.post("/doctor/check-safety")
async def check_drug_safety(data: DrugCheckRequest):
    """
    Checks 1 Drug against:
    1. Knowledge Map (Fornas Compliance)
    2. Existing Drugs (OpenFDA DDI)
    """
    alerts = []
    compliance_flags = []
    
    # 1. Database Lookup (Compliance)
    # We search the knowledge map for the drug name
    db_drug = supabase.table("knowledge_map")\
        .select("*")\
        .text_search("fts", data.drug_name)\
        .limit(1)\
        .execute()

    matched_drug_contents = None
    
    if db_drug.data:
        match = db_drug.data[0]
        matched_drug_contents = match.get('openfda_term') # For DDI check
        
        # Check Fornas
        if match.get('fornas_rule'):
            rule = match['fornas_rule']
            # Simple check: Does rule exist?
            if rule:
                 compliance_flags.append({
                     "type": "fornas",
                     "msg": f"Fornas Limit: {rule.get('max_qty', 'N/A')}",
                     "severity": "warning"
                 })
    
    # 2. OpenFDA DDI Check (Mocked Logic for Reliability)
    # Check current drug against existing list
    current = matched_drug_contents or data.drug_name
    for existing in data.existing_drugs:
        # MOCK DDI LOGIC: Trigger on specific keywords for demo
        combo = f"{current} + {existing}".lower()
        if "aspirin" in combo and "warfarin" in combo:
            alerts.append({"severity": "high", "msg": "Critical Interaction: Bleeding Risk (Aspirin + Warfarin)"})
        if "simvastatin" in combo and "amlodipine" in combo:
            alerts.append({"severity": "moderate", "msg": "Interaction: Myopathy Risk (Simvastatin + Amlodipine)"})

    return {
        "is_safe": len(alerts) == 0,
        "alerts": alerts,
        "compliance": compliance_flags,
        "db_match": match.get('local_term') if db_drug.data else None
    }

@app.post("/doctor/submit-consultation")
async def submit_consultation(data: ConsultationSubmit):
    try:
        # 1. Save Consultation
        consult_res = supabase.table("consultations").insert({
            "appointment_id": data.appointment_id,
            "doctor_id": data.doctor_id,
            "subjective": data.subjective,
            "objective": data.objective,
            "assessment": data.assessment,
            "plan": data.plan,
            # Concatenate prescription for raw text backup
            "prescription_raw_text": "; ".join([f"{d['name']} {d['dosage']}" for d in data.prescription_items])
        }).execute()
        
        consult_id = consult_res.data[0]['id']

        # 2. Save Items
        items_payload = []
        for item in data.prescription_items:
            items_payload.append({
                "consultation_id": consult_id,
                "drug_name_snapshot": item['name'],
                "quantity": 10, # Default if not specified
                "dosage_instruction": f"{item['dosage']} {item['frequency']} - {item.get('instructions','')}",
                "status": "pending"
            })
        
        if items_payload:
            supabase.table("prescription_items").insert(items_payload).execute()

        # 3. Update Status
        supabase.table("appointments").update({"status": "pharmacy"}).eq("id", data.appointment_id).execute()

        return {"status": "success"}

    except Exception as e:
        print(e)

        raise HTTPException(status_code=500, detail="Submission Failed")

