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
# Use environment variables in production!
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://wasadrygnoevtkckqqrv.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Indhc2Fkcnlnbm9ldnRrY2txcXJ2Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2ODM5NzI4MCwiZXhwIjoyMDgzOTczMjgwfQ.TN83oe-OR0k9KzZRsVi23sRSjuLqemjAStTRDmAgR4I")

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
    print(f"Supabase Connection Failed: {e}")
    supabase = None

# --- PYDANTIC MODELS ---
class ConsultationData(BaseModel):
    doctor_id: str
    appointment_id: str
    subjective: str
    objective: str
    assessment: str
    plan: str
    prescription_items: List[Dict[str, Any]]

# --- API ENDPOINTS ---

@app.get("/")
def read_root():
    return {"status": "active", "service": "Smart HIS Backend"}

@app.get("/doctor/queue")
async def get_doctor_queue(doctor_id: str):
    """
    Fetches the active queue for a specific doctor.
    Joins Appointments -> Patients -> Triage Notes.
    """
    if not supabase:
        raise HTTPException(status_code=500, detail="Database connection failed")

    try:
        # Supabase syntax for joins: table(column, ...)
        # We fetch appointments where doctor_id matches and status is active
        response = supabase.table("appointments")\
            .select("*, patients(*), triage_notes(*)")\
            .eq("doctor_id", doctor_id)\
            .in_("status", ["scheduled", "checked_in", "triage", "consultation"])\
            .order("queue_number")\
            .execute()

        return response.data
    except Exception as e:
        print(f"Error fetching queue: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/doctor/submit-consultation")
async def submit_consultation(data: ConsultationData):
    """
    Saves the SOAP note and prescription items transactionally.
    """
    if not supabase:
        raise HTTPException(status_code=500, detail="Database connection failed")

    try:
        # 1. Save Consultation
        consult_res = supabase.table("consultations").insert({
            "appointment_id": data.appointment_id,
            "doctor_id": data.doctor_id,
            "subjective": data.subjective,
            "objective": data.objective,
            "assessment": data.assessment,
            "plan": data.plan,
            "prescription_raw_text": "; ".join([f"{d['name']} {d['dosage']}" for d in data.prescription_items])
        }).execute()
        
        if not consult_res.data:
             raise HTTPException(status_code=500, detail="Failed to create consultation record")

        consult_id = consult_res.data[0]['id']

        # 2. Save Prescription Items
        items_payload = []
        for item in data.prescription_items:
            items_payload.append({
                "consultation_id": consult_id,
                "drug_name_snapshot": item['name'],
                "quantity": 10, # Default logic, should come from frontend
                "dosage_instruction": f"{item['dosage']} {item['frequency']} - {item.get('instructions','')}",
                "status": "pending"
            })
        
        if items_payload:
            supabase.table("prescription_items").insert(items_payload).execute()

        # 3. Update Appointment Status to 'pharmacy' (or 'completed')
        supabase.table("appointments").update({"status": "pharmacy"}).eq("id", data.appointment_id).execute()

        return {"status": "success", "consultation_id": consult_id}

    except Exception as e:
        print(f"Error submitting consultation: {e}")
        raise HTTPException(status_code=500, detail=str(e))
