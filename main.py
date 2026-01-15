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

app = FastAPI(title="Smart HIS Backend", version="2.5")

# Enable CORS
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

# --- MODELS ---
class ConsultationData(BaseModel):
    doctor_id: str
    appointment_id: str
    subjective: str
    objective: str
    assessment: str
    plan: str
    prescription_items: List[Dict[str, Any]]

class AppointmentBooking(BaseModel):
    patient_id: str
    doctor_id: str
    date: str
    time: str
    
class PatientRegistration(BaseModel):
    email: str
    password: str
    full_name: str
    nik: str
    dob: str
    gender: str

# --- DOCTOR ENDPOINTS ---

@app.get("/doctor/queue")
async def get_doctor_queue(doctor_id: str):
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
    try:
        # Fetch active appointments
        response = supabase.table("appointments")\
            .select("*, patients(*), triage_notes(*)")\
            .eq("doctor_id", doctor_id)\
            .in_("status", ["scheduled", "checked_in", "triage", "consultation"])\
            .order("queue_number")\
            .execute()
        return response.data
    except Exception as e:
        print(f"Queue Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/doctor/submit-consultation")
async def submit_consultation(data: ConsultationData):
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
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
        
        consult_id = consult_res.data[0]['id']

        # 2. Save Prescription Items
        items_payload = []
        for item in data.prescription_items:
            items_payload.append({
                "consultation_id": consult_id,
                "drug_name_snapshot": item['name'],
                "quantity": 10,
                "dosage_instruction": f"{item['dosage']} {item['frequency']} - {item.get('instructions','')}",
                "status": "pending"
            })
        
        if items_payload:
            supabase.table("prescription_items").insert(items_payload).execute()

        # 3. Update Status
        supabase.table("appointments").update({"status": "pharmacy"}).eq("id", data.appointment_id).execute()

        return {"status": "success", "consultation_id": consult_id}
    except Exception as e:
        print(f"Consultation Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- PATIENT ENDPOINTS ---

@app.get("/patient/doctors")
async def get_all_doctors():
    """Returns list of doctors for booking dropdown"""
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
    try:
        # Query profiles where role is doctor
        res = supabase.table("profiles").select("id, full_name, specialization").eq("role", "doctor").execute()
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/patient/book-appointment")
async def book_appointment(booking: AppointmentBooking):
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
    try:
        # 1. Calculate Queue Number (Simple logic: Max + 1)
        # Note: In production, filter by date too.
        q_res = supabase.table("appointments")\
            .select("queue_number")\
            .eq("doctor_id", booking.doctor_id)\
            .eq("status", "scheduled")\
            .order("queue_number", desc=True)\
            .limit(1)\
            .execute()
        
        next_q = 1
        if q_res.data:
            next_q = q_res.data[0]['queue_number'] + 1

        # 2. Insert Appointment
        # Combine date and time into timestamp
        scheduled_ts = f"{booking.date}T{booking.time}:00"

        new_appt = supabase.table("appointments").insert({
            "patient_id": booking.patient_id,
            "doctor_id": booking.doctor_id,
            "status": "scheduled",
            "queue_number": next_q,
            "scheduled_time": scheduled_ts
        }).execute()
        
        return {"status": "success", "data": new_appt.data}
    except Exception as e:
        print(f"Booking Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/patient/history")
async def get_patient_history(patient_id: str):
    """Fetches past consultations for EMR view"""
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
    try:
        # Join consultations -> doctors
        # Also need appointment date
        res = supabase.table("consultations")\
            .select("*, doctors:profiles!doctor_id(full_name), appointments(scheduled_time), prescription_items(*)")\
            .eq("appointments.patient_id", patient_id)\
            .execute()
            
        # Filter out rows where appointment might be null (if using inner join logic manually)
        # Supabase returns the structure, we just pass it to frontend to parse
        return res.data
    except Exception as e:
        print(f"History Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
