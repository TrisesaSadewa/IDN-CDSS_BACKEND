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
# 1. SUPABASE CREDENTIALS (SERVICE_ROLE KEY for Backend)
SUPABASE_URL = "https://crywwqleinnwoacithmw.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNyeXd3cWxlaW5ud29hY2l0aG13Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2ODQwODgxMiwiZXhwIjoyMDgzOTg0ODEyfQ.Uk9AFwxRHi7pwgP_lqYIWQ6JD7Ov1d07OzxiHswPNPQ"

app = FastAPI(title="Smart HIS Backend", version="3.0 - Prod")

# 2. CORS SETTINGS (Allowing all for smooth deployment testing)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Supabase Client
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

class AppointmentBooking(BaseModel):
    patient_id: str
    doctor_id: str
    date: str
    time: str

# --- ENDPOINTS ---

@app.get("/")
def read_root():
    return {"status": "active", "service": "Smart HIS Backend", "db": "Connected" if supabase else "Disconnected"}

@app.get("/patient/profile")
async def get_patient_profile(user_id: str):
    """Fetches MRN and details for the dashboard"""
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
    try:
        res = supabase.table("patients").select("*").eq("id", user_id).execute()
        if res.data and len(res.data) > 0:
            return res.data[0]
        else:
            return {"full_name": "Profile Error", "mrn": "MISSING_ROW"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/patient/doctors")
async def get_all_doctors():
    """Fetches list of doctors for booking"""
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
    try:
        # Fetch profiles with role 'doctor'
        res = supabase.table("profiles").select("id, full_name, specialization").eq("role", "doctor").execute()
        return res.data
    except Exception as e:
        print(f"Fetch Doctors Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/patient/book-appointment")
async def book_appointment(booking: AppointmentBooking):
    """Allows patient to book, syncing with Doctor's Queue"""
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
    try:
        # 1. Calculate Queue Number
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

@app.get("/doctor/queue")
async def get_doctor_queue(doctor_id: str):
    """Fetches Doctor's Schedule"""
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
    try:
        response = supabase.table("appointments")\
            .select("*, patients(*), triage_notes(*)")\
            .eq("doctor_id", doctor_id)\
            .in_("status", ["scheduled", "checked_in", "triage", "consultation"])\
            .order("queue_number")\
            .execute()
        return response.data
    except Exception as e:
        print(f"Queue Error: {e}")
        return []

@app.post("/doctor/submit-consultation")
async def submit_consultation(data: ConsultationData):
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
    try:
        # Transactional insert logic
        consult_res = supabase.table("consultations").insert({
            "appointment_id": data.appointment_id,
            "doctor_id": data.doctor_id,
            "subjective": data.subjective,
            "objective": data.objective,
            "assessment": data.assessment,
            "plan": data.plan,
            "prescription_raw_text": "; ".join([f"{d['name']} {d['dosage']}" for d in data.prescription_items])
        }).execute()
        
        if not consult_res.data: raise Exception("Consultation insert failed")
        consult_id = consult_res.data[0]['id']

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

        supabase.table("appointments").update({"status": "pharmacy"}).eq("id", data.appointment_id).execute()
        return {"status": "success", "consultation_id": consult_id}
    except Exception as e:
        print(f"Consultation Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/patient/history")
async def get_patient_history(patient_id: str):
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
    try:
        res = supabase.table("consultations")\
            .select("*, doctors:profiles!doctor_id(full_name), appointments(scheduled_time), prescription_items(*)")\
            .eq("appointments.patient_id", patient_id)\
            .execute()
        return res.data
    except Exception as e:
        print(f"History Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
