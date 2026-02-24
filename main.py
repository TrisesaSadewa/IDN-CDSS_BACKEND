import os
import json
import re
from typing import List, Optional, Dict, Any
from itertools import combinations
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
import aiohttp
import asyncio
import datetime

# --- IMPORT LOCAL MODULES ---
ner_engine = None
structured_drug_db = None

try:
    import ner_parser
    ner_engine = ner_parser.parser 
    import structured_drug_db
    print("SUCCESS: Local modules loaded.")
except ImportError as e:
    print(f"WARNING: Modules not found: {e}")

# --- CONFIGURATION ---
SUPABASE_URL = "https://crywwqleinnwoacithmw.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNyeXd3cWxlaW5ud29hY2l0aG13Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2ODQwODgxMiwiZXhwIjoyMDgzOTg0ODEyfQ.Uk9AFwxRHi7pwgP_lqYIWQ6JD7Ov1d07OzxiHswPNPQ"

app = FastAPI(title="Smart HIS Backend", version="11.4 - Master Dose Extraction")

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

class AlternativeRequest(BaseModel):
    drug_to_replace: str
    interacting_with: str

class TriageData(BaseModel):
    appointment_id: str
    weight_kg: Optional[float] = None
    height_cm: Optional[float] = None
    systolic: Optional[int] = None
    diastolic: Optional[int] = None
    temperature: Optional[float] = None
    heart_rate: Optional[int] = None
    respiratory_rate: Optional[int] = None
    spo2: Optional[int] = None
    pain_score: Optional[int] = None
    pain_location: Optional[str] = None
    chief_complaint: Optional[str] = None

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

class BookingRequest(BaseModel):
    patient_id: str
    doctor_id: str
    date: str
    time: str

class StaffCreateRequest(BaseModel):
    name: str
    email: str
    password: str
    role: str
    specialization: Optional[str] = None

class PatientCreateRequest(BaseModel):
    email: str
    password: str
    name: str
    dob: str
    gender: str
    nik: str
    phone_number: str
    address: str
    allergies: Optional[str] = None
    insurance_provider: Optional[str] = None
    insurance_number: Optional[str] = None
    insurance_coverage_limit: Optional[float] = None
    insurance_plan_type: Optional[str] = None
    emergency_name: Optional[str] = None
    emergency_relationship: Optional[str] = None
    emergency_phone: Optional[str] = None
    consent_data_processing: Optional[bool] = True
    consent_notifications: Optional[bool] = False


# DATABASE RULE LOOKUPS (Dynamically Fetched)
# --- HELPERS ---
def get_drug_info(drug_name: str):
    if not drug_name: return ("unknown", "unknown")
    
    clean_name = drug_name.replace("ANS ", "").lower().strip()
    clean_name = re.sub(r'\s+\d+.*$', '', clean_name).strip() 
    
    # 1. Broad Overrides for exact clinical alignment
    if "notisil" in clean_name or "warfarin" in clean_name: return ("warfarin", "anticoagulant")
    if "clopidogrel" in clean_name: return ("clopidogrel", "antiplatelet")
    if "spironolacton" in clean_name: return ("spironolactone", "k_sparing_diuretic")
    if "furosemide" in clean_name or "furosemid" in clean_name: return ("furosemide", "loop_diuretic")
    if "digoxin" in clean_name: return ("digoxin", "cardiac_glycoside")
    if "v-bloc" in clean_name or "carvedilol" in clean_name: return ("carvedilol", "beta-blocker")
    if "methyl" in clean_name and "prednisolon" in clean_name: return ("methylprednisolone", "corticosteroid")
    if "meloxicam" in clean_name: return ("meloxicam", "nsaid")
    if "candesartan" in clean_name: return ("candesartan", "arb")
    if "cetirizine" in clean_name: return ("cetirizine", "antihistamine")
    if "metformin" in clean_name: return ("metformin", "biguanide")
    if "glyburide" in clean_name or "glibenclamide" in clean_name or "glybenclamide" in clean_name: return ("glibenclamide", "sulfonylurea")
    if "glimepiride" in clean_name: return ("glimepiride", "sulfonylurea")
    if "gliclazide" in clean_name: return ("gliclazide", "sulfonylurea")
    if "fenofibrate" in clean_name: return ("fenofibrate", "fibrate")
    if "bicarbonas" in clean_name or "bicarbonate" in clean_name: return ("sodium bicarbonate", "alkalinizing_agent")
    if "gabapentin" in clean_name: return ("gabapentin", "gabapentinoid")
    if "captopril" in clean_name: return ("captopril", "ace-inhibitor")
    if "nifedipine" in clean_name or "amlodipin" in clean_name: return ("ccb", "ccb")
    if "simvastatin" in clean_name: return ("simvastatin", "statin")
    if "humalog" in clean_name or "insulin" in clean_name: return ("insulin", "insulin")
    if "obh" in clean_name: return ("obh", "antitussive")
    
    if "omega" in clean_name or "fish oil" in clean_name: return ("omega-3", "supplement")
    if "levothyroxin" in clean_name or "thyrax" in clean_name: return ("levothyroxine", "thyroid")
    if "methotrexate" in clean_name or "mtx" in clean_name: return ("methotrexate", "antineoplastic")
    if "rifampicin" in clean_name or "rifampin" in clean_name or "rimstar" in clean_name: return ("rifampicin", "antituberculosis")
    if "ketoconazole" in clean_name or "fluconazole" in clean_name or "itraconazole" in clean_name: return ("azole", "antifungal")
    if "acyclovir" in clean_name or "asiklovir" in clean_name: return ("acyclovir", "antiviral")
    if "enalapril" in clean_name or "lisinopril" in clean_name or "ramipril" in clean_name: return ("ace-inhibitor", "ace-inhibitor")
    if "losartan" in clean_name or "valsartan" in clean_name or "irbesartan" in clean_name: return ("arb", "arb")
    if "amlodipin" in clean_name or "diltiazem" in clean_name or "verapamil" in clean_name: return ("ccb", "ccb")
    if "hydrochlorothiazide" in clean_name or "hct" in clean_name: return ("hct", "diuretic")
    if "furosemid" in clean_name: return ("furosemide", "diuretic")
    if "prednison" in clean_name: return ("prednisone", "corticosteroid")
    if "alprazolam" in clean_name or "diazepam" in clean_name: return ("benzodiazepine", "sedative_hypnotic")
    if "sertraline" in clean_name or "fluoxetine" in clean_name: return ("ssri", "psychotropic")
    if "paracetamol" in clean_name or "panadol" in clean_name: return ("paracetamol", "analgesic")
    if "antacid" in clean_name or "promag" in clean_name or "mylanta" in clean_name: return ("antacid", "antacid")
    if "alendronate" in clean_name: return ("alendronate", "bisphosphonate")
    
    # Excipients, GI, Antivertigo & Antibiotics Overrides
    if "sirplus" in clean_name or "syrplus" in clean_name: return ("sirplus", "pharmaceutical_excipient")
    if "l-bio" in clean_name or "lacto-b" in clean_name: return ("probiotic", "probiotic")
    if "betahistin" in clean_name or "merislon" in clean_name: return ("betahistine", "antivertigo")
    if "ondansetron" in clean_name: return ("ondansetron", "antiemetic")
    if "vosedon" in clean_name or "domperidon" in clean_name: return ("domperidone", "antiemetic")
    if "diagit" in clean_name: return ("attapulgite", "antidiarrheal")
    if "spasminal" in clean_name: return ("hyoscine", "antispasmodic")
    if "santagesik" in clean_name: return ("metamizole", "analgesic")
    if "sanprima" in clean_name: return ("cotrimoxazole", "antibiotic")
    if "lopamid" in clean_name or "loperamid" in clean_name: return ("loperamide", "antidiarrheal")
    if "ranitidine" in clean_name: return ("ranitidine", "h2-blocker")
    if "amoxsan" in clean_name or "amoxicillin" in clean_name: return ("amoxicillin", "antibiotic")
    if "tremenza" in clean_name: return ("pseudoephedrine", "decongestant")
    if "lasal" in clean_name: return ("salbutamol", "bronchodilator")
    if "trilac" in clean_name: return ("triamcinolone", "corticosteroid")
    if "zinc" in clean_name: return ("zinc", "supplement")
    if "cobazym" in clean_name: return ("cobamamide", "supplement")
    
    # General Safeties
    if "aspirin" in clean_name or "aspilet" in clean_name or "nospirinal" in clean_name: return ("acetylsalicylic acid", "antiplatelet")
    if "ibuprofen" in clean_name: return ("ibuprofen", "nsaid")
    if "omeprazole" in clean_name or "lanzoprazole" in clean_name: return ("ppi", "ppi")
    if "sucralfate" in clean_name: return ("sucralfate", "mucosal-protective")
    if "nitro" in clean_name or "isdn" in clean_name: return ("nitroglycerin", "nitrate")
    if "phenitoin" in clean_name or "phenytoin" in clean_name: return ("phenytoin", "anticonvulsant")
    if "folat" in clean_name or "folic" in clean_name: return ("folic acid", "folate")
    
    # DB Lookup
    if structured_drug_db and hasattr(structured_drug_db, 'DRUG_INDEX'):
        drug_obj = structured_drug_db.DRUG_INDEX.get(clean_name)
        if drug_obj and drug_obj.drug_class and drug_obj.drug_class.lower() != "unknown":
            return (drug_obj.generic_name.lower(), drug_obj.drug_class.lower())

    return (clean_name, "unknown")


async def get_fda_interaction_warning(drug_name: str, drug_target: str) -> Optional[str]:
    """Queries OpenFDA for drug-specific interaction warnings."""
    if not drug_name or drug_name == "unknown": return None
    
    url = f"https://api.fda.gov/drug/label.json?search=drug_interactions:{drug_name}&limit=1"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    if "results" in data:
                        label = data["results"][0]
                        interaction_text = label.get("drug_interactions", [""])[0]
                        
                        # Match target drug name in the text
                        if drug_target.lower() in interaction_text.lower():
                            # Extract a snippet around the target drug for brevity
                            pattern = rf'[^.]*?{re.escape(drug_target)}[^.]*\.'
                            match = re.search(pattern, interaction_text, re.IGNORECASE)
                            if match:
                                return match.group(0).strip()
                            return interaction_text[:300] + "..."
    except Exception as e:
        print(f"FDA API Error for {drug_name}: {e}")
    return None


def extract_frequency(text: str) -> str:
    """Robust extraction of frequencies, capturing # and : delimiters."""
    if not text: return "1 x 1"
    
    # Splits on either : or #
    parts = re.split(r'[:#]', text)
    if len(parts) >= 3:
        sig_part = parts[-1].strip()
        # Accept valid signature lengths gracefully
        if len(sig_part) >= 2:
            return sig_part
            
    # Fallback RegExes
    m = re.search(r'\d+\s*(?:tab|tablet|pulv|bungkus)?\s*/\s*BAB', text, re.IGNORECASE)
    if m: return m.group(0).strip()
    m = re.search(r'(?:s\s*)?\d+\s*dd\s*(?:[a-zA-Z]+\s+)?\d+(?:/\d+)?(?:[.,]\d+)?(?:\s*[a-zA-Z]+)?', text, re.IGNORECASE)
    if m: return m.group(0).strip()
    m = re.search(r'\d+\s*[xX]\s*\d+(?:/\d+)?(?:[.,]\d+)?(?:\s*[a-zA-Z]+)?', text, re.IGNORECASE)
    if m: return m.group(0).strip()
    m = re.search(r'\d+-\d+-\d+', text)
    if m: return m.group(0).strip()
    m = re.search(r'\b(imm|prn|k/p|k\.p)\b', text, re.IGNORECASE)
    if m: return m.group(0).strip()
    
    return "1 x 1"


def extract_dose_text(text: str) -> Optional[str]:
    """Upgraded dosage extraction capable of finding fractionated prescriptions."""
    if not text: return None
    
    # Isolate the drug name segment
    text_to_search = re.split(r'[:#]', text)[0]
        
    # Match standard doses and fractions (1/4 tablet, 80 MG)
    match1 = re.search(r'(\d+/\d+|\d+[.,]\d+|\d+)\s*(mg|g|mcg|ml|cc|iu|tab|tablet|tabs|sach|sachet|bungkus|pulv|cap|kapsul|capsule|drop|drops)\b', text_to_search, re.IGNORECASE)
    if match1:
        return f"{match1.group(1)} {match1.group(2).lower()}"
        
    # Match reverse fractions (sach 1/2)
    match2 = re.search(r'\b(tab|tablet|tabs|sach|sachet|bungkus|pulv|cap|kapsul|capsule|drop|drops)\s+(\d+/\d+|\d+[.,]\d+|\d+)', text_to_search, re.IGNORECASE)
    if match2:
        return f"{match2.group(2)} {match2.group(1).lower()}"
        
    # Deep Fallback: If not found in the name, scan the entire raw line (for things like "3 dd 1 ml")
    match3 = re.search(r'(\d+/\d+|\d+[.,]\d+|\d+)\s*(mg|g|mcg|ml|cc|iu|tab|tablet|tabs|sach|sachet|bungkus|pulv|cap|kapsul|capsule|drop|drops)\b', text, re.IGNORECASE)
    if match3:
        return f"{match3.group(1)} {match3.group(2).lower()}"
        
    match4 = re.search(r'\b(tab|tablet|tabs|sach|sachet|bungkus|pulv|cap|kapsul|capsule|drop|drops)\s+(\d+/\d+|\d+[.,]\d+|\d+)', text, re.IGNORECASE)
    if match4:
        return f"{match4.group(2)} {match4.group(1).lower()}"
        
    return None

# --- ENDPOINTS ---

@app.post("/api/suggest-alternative")
async def suggest_alternative(payload: AlternativeRequest):
    if not supabase: return {"alternatives": []}
    
    gen_a, class_a = get_drug_info(payload.drug_to_replace)
    gen_b, class_b = get_drug_info(payload.interacting_with)
    
    res_alt = supabase.table("therapeutic_alternatives").select("alternative_class").eq("target_class", class_a).order("priority").execute()
    candidate_classes = [r["alternative_class"] for r in res_alt.data] if res_alt.data else []
    
    if not candidate_classes:
        return {"alternatives": []}
        
    safe_classes = []
    
    for cand_class in candidate_classes:
        c1, c2 = sorted([cand_class, class_b])
        rule_res = supabase.table("ddi_rules").select("*").eq("class_a", c1).eq("class_b", c2).execute()
        
        if not rule_res.data:
            safe_classes.append(cand_class)
            
    suggestions = []
    
    if structured_drug_db and hasattr(structured_drug_db, 'DRUGS'):
        for safe_c in safe_classes:
            found = 0
            for drug in structured_drug_db.DRUGS:
                if drug.drug_class.lower() == safe_c and drug.generic_name and drug.generic_name.lower() != "unknown":
                    suggestions.append({
                        "generic_name": drug.generic_name.title(),
                        "class": safe_c.replace('_', ' ').title()
                    })
                    found += 1
                    if found >= 2: break 
                    
    unique_suggestions = list({v['generic_name']: v for v in suggestions}.values())
    return {"alternatives": unique_suggestions}

@app.post("/api/check-ddi")
async def check_ddi_endpoint(payload: DDIRequest):
    if not supabase: raise HTTPException(status_code=500, detail="Database connection not available")
    drugs = [d for d in payload.drugs if d]
    results = []
    
    if len(drugs) < 2: return {"interactions": [], "safe": True}
    
    pairs = list(combinations(drugs, 2))
    for da, db in pairs:
        gen_a, class_a = get_drug_info(da)
        gen_b, class_b = get_drug_info(db)
        c1, c2 = sorted([class_a, class_b])
        
        # 1. Check Mechanistic Rules in Database
        rule_res = supabase.table("ddi_rules").select("*").eq("class_a", c1).eq("class_b", c2).execute()
        
        description = None
        severity = "Info"
        advice = "Monitor clinical status."
        source = "Heuristic"
        has_local_rule = False
        
        if rule_res.data:
            has_local_rule = True
            rule_data = rule_res.data[0]
            severity = rule_data["severity"]
            description = rule_data["description"]
            advice = rule_data["advice"]
            source = "Local Knowledge Base"

        # 2. Dynamic Enrichment from FDA
        if not has_local_rule:
            # We check both directions because labels differ
            fda_warning = await get_fda_interaction_warning(gen_a, gen_b)
            if not fda_warning:
                fda_warning = await get_fda_interaction_warning(gen_b, gen_a)
                
            if fda_warning:
                description = fda_warning
                source = "OpenFDA Regulatory API"
                if "must not be used" in fda_warning.lower() or "contraindicated" in fda_warning.lower():
                    severity = "Major"
                else:
                    severity = "Intermediate"

        if description or has_local_rule:
            results.append({
                "pair": [da.title(), db.title()],
                "severity": severity,
                "description": description or "Interaction suspected via class-mechanism logic.",
                "advice": advice,
                "source": source
            })

    severity_order = {"Major": 1, "Intermediate": 2, "Moderate": 2, "Minor": 3, "Info": 4}
    results.sort(key=lambda x: severity_order.get(x["severity"], 99))
    return {"interactions": results, "safe": len(results) == 0}

@app.post("/api/parse-prescription")
async def parse_prescription_endpoint(payload: ParseRequest):
    if not ner_engine: raise HTTPException(status_code=500, detail="NER Parser not loaded. Check server logs.")
    try:
        text = payload.text
        
        # Safely split bulk pasted chunks
        if "|||" in text:
            lines = [l.strip() for l in text.split("|||") if l.strip()]
        elif ";" in text:
            lines = [l.strip() for l in text.split(";") if l.strip()]
        else:
            lines = text.split('\n')
            
        parsed_drugs = ner_engine.extract_drugs(lines)
        
        # Force fallback if NER completely misses complex items
        if not parsed_drugs and lines:
            parsed_drugs = [{"original_text": line} for line in lines]
            
        frontend_drugs = []
        for d in parsed_drugs:
            orig = d.get('original_text', '')
            if not orig: continue
            
            freq = extract_frequency(orig)
            
            # --- PRIORITY DOSAGE EXTRACTION ---
            # Text extraction dominates DB extraction. This fixes "80 MG" being overridden by "None" or "100mg"
            text_dose = extract_dose_text(orig)
            
            if text_dose:
                dosage = text_dose
            elif d.get('dose_mg'):
                dosage = f"{d.get('dose_mg')} mg"
            else:
                dosage = "Unknown dose"
            
            # --- BRAND EXTRACTION ---
            b_name = d.get('brand_name', 'Unknown')
            if (not b_name or b_name.lower() == 'unknown'):
                parts = re.split(r'[:#]', orig)
                b_name = parts[0].replace("ANS ", "").replace("*", "").strip()
                
            # --- CLASS OVERRIDE ---
            d_class = str(d.get('class', 'unknown')).strip()
            if not d_class or d_class.lower() in ['unknown', 'unknown class', 'none']:
                _, d_class = get_drug_info(b_name)
                if d_class.lower() == 'unknown':
                    _, d_class = get_drug_info(orig)
                
            frontend_drugs.append({
                "drugName": b_name,
                "drugClass": d_class,
                "dosage": dosage,
                "frequency": freq
            })
            
        return {"separate_drugs": frontend_drugs, "racikan": []}
    except Exception as e:
        print(f"Parse Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/resolve-drug-class")
async def resolve_drug_class(q: str):
    """Utility to determine drug class using the backend's master logic."""
    _, d_class = get_drug_info(q)
    return {"drug_class": d_class}

@app.get("/api/icd/search")
async def search_icd(q: str):
    if not supabase: return []
    try:
        safe_q = q.replace(",", "") 
        res = supabase.table("icd10_mit") \
            .select("icd10_code,who_full_desc") \
            .or_(f"icd10_code.ilike.%{safe_q}%,who_full_desc.ilike.%{safe_q}%") \
            .limit(20) \
            .execute()
        
        return [{"code": r["icd10_code"], "description": r["who_full_desc"]} for r in res.data]
    except Exception as e:
        print(f"ICD Search Error: {e}")
        return []

@app.post("/nurse/submit-triage")
async def submit_triage(data: TriageData):
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
    try:
        res = supabase.table("triage_notes").insert(data.model_dump(exclude_none=True)).execute()
        supabase.table("appointments").update({"status": "consultation"}).eq("id", data.appointment_id).execute()
        return {"status": "success", "triage_id": res.data[0]['id']}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/nurse/queue")
async def get_nurse_queue():
    if not supabase: return []
    return supabase.table("appointments").select("*, patients(*)").in_("status", ["scheduled", "checked_in"]).order("queue_number").execute().data

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
            "objective": "Recorded in Triage",
            "assessment": assessment,
            "plan": data.therapy_instructions,
            "prescription_raw_text": json.dumps(data.prescription_items)
        }).execute()
        
        consult_id = res.data[0]['id']
        supabase.table("appointments").update({"status": "pharmacy"}).eq("id", data.appointment_id).execute()
        
        return {"status": "success", "consultation_id": consult_id, "interactions": []}
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

@app.get("/patient/doctors")
async def get_patient_doctors():
    if not supabase: return []
    # Fetch profiles with role 'doctor'
    res = supabase.table("profiles").select("id, full_name, specialization").eq("role", "doctor").execute()
    return res.data

@app.get("/patient/appointments")
async def get_patient_appointments(patient_id: str):
    if not supabase: return []
    try:
        # Fetch appointments and join with doctor profiles
        res = supabase.table("appointments") \
            .select("*, doctor:profiles!doctor_id(full_name, specialization)") \
            .eq("patient_id", patient_id) \
            .order("scheduled_time", desc=True) \
            .execute()
        return res.data
    except Exception as e:
        print(f"Fetch appointments error: {e}")
        return []

@app.post("/patient/book-appointment")
async def book_appointment(data: BookingRequest):
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
    try:
        scheduled_time = f"{data.date}T{data.time}:00"
        
        # Get next queue number for that day
        today_res = supabase.table("appointments") \
            .select("queue_number") \
            .gte("scheduled_time", f"{data.date}T00:00:00") \
            .lte("scheduled_time", f"{data.date}T23:59:59") \
            .order("queue_number", desc=True) \
            .limit(1) \
            .execute()
            
        next_q = 1
        if today_res.data:
            next_q = (today_res.data[0]['queue_number'] or 0) + 1
            
        res = supabase.table("appointments").insert({
            "patient_id": data.patient_id,
            "doctor_id": data.doctor_id,
            "scheduled_time": scheduled_time,
            "status": "scheduled",
            "queue_number": next_q
        }).execute()
        
        return {"status": "success", "appointment": res.data[0]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/create-staff")
async def create_staff(data: StaffCreateRequest):
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
    try:
        # 1. Create User in Auth (Admin Bypass Confirmation)
        new_user = supabase.auth.admin.create_user({
            "email": data.email,
            "password": data.password,
            "user_metadata": {"full_name": data.name},
            "email_confirm": True
        })
        
        user_id = new_user.user.id
        
        # 2. Create Profile
        supabase.table("profiles").insert({
            "id": user_id,
            "email": data.email,
            "full_name": data.name,
            "role": data.role,
            "specialization": data.specialization
        }).execute()
        
        return {"status": "success", "user_id": user_id}
    except Exception as e:
        print(f"Create Staff Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/create-patient")
async def create_patient(data: PatientCreateRequest):
    if not supabase: raise HTTPException(status_code=500, detail="DB Error")
    try:
        # 1. Create User in Auth
        new_user = supabase.auth.admin.create_user({
            "email": data.email,
            "password": data.password,
            "user_metadata": {"full_name": data.name},
            "email_confirm": True
        })
        
        user_id = new_user.user.id
        
        # 2. Generate MRN
        mrn = f"HIS-{datetime.datetime.now().strftime('%Y%m%d')}-{str(abs(hash(data.nik)) % 10000).zfill(4)}"
        
        # 3. Create Patient Record
        supabase.table("patients").insert({
            "id": user_id,
            "full_name": data.name,
            "dob": data.dob,
            "gender": data.gender,
            "nik": data.nik,
            "phone_number": data.phone_number,
            "address": data.address,
            "mrn": mrn,
            "insurance_provider": data.insurance_provider,
            "insurance_number": data.insurance_number,
            "insurance_plan_type": data.insurance_plan_type,
            "insurance_coverage_limit": data.insurance_coverage_limit,
            "allergies": data.allergies,
            "emergency_name": data.emergency_name,
            "emergency_relationship": data.emergency_relationship,
            "emergency_phone": data.emergency_phone,
            "consent_data_processing": data.consent_data_processing,
            "consent_notifications": data.consent_notifications
        }).execute()
        
        # 4. Create Profile entry
        supabase.table("profiles").insert({
            "id": user_id,
            "email": data.email,
            "full_name": data.name,
            "role": "patient"
        }).execute()
        
        return {"status": "success", "user_id": user_id, "mrn": mrn}
    except Exception as e:
        print(f"Create Patient Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
def read_root(): return {"status": "active", "version": "11.4 - Master Dose Extraction"}

if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
