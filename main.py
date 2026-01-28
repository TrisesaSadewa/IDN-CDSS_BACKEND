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

app = FastAPI(title="Smart HIS Backend", version="8.4 - DDI Calibrated")

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

# --- CURATED DDI KNOWLEDGE BASE (INDONESIAN STANDARD CALIBRATION) ---
KNOWN_INTERACTIONS = {
    # --- MAJOR (Severe / Avoid) ---
    frozenset(["aspirin", "ibuprofen"]): {
        "severity": "Major",
        "mechanism": "Pharmacodynamic Antagonism",
        "description": "Ibuprofen binds to COX-1 receptors, blocking Aspirin's access. This negates the cardioprotective (anti-platelet) effect of Aspirin, increasing stroke/MI risk.",
        "advice": "AVOID concurrent use if possible. If necessary, take Ibuprofen 8 hours before or 30 minutes after Aspirin. Prefer Paracetamol for pain."
    },
    frozenset(["acetylsalicylic acid", "ibuprofen"]): {
        "severity": "Major",
        "mechanism": "Pharmacodynamic Antagonism",
        "description": "Ibuprofen blocks the anti-platelet effect of Acetylsalicylic Acid (Aspirin), reducing heart protection.",
        "advice": "Take Ibuprofen 8 hours before Aspirin. Consider switching to Paracetamol."
    },

    # --- INTERMEDIATE (Moderate / Monitor) ---
    frozenset(["carvedilol", "ibuprofen"]): {
        "severity": "Moderate",
        "mechanism": "Antagonism of Antihypertensive Effect",
        "description": "NSAIDs (Ibuprofen) cause sodium/water retention and vasoconstriction, directly opposing the BP-lowering effect of Beta-blockers (Carvedilol).",
        "advice": "Monitor BP closely. If BP is uncontrolled, stop Ibuprofen or increase antihypertensive dose."
    },
    frozenset(["candesartan", "ibuprofen"]): {
        "severity": "Moderate",
        "mechanism": "Renal Impairment Risk",
        "description": "NSAIDs constrict afferent arterioles while ARBs (Candesartan) dilate efferent arterioles. Combined, they drastically drop glomerular filtration pressure.",
        "advice": "High risk of Acute Kidney Injury. Ensure hydration. Monitor Creatinine/Potassium."
    },

    # --- MINOR (Advisory / Pharmacokinetic) ---
    frozenset(["carvedilol", "sucralfate"]): {
        "severity": "Minor",
        "mechanism": "Reduced Absorption",
        "description": "Sucralfate coats the stomach lining and may physically bind to Carvedilol, reducing its absorption.",
        "advice": "Separate dosing. Take Carvedilol at least 2 hours before Sucralfate."
    },
    frozenset(["carvedilol", "aspirin"]): { 
        "severity": "Minor", 
        "mechanism": "Additive Hypotension/Bleeding (Low Risk)", 
        "description": "Generally a standard post-MI combination. Minimal interaction risk.", 
        "advice": "Routine monitoring." 
    },
    frozenset(["carvedilol", "acetylsalicylic acid"]): { 
        "severity": "Minor", 
        "mechanism": "Standard Therapy", 
        "description": "Standard combination for heart failure/CAD.", 
        "advice": "Routine monitoring." 
    },
    frozenset(["nitroglycerin", "aspirin"]): {
        "severity": "Minor",
        "mechanism": "Pharmacokinetic",
        "description": "Aspirin may increase serum concentrations of Nitroglycerin.",
        "advice": "Monitor for headache or hypotension."
    },
     frozenset(["acetylsalicylic acid", "nitroglycerin"]): {
        "severity": "Minor",
        "mechanism": "Pharmacokinetic",
        "description": "Aspirin may increase serum concentrations of Nitroglycerin.",
        "advice": "Monitor for headache or hypotension."
    },
    frozenset(["nitroglycerin", "omeprazole"]): {
        "severity": "Minor", 
        "mechanism": "pH Dependent Absorption",
        "description": "Dry mouth from nitrates might delay dissolution of sublingual tablets, but interaction is minimal.",
        "advice": "No specific action required."
    },
     frozenset(["sucralfate", "omeprazole"]): {
        "severity": "Minor", 
        "mechanism": "pH Dependent Activation",
        "description": "Sucralfate needs acid to activate. Omeprazole reduces acid.",
        "advice": "Take Sucralfate 1 hour before Omeprazole."
    },
    frozenset(["aspirin", "omeprazole"]): { 
        "severity": "Info", # Changed from Minor to Info to reduce alert fatigue
        "mechanism": "Therapeutic Pair", 
        "description": "Omeprazole is often prescribed to prevent Aspirin-induced ulcers.", 
        "advice": "Beneficial combination." 
    },
    frozenset(["acetylsalicylic acid", "omeprazole"]): { 
        "severity": "Info", 
        "mechanism": "Therapeutic Pair", 
        "description": "Omeprazole protects stomach from NSAID injury.", 
        "advice": "Beneficial combination." 
    },
}

# --- INTELLIGENT DDI CHECKER ---

def resolve_active_ingredients(drug_name: str) -> List[str]:
    """
    Resolves Brand Name to Active Ingredients List.
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
                cleaned = re.sub(r'\s*\d+.*$', '', cleaned) 
                if cleaned:
                    ingredients.append(cleaned)
            return ingredients

    return [clean_name]

async def check_openfda_interactions(drug_list: List[str]) -> List[Dict[str, Any]]:
    IGNORE_TERMS = {
        "tab", "tablet", "cap", "capsule", "caps", "inj", "injection", "injeksi",
        "syr", "syrup", "sirup", "susp", "suspension", "drops", "drop", "gtts",
        "ml", "mg", "g", "mcg", "iu", "unit", "amp", "vial", "btl", "tube", "sachet",
        "supp", "suppository", "kapsul", "bungkus", "puyer", "racikan", "compound",
        "cream", "krim", "oint", "ointment", "salep", "gel", "lotion",
        "spuit", "infus", "set", "kasa", "needle", "syringe", "disp",
        "acid", "sodium", "calcium", "potassium"
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
    results = []

    if len(check_items) < 2: return results

    pairs = list(combinations(check_items, 2))
    
    async with aiohttp.ClientSession() as session:
        for drug_a, drug_b in pairs:
            # 1. CHECK LOCAL KNOWLEDGE BASE FIRST
            pair_set = frozenset([drug_a.lower(), drug_b.lower()])
            
            if pair_set in KNOWN_INTERACTIONS:
                info = KNOWN_INTERACTIONS[pair_set]
                # Filter out 'Info' severity unless requested, or mark them clearly
                results.append({
                    "pair": [drug_a.title(), drug_b.title()],
                    "severity": info["severity"],
                    "mechanism": info.get("mechanism", "Pharmacodynamic"),
                    "description": info["description"],
                    "advice": info["advice"],
                    "source": "Clinical DB"
                })
                continue 

            # 2. CHECK OPENFDA (FALLBACK)
            query = f'patient.drug.medicinalproduct:("{drug_a}"+AND+"{drug_b}")'
            try:
                count_url = f"https://api.fda.gov/drug/event.json?search={query}&limit=1"
                async with session.get(count_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        total = data.get('meta', {}).get('results', {}).get('total', 0)
                        
                        if total > 1000:
                            results.append({
                                "pair": [drug_a.title(), drug_b.title()],
                                "severity": "Moderate",
                                "mechanism": "Statistical Signal (OpenFDA)",
                                "description": f"High co-occurrence in adverse event reports ({total} cases). Specific interaction mechanism not in local DB.",
                                "advice": "Review patient history. Check standard references.",
                                "source": "OpenFDA"
                            })
            except Exception as e:
                print(f"OpenFDA Error: {e}")
                
    return results

# --- ENDPOINTS ---

@app.post("/api/check-ddi")
async def check_ddi_endpoint(payload: DDIRequest):
    print(f"Checking DDI for: {payload.drugs}")
    interaction_list = await check_openfda_interactions(payload.drugs)
    
    severity_order = {"Major": 1, "Moderate": 2, "Minor": 3, "Info": 4}
    interaction_list.sort(key=lambda x: severity_order.get(x["severity"], 99))
    
    # Only consider it "Unsafe" if there are Major or Moderate interactions
    is_safe = not any(i["severity"] in ["Major", "Moderate"] for i in interaction_list)
    return {"interactions": interaction_list, "safe": is_safe}

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
        check_list = []
        for item in data.prescription_items:
            if item.get('ingredients') and isinstance(item['ingredients'], list):
                for ing in item['ingredients']:
                    if isinstance(ing, dict) and 'name' in ing:
                        check_list.append(ing['name'])
            else:
                check_list.append(item['name'])

        interactions = await check_openfda_interactions(check_list)
        
        all_warnings = []
        for i in interactions:
            # Skip Info logs in persistent record to reduce noise
            if i['severity'] != 'Info':
                all_warnings.append(f"[{i['severity'].upper()}] {i['pair'][0]} + {i['pair'][1]}: {i['description']}")

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
            "interactions": interactions
        }
    except Exception as e:
        print(f"Submit Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ... (Standard Getters - Kept for file completeness) ...
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
        appts_res = supabase.table("appointments").select("id").eq("patient_id", patient_id).execute()
        if not appts_res.data: return []
        appt_ids = [a['id'] for a in appts_res.data]
        consultations = supabase.table("consultations").select("*, doctors:profiles!doctor_id(full_name), appointments(scheduled_time)").in_("appointment_id", appt_ids).order("created_at", desc=True).execute()
        return consultations.data
    except Exception as e:
        print(f"History Error: {e}")
        return []
