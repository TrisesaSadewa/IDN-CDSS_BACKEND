import os
import json
import re
from typing import List, Optional, Dict, Any
from itertools import combinations
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

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


# --- 1. MECHANISM-BASED RULES ---
CLASS_RULES = {
    # --- MAJOR (Red) ---
    frozenset(["anticoagulant", "nsaid"]): { "severity": "Major", "description": "Additive Bleeding Risk.", "advice": "Avoid concurrent use." },
    frozenset(["anticoagulant", "antiplatelet"]): { "severity": "Major", "description": "Additive Bleeding Risk.", "advice": "Strict monitoring of INR/Bleeding." },
    frozenset(["hemostatic", "oral_contraceptive"]): { "severity": "Major", "description": "Additive Thrombogenic Effect: High risk of clots/stroke.", "advice": "Contraindicated." },
    frozenset(["ccb", "anticonvulsant"]): { "severity": "Major", "description": "Metabolic Induction.", "advice": "Monitor BP closely." },
    frozenset(["triptan", "psychotropic"]): { "severity": "Major", "description": "Serotonin Syndrome Risk.", "advice": "Monitor for serotonin toxicity." },
    frozenset(["sedative_hypnotic", "opioid"]): { "severity": "Major", "description": "Additive CNS Depression.", "advice": "Strict monitoring or avoid." },
    frozenset(["fibrate", "statin"]): { "severity": "Major", "description": "Additive Myotoxicity.", "advice": "Avoid if possible; monitor CK levels." },

    # --- INTERMEDIATE (Orange) ---
    frozenset(["antiplatelet", "nsaid"]): { "severity": "Intermediate", "description": "Pharmacodynamic Antagonism.", "advice": "Avoid concurrent use or space out dosing." },
    frozenset(["beta-blocker", "nsaid"]): { "severity": "Intermediate", "description": "Physiologic Antagonism.", "advice": "Monitor BP." },
    frozenset(["ace-inhibitor", "nsaid"]): { "severity": "Intermediate", "description": "Renal Hemodynamics.", "advice": "Monitor renal function." },
    frozenset(["arb", "nsaid"]): { "severity": "Intermediate", "description": "Renal Hemodynamics.", "advice": "Monitor renal function." },
    frozenset(["anticonvulsant", "folate"]): { "severity": "Intermediate", "description": "Pharmacokinetic Interaction.", "advice": "Monitor Phenytoin levels." },
    frozenset(["bisphosphonate", "nsaid"]): { "severity": "Intermediate", "description": "Additive GI Toxicity.", "advice": "Use with caution." },
    frozenset(["nsaid", "biguanide"]): { "severity": "Intermediate", "description": "Renal Risk.", "advice": "Monitor renal function." },
    frozenset(["nsaid", "sulfonylurea"]): { "severity": "Intermediate", "description": "Pharmacokinetic displacement.", "advice": "Monitor blood glucose." },
    frozenset(["nsaid", "fibrate"]): { "severity": "Intermediate", "description": "Renal/Protein Binding.", "advice": "Monitor renal function." },
    frozenset(["nsaid", "ccb"]): { "severity": "Intermediate", "description": "Physiologic Antagonism.", "advice": "Monitor BP." },
    frozenset(["ace-inhibitor", "biguanide"]): { "severity": "Intermediate", "description": "Renal competition.", "advice": "Monitor renal function." },
    frozenset(["ace-inhibitor", "sulfonylurea"]): { "severity": "Intermediate", "description": "Metabolic.", "advice": "Monitor blood glucose." },
    frozenset(["biguanide", "sulfonylurea"]): { "severity": "Intermediate", "description": "Additive Hypoglycemia Risk.", "advice": "Standard combo, but monitor glucose." },
    frozenset(["biguanide", "ccb"]): { "severity": "Intermediate", "description": "Renal/Metabolic interaction.", "advice": "Monitor status." }, 
    frozenset(["sulfonylurea", "fibrate"]): { "severity": "Intermediate", "description": "Metabolic.", "advice": "Monitor blood glucose." },
    frozenset(["sulfonylurea", "alkalinizing_agent"]): { "severity": "Intermediate", "description": "Absorption alteration.", "advice": "Separate dosing or monitor." },
    frozenset(["ccb", "statin"]): { "severity": "Intermediate", "description": "Pharmacokinetic CYP3A4 competition.", "advice": "Monitor for statin toxicity/myopathy." }, 
    frozenset(["k_sparing_diuretic", "beta-blocker"]): { "severity": "Intermediate", "description": "Additive Hypotension.", "advice": "Monitor BP." },
    frozenset(["k_sparing_diuretic", "corticosteroid"]): { "severity": "Intermediate", "description": "Physiologic Antagonism.", "advice": "Monitor fluid status." },
    frozenset(["k_sparing_diuretic", "nsaid"]): { "severity": "Intermediate", "description": "Physiologic Antagonism & Renal Risk.", "advice": "Monitor renal function and potassium." },
    frozenset(["loop_diuretic", "cardiac_glycoside"]): { "severity": "Intermediate", "description": "Toxicity Risk.", "advice": "Monitor potassium and Digoxin levels closely." },
    frozenset(["loop_diuretic", "beta-blocker"]): { "severity": "Intermediate", "description": "Additive Hypotension.", "advice": "Monitor BP." },
    frozenset(["loop_diuretic", "corticosteroid"]): { "severity": "Intermediate", "description": "Electrolyte Imbalance.", "advice": "Monitor potassium levels." },
    frozenset(["loop_diuretic", "nsaid"]): { "severity": "Intermediate", "description": "Physiologic Antagonism.", "advice": "Monitor BP and fluid status." },
    frozenset(["anticoagulant", "corticosteroid"]): { "severity": "Intermediate", "description": "GI Risk.", "advice": "Monitor for GI bleeding." },
    frozenset(["cardiac_glycoside", "beta-blocker"]): { "severity": "Intermediate", "description": "Additive Bradycardia.", "advice": "Monitor heart rate and ECG." },
    frozenset(["cardiac_glycoside", "corticosteroid"]): { "severity": "Intermediate", "description": "Toxicity Risk.", "advice": "Monitor potassium levels." },
    frozenset(["beta-blocker", "corticosteroid"]): { "severity": "Intermediate", "description": "Physiologic Antagonism.", "advice": "Monitor BP." },
    frozenset(["corticosteroid", "nsaid"]): { "severity": "Intermediate", "description": "Additive GI Toxicity.", "advice": "Use with caution; consider gastroprotection." },

    # --- MINOR (Yellow) ---
    frozenset(["k_sparing_diuretic", "cardiac_glycoside"]): { "severity": "Minor", "description": "Pharmacokinetic interaction.", "advice": "Monitor Digoxin levels." },
    frozenset(["mucosal-protective", "beta-blocker"]): { "severity": "Minor", "description": "Absorption Interference.", "advice": "Separate dosing by 2 hours." },
    frozenset(["mucosal-protective", "antiplatelet"]): { "severity": "Minor", "description": "Absorption Interference.", "advice": "Separate dosing by 2 hours." },
    frozenset(["nitrate", "antiplatelet"]): { "severity": "Minor", "description": "Additive Hemodynamics.", "advice": "Monitor for hypotension." },
    frozenset(["nitrate", "ppi"]): { "severity": "Minor", "description": "Minor pharmacokinetic interaction.", "advice": "Monitor status." },
    frozenset(["beta-blocker", "antiplatelet"]): { "severity": "Minor", "description": "Additive Hemodynamics.", "advice": "Routine monitoring." },
    frozenset(["antiplatelet", "ppi"]): { "severity": "Minor", "description": "Pharmacokinetic: pH alteration.", "advice": "Monitor efficacy." },
    frozenset(["anticonvulsant", "antiplatelet"]): { "severity": "Minor", "description": "Protein Binding Displacement.", "advice": "Monitor for signs of Phenytoin toxicity." },
    frozenset(["ace-inhibitor", "ccb"]): { "severity": "Minor", "description": "Additive Hypotension.", "advice": "Routine monitoring." }, 
    frozenset(["ace-inhibitor", "alkalinizing_agent"]): { "severity": "Minor", "description": "Absorption alteration.", "advice": "Separate dosing." }, 
    frozenset(["gabapentinoid", "ccb"]): { "severity": "Minor", "description": "Additive Edema/CNS effects.", "advice": "Monitor for peripheral edema." }, 
}

# --- 2. ALGORITHMIC THERAPEUTIC ALTERNATIVES MAP ---
ALT_CLASS_MAP = {
    "nsaid": ["analgesic", "corticosteroid"],
    "ace-inhibitor": ["arb", "ccb", "beta-blocker", "thiazide_diuretic"],
    "arb": ["ace-inhibitor", "ccb", "beta-blocker", "thiazide_diuretic"],
    "ccb": ["ace-inhibitor", "arb", "beta-blocker", "thiazide_diuretic"],
    "beta-blocker": ["ccb", "ace-inhibitor", "arb", "thiazide_diuretic"],
    "sulfonylurea": ["biguanide", "antidiabetic_other", "insulin"],
    "biguanide": ["sulfonylurea", "antidiabetic_other", "insulin"],
    "statin": ["fibrate"], 
    "fibrate": ["statin"],
    "corticosteroid": ["nsaid", "analgesic"],
    "loop_diuretic": ["thiazide_diuretic", "k_sparing_diuretic"],
    "thiazide_diuretic": ["loop_diuretic", "k_sparing_diuretic"],
    "k_sparing_diuretic": ["loop_diuretic", "thiazide_diuretic"],
    "antiplatelet": ["anticoagulant"],
    "anticoagulant": ["antiplatelet"]
}

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
    if "amoxsan" in clean_name: return ("amoxicillin", "antibiotic")
    if "tremenza" in clean_name: return ("pseudoephedrine", "decongestant")
    if "lasal" in clean_name: return ("salbutamol", "bronchodilator")
    if "trilac" in clean_name: return ("triamcinolone", "corticosteroid")
    if "zinc" in clean_name: return ("zinc", "supplement")
    if "cobazym" in clean_name: return ("cobamamide", "supplement")
    
    # General Safeties
    if "aspirin" in clean_name or "aspilet" in clean_name or "nospirinal" in clean_name: return ("acetylsalicylic acid", "antiplatelet")
    if "ibuprofen" in clean_name: return ("ibuprofen", "nsaid")
    if "omeprazole" in clean_name: return ("omeprazole", "ppi")
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
    gen_a, class_a = get_drug_info(payload.drug_to_replace)
    gen_b, class_b = get_drug_info(payload.interacting_with)
    
    if class_a not in ALT_CLASS_MAP:
        return {"alternatives": []}
        
    candidate_classes = ALT_CLASS_MAP[class_a]
    safe_classes = []
    
    for cand_class in candidate_classes:
        mech_key = frozenset([cand_class, class_b])
        if mech_key not in CLASS_RULES:
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
                "source": "Algorithm"
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

@app.get("/")
def read_root(): return {"status": "active", "version": "11.4 - Master Dose Extraction"}

if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
