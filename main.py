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
app = FastAPI(title="Smart HIS Backend", version="10.5 - Comprehensive Interaction Matrix")

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
    # --- MAJOR (Red) ---
    frozenset(["anticoagulant", "nsaid"]): { "severity": "Major", "description": "Additive Bleeding Risk: NSAIDs inhibit platelet aggregation and cause gastric irritation, increasing bleeding risk with anticoagulants.", "advice": "Avoid concurrent use." },
    frozenset(["anticoagulant", "antiplatelet"]): { "severity": "Major", "description": "Additive Bleeding Risk: Concurrent use significantly increases risk of major hemorrhage.", "advice": "Strict monitoring of INR/Bleeding." },
    frozenset(["hemostatic", "oral_contraceptive"]): { "severity": "Major", "description": "Additive Thrombogenic Effect: High risk of clots/stroke.", "advice": "Contraindicated." },
    frozenset(["ccb", "anticonvulsant"]): { "severity": "Major", "description": "Metabolic Induction: Anticonvulsants (e.g., Phenytoin) induce CYP3A4, reducing CCB levels.", "advice": "Monitor BP closely." },
    frozenset(["triptan", "psychotropic"]): { "severity": "Major", "description": "Serotonin Syndrome Risk: Combined use with SSRI/SNRI increases serotonin levels.", "advice": "Monitor for serotonin toxicity." },
    frozenset(["sedative_hypnotic", "opioid"]): { "severity": "Major", "description": "Additive CNS Depression.", "advice": "Strict monitoring or avoid." },
    frozenset(["fibrate", "statin"]): { "severity": "Major", "description": "Additive Myotoxicity: Increased risk of Rhabdomyolysis.", "advice": "Avoid if possible; monitor CK levels." },

    # --- INTERMEDIATE (Orange) ---
    frozenset(["antiplatelet", "nsaid"]): { "severity": "Intermediate", "description": "Pharmacodynamic Antagonism: NSAID blocks antiplatelet site, negating stroke protection.", "advice": "Avoid concurrent use or space out dosing." },
    frozenset(["beta-blocker", "nsaid"]): { "severity": "Intermediate", "description": "Physiologic Antagonism: NSAIDs reduce antihypertensive efficacy.", "advice": "Monitor BP." },
    frozenset(["ace-inhibitor", "nsaid"]): { "severity": "Intermediate", "description": "Renal Hemodynamics: Additive risk of renal impairment.", "advice": "Monitor renal function." },
    frozenset(["arb", "nsaid"]): { "severity": "Intermediate", "description": "Renal Hemodynamics: Additive risk of renal impairment.", "advice": "Monitor renal function." },
    frozenset(["anticonvulsant", "folate"]): { "severity": "Intermediate", "description": "Pharmacokinetic: Folic acid decreases Phenytoin levels; Phenytoin decreases Folate.", "advice": "Monitor Phenytoin levels and folate status." },
    frozenset(["bisphosphonate", "nsaid"]): { "severity": "Intermediate", "description": "Additive GI Toxicity: Increased risk of gastric ulceration.", "advice": "Use with caution." },
    
    # Diuretics Interactions
    frozenset(["k_sparing_diuretic", "beta-blocker"]): { "severity": "Intermediate", "description": "Additive Hypotension.", "advice": "Monitor BP." },
    frozenset(["k_sparing_diuretic", "corticosteroid"]): { "severity": "Intermediate", "description": "Physiologic Antagonism: Corticosteroids may antagonize the diuretic effect via fluid retention.", "advice": "Monitor fluid status." },
    frozenset(["k_sparing_diuretic", "nsaid"]): { "severity": "Intermediate", "description": "Physiologic Antagonism & Renal Risk: NSAIDs reduce diuretic efficacy and increase hyperkalemia risk.", "advice": "Monitor renal function and potassium." },
    frozenset(["loop_diuretic", "cardiac_glycoside"]): { "severity": "Intermediate", "description": "Toxicity Risk: Diuretic-induced hypokalemia increases the risk of Digoxin toxicity.", "advice": "Monitor potassium and Digoxin levels closely." },
    frozenset(["loop_diuretic", "beta-blocker"]): { "severity": "Intermediate", "description": "Additive Hypotension.", "advice": "Monitor BP." },
    frozenset(["loop_diuretic", "corticosteroid"]): { "severity": "Intermediate", "description": "Electrolyte Imbalance: Corticosteroids can exacerbate loop diuretic-induced hypokalemia.", "advice": "Monitor potassium levels." },
    frozenset(["loop_diuretic", "nsaid"]): { "severity": "Intermediate", "description": "Physiologic Antagonism: NSAIDs reduce diuretic efficacy.", "advice": "Monitor BP and fluid status." },
    
    # Corticosteroid & Cardiovascular Interactions
    frozenset(["anticoagulant", "corticosteroid"]): { "severity": "Intermediate", "description": "GI Risk: Corticosteroids increase risk of gastrointestinal ulceration and bleeding.", "advice": "Monitor for GI bleeding." },
    frozenset(["cardiac_glycoside", "beta-blocker"]): { "severity": "Intermediate", "description": "Additive Bradycardia: Both drugs slow AV node conduction.", "advice": "Monitor heart rate and ECG." },
    frozenset(["cardiac_glycoside", "corticosteroid"]): { "severity": "Intermediate", "description": "Toxicity Risk: Corticosteroid-induced hypokalemia increases the risk of Digoxin toxicity.", "advice": "Monitor potassium levels." },
    frozenset(["beta-blocker", "corticosteroid"]): { "severity": "Intermediate", "description": "Physiologic Antagonism: Corticosteroids cause fluid retention, antagonizing antihypertensive effects.", "advice": "Monitor BP." },
    frozenset(["corticosteroid", "nsaid"]): { "severity": "Intermediate", "description": "Additive GI Toxicity: Increased risk of gastrointestinal ulceration.", "advice": "Use with caution; consider gastroprotection." },

    # Diabetes / Metabolic Interactions
    frozenset(["nsaid", "biguanide"]): { "severity": "Intermediate", "description": "Renal Risk: NSAIDs may impair renal function, increasing risk of Metformin-induced Lactic Acidosis.", "advice": "Monitor renal function." },
    frozenset(["nsaid", "sulfonylurea"]): { "severity": "Intermediate", "description": "Pharmacokinetic: NSAIDs may displace Sulfonylureas from protein binding, increasing hypoglycemia risk.", "advice": "Monitor blood glucose." },
    frozenset(["nsaid", "fibrate"]): { "severity": "Intermediate", "description": "Renal/Protein Binding: Potential for increased toxicity.", "advice": "Monitor renal function." },
    frozenset(["nsaid", "ccb"]): { "severity": "Intermediate", "description": "Physiologic Antagonism: NSAIDs reduce antihypertensive efficacy.", "advice": "Monitor BP." },
    frozenset(["ace-inhibitor", "biguanide"]): { "severity": "Intermediate", "description": "Renal: ACE inhibitors may decrease renal clearance of Metformin.", "advice": "Monitor renal function." },
    frozenset(["ace-inhibitor", "sulfonylurea"]): { "severity": "Intermediate", "description": "Metabolic: ACE inhibitors may increase insulin sensitivity, potentiating hypoglycemia.", "advice": "Monitor blood glucose." },
    frozenset(["biguanide", "sulfonylurea"]): { "severity": "Intermediate", "description": "Additive Hypoglycemia Risk (Synergistic).", "advice": "Standard combo, but monitor glucose." },
    frozenset(["biguanide", "ccb"]): { "severity": "Intermediate", "description": "Renal/Metabolic interaction.", "advice": "Monitor status." }, 
    frozenset(["sulfonylurea", "fibrate"]): { "severity": "Intermediate", "description": "Metabolic: Fibrates may enhance effects of Sulfonylureas (Hypoglycemia).", "advice": "Monitor blood glucose." },
    frozenset(["sulfonylurea", "alkalinizing_agent"]): { "severity": "Intermediate", "description": "Absorption: Sodium Bicarbonate increases absorption of Sulfonylureas, risking hypoglycemia.", "advice": "Separate dosing or monitor." },
    frozenset(["ccb", "statin"]): { "severity": "Intermediate", "description": "Pharmacokinetic: CYP3A4 competition (e.g. Nifedipine/Amlodipine x Simvastatin).", "advice": "Monitor for statin toxicity/myopathy." }, 

    # --- MINOR (Yellow) ---
    frozenset(["k_sparing_diuretic", "cardiac_glycoside"]): { "severity": "Minor", "description": "Pharmacokinetic: Spironolactone may increase Digoxin levels or interfere with assays.", "advice": "Monitor Digoxin levels." },
    frozenset(["mucosal-protective", "beta-blocker"]): { "severity": "Minor", "description": "Absorption Interference.", "advice": "Separate dosing by 2 hours." },
    frozenset(["mucosal-protective", "antiplatelet"]): { "severity": "Minor", "description": "Absorption Interference.", "advice": "Separate dosing by 2 hours." },
    frozenset(["nitrate", "antiplatelet"]): { "severity": "Minor", "description": "Additive Hemodynamics.", "advice": "Monitor for hypotension." },
    frozenset(["nitrate", "ppi"]): { "severity": "Minor", "description": "Minor pharmacokinetic interaction.", "advice": "Monitor status." },
    frozenset(["beta-blocker", "antiplatelet"]): { "severity": "Minor", "description": "Additive Hemodynamics.", "advice": "Routine monitoring." },
    frozenset(["antiplatelet", "ppi"]): { "severity": "Minor", "description": "Pharmacokinetic: pH alteration.", "advice": "Monitor efficacy." },
    frozenset(["anticonvulsant", "antiplatelet"]): { "severity": "Minor", "description": "Protein Binding Displacement: Salicylates can displace Phenytoin.", "advice": "Monitor for signs of Phenytoin toxicity." },
    frozenset(["ace-inhibitor", "ccb"]): { "severity": "Minor", "description": "Additive Hypotension.", "advice": "Routine monitoring." }, 
    frozenset(["ace-inhibitor", "alkalinizing_agent"]): { "severity": "Minor", "description": "Absorption/Excretion alteration.", "advice": "Separate dosing." }, 
    frozenset(["gabapentinoid", "ccb"]): { "severity": "Minor", "description": "Additive Edema/CNS effects.", "advice": "Monitor for peripheral edema." }, 
}

# --- HELPERS ---
def get_drug_info(drug_name: str):
    if not drug_name: return ("unknown", "unknown")
    
    clean_name = drug_name.replace("ANS ", "").lower().strip()
    clean_name = re.sub(r'\s+\d+.*$', '', clean_name).strip() # strip dosages
    
    # --- BULLETPROOF OVERRIDES ---
    # Guarantees the CDSS catches these specific molecules regardless of your DB's sync state
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
    
    # Existing Diabetes/Metabolic Overrides
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
    
    # Existing General Safeties
    if "aspirin" in clean_name or "aspilet" in clean_name or "nospirinal" in clean_name: return ("acetylsalicylic acid", "antiplatelet")
    if "ibuprofen" in clean_name: return ("ibuprofen", "nsaid")
    if "omeprazole" in clean_name: return ("omeprazole", "ppi")
    if "sucralfate" in clean_name: return ("sucralfate", "mucosal-protective")
    if "nitro" in clean_name or "isdn" in clean_name: return ("nitroglycerin", "nitrate")
    if "phenitoin" in clean_name or "phenytoin" in clean_name: return ("phenytoin", "anticonvulsant")
    if "folat" in clean_name or "folic" in clean_name: return ("folic acid", "folate")
    
    # 1. DB Lookup (if no override hit)
    if structured_drug_db and hasattr(structured_drug_db, 'DRUG_INDEX'):
        drug_obj = structured_drug_db.DRUG_INDEX.get(clean_name)
        if drug_obj and drug_obj.drug_class and drug_obj.drug_class.lower() != "unknown":
            return (drug_obj.generic_name.lower(), drug_obj.drug_class.lower())

    return (clean_name, "unknown")

def extract_frequency(text: str) -> str:
    match = re.search(r'(\d+\s*[xX]\s*[\d\.,/]+)|(\d+-\d+-\d+)|(s\s*\d+\s*dd)', text, re.IGNORECASE)
    if match: return match.group(0)
    return "1 x 1"

def extract_dose_text(text: str) -> Optional[str]:
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
        
        # Check specific rule
        if mech_key in CLASS_RULES:
            rule = CLASS_RULES[mech_key]
            results.append({
                "pair": [da.title(), db.title()],
                "severity": rule["severity"],
                "description": rule["description"],
                "advice": rule["advice"],
                "source": "Mechanism Logic"
            })
        elif "anticonvulsant" in mech_key and "supplement" in mech_key:
             if "folat" in gen_a or "folat" in gen_b or "folic" in gen_a or "folic" in gen_b:
                 rule = CLASS_RULES.get(frozenset(["anticonvulsant", "folate"]))
                 if rule:
                     results.append({
                        "pair": [da.title(), db.title()],
                        "severity": rule["severity"],
                        "description": rule["description"],
                        "advice": rule["advice"],
                        "source": "Mechanism Logic (Heuristic)"
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
def read_root(): return {"status": "active", "version": "10.5 - Comprehensive Interaction Matrix"}

if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
