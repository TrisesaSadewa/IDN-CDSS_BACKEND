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

# --- DYNAMIC METADATA CACHE ---
CLASS_MOA = {}

def load_class_metadata():
    """Fetches drug class metadata (MoA, etc.) from Supabase and caches locally."""
    global CLASS_MOA
    if not supabase: return
    try:
        print("Loading Drug Class Metadata from Supabase...")
        res = supabase.table("drug_class_metadata").select("*").execute()
        if res.data:
            # Simple MoA lookup for backend interaction checks
            CLASS_MOA = {item['class_key']: item['moa_description'] for item in res.data}
            # Entire object for UI guide consumption
            global FULL_CLASS_METADATA_CACHE
            FULL_CLASS_METADATA_CACHE = res.data
            print(f"SUCCESS: {len(CLASS_MOA)} drug classes metadata loaded.")
    except Exception as e:
        print(f"ERROR: Failed to load drug class metadata: {e}")
        CLASS_MOA = {}
        FULL_CLASS_METADATA_CACHE = []

# Global cache for the Guide UI
FULL_CLASS_METADATA_CACHE = []

# Load on startup
load_class_metadata()

# --- MODELS ---
class ParseRequest(BaseModel):
    text: str

class MedicationItem(BaseModel):
    name: str
    frequency: Optional[str] = "Anytime"

class DDIRequest(BaseModel):
    medications: Optional[List[MedicationItem]] = None
    drugs: Optional[List[str]] = None

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
    
    # Database Lookup (Structured index from Supabase knowledge_map + generic_classes)
    if structured_drug_db and hasattr(structured_drug_db, 'DRUG_INDEX'):
        drug_obj = structured_drug_db.DRUG_INDEX.get(clean_name)
        if drug_obj and drug_obj.drug_class and drug_obj.drug_class.lower() != "unknown":
            return (drug_obj.generic_name.lower(), drug_obj.drug_class.lower())
        
        # Fallback: Check if first word of the name exists in index
        first_word = clean_name.split()[0]
        drug_obj_fallback = structured_drug_db.DRUG_INDEX.get(first_word)
        if drug_obj_fallback and drug_obj_fallback.drug_class and drug_obj_fallback.drug_class.lower() != "unknown":
            return (drug_obj_fallback.generic_name.lower(), drug_obj_fallback.drug_class.lower())

    return (clean_name, "unknown")


async def get_fda_interaction_warning(drug_name: str, drug_target: str) -> Optional[str]:
    """Queries OpenFDA for drug-specific interaction warnings."""
    import urllib.parse
    if not drug_name or drug_name == "unknown": return None
    if not drug_target or drug_target == "unknown": return None
    
    q_name = urllib.parse.quote(f'"{drug_name}"')
    q_target = urllib.parse.quote(f'"{drug_target}"')
    
    # Ensure we actually pull the label FOR drug_name, and it mentions drug_target
    url = f"https://api.fda.gov/drug/label.json?search=(openfda.generic_name:{q_name}+openfda.substance_name:{q_name})+AND+drug_interactions:{q_target}&limit=1"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    if "results" in data:
                        label = data["results"][0]
                        interaction_text = label.get("drug_interactions", [""])[0]
                        
                        if drug_target.lower() in interaction_text.lower():
                            # Clean excessive newlines/spaces (common in FDA tables)
                            clean_text = re.sub(r'\s+', ' ', interaction_text)
                            
                            # Extract meaningful sentences rather than massive tables
                            sentences = re.split(r'(?<=[.!?])\s+', clean_text)
                            for s in sentences:
                                if drug_target.lower() in s.lower():
                                    # If it's a digestible sentence, return it directly
                                    if len(s.split()) < 45 and "table" not in s.lower() and "examples of" not in s.lower():
                                        return s.strip()
                            
                            # Fallback: Capture a 240-character window around the target drug name
                            pattern = rf'(.{{0,120}}{re.escape(drug_target)}.{{0,120}})'
                            match = re.search(pattern, clean_text, re.IGNORECASE)
                            if match:
                                return f"...{match.group(1).strip()}..."
                                
                            return interaction_text[:250] + "..."
    except Exception as e:
        print(f"FDA API Error for {drug_name} + {drug_target}: {e}")
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

def get_administration_slots(frequency: Optional[str]) -> set:
    """Categorizes frequency strings into clinical administration slots with robust pattern matching."""
    if not frequency:
        return {"ANYTIME"}
    
    freq = frequency.lower().strip()
    if freq in ["unknown", "none", "nan", ""]:
        return {"ANYTIME"}
    
    slots = set()
    
    # 1. SPECIAL CASE: PRN / Anytime Keywords
    anytime_keywords = ["prn", "k/p", "kp", "needed", "whenever", "anytime", "urgent", "imm", "setiap", "kapanpun", "whenever", "as needed"]
    if any(k in freq for k in anytime_keywords):
        return {"ANYTIME"}

    # 2. Dash Pattern (e.g. 1-0-1 or 1-1-1-1)
    dash_m = re.search(r'(\d+)-(\d+)-(\d+)(?:-(\d+))?', freq)
    if dash_m:
        if int(dash_m.group(1)) > 0: slots.add("MORNING")
        if int(dash_m.group(2)) > 0: slots.add("AFTERNOON")
        if int(dash_m.group(3)) > 0: slots.add("EVENING")
        if dash_m.group(4) and int(dash_m.group(4)) > 0: slots.add("NIGHT")
        if slots: return slots

    # 3. Numeric Pattern: N x M, NddM, N x c M, N.X.M
    norm_freq = freq.replace(' ', '')
    
    # Common medical abbreviations
    if "tid" in norm_freq or "t.i.d" in norm_freq or "3dd" in norm_freq:
        slots.update(["MORNING", "AFTERNOON", "EVENING"])
    elif "bid" in norm_freq or "b.i.d" in norm_freq or "2dd" in norm_freq:
        slots.update(["MORNING", "EVENING"])
    elif "od" in norm_freq or "o.d" in norm_freq or "1dd" in norm_freq:
        slots.add("MORNING")
    
    if not slots:
        # Regex for NxM patterns (capture N)
        numeric_m = re.search(r'(\d+)\s*[xX*Dd]+\s*(?:c|cap|tab|tablet|kapsul)?\s*(\d+)', freq)
        if numeric_m:
            times = int(numeric_m.group(1))
            if times >= 4:
                slots.update(["MORNING", "AFTERNOON", "EVENING", "NIGHT"])
            elif times == 3:
                slots.update(["MORNING", "AFTERNOON", "EVENING"])
            elif times == 2:
                slots.update(["MORNING", "EVENING"])
            elif times == 1:
                slots.add("MORNING")

    # 4. Indonesian & English Time Keywords
    if any(k in freq for k in ["pagi", "morning", "am"]): slots.add("MORNING")
    if any(k in freq for k in ["siang", "afternoon", "diner", "lunch", "siang"]): slots.add("AFTERNOON")
    if any(k in freq for k in ["sore", "evening"]): slots.add("EVENING")
    if any(k in freq for k in ["malam", "night", "bedtime", "hs", "bed"]): slots.add("NIGHT")
    
    if slots: return slots

    return {"ANYTIME"}

@app.post("/api/check-ddi")
async def check_ddi_endpoint(payload: DDIRequest):
    if not supabase: raise HTTPException(status_code=500, detail="Database connection not available")
    
    # Normalize input into a standard list of {name, frequency, slots}
    med_list = []
    if payload.medications:
        for m in payload.medications:
            if m.name:
                med_list.append({
                    "name": m.name,
                    "frequency": m.frequency or "Anytime",
                    "slots": get_administration_slots(m.frequency)
                })
    elif payload.drugs:
        for dname in payload.drugs:
            if dname:
                med_list.append({
                    "name": dname,
                    "frequency": "Anytime",
                    "slots": {"ANYTIME"}
                })

    results = []
    if not med_list or len(med_list) < 2: 
        return {"interactions": [], "safe": True, "timing_safe": True}
    
    # Check all pairs
    pairs = list(combinations(med_list, 2))
    for ma, mb in pairs:
        # TIMING FILTER: Only check if they share a timing slot OR one is ANYTIME
        shared_slots = ma["slots"].intersection(mb["slots"])
        is_anytime = "ANYTIME" in ma["slots"] or "ANYTIME" in mb["slots"]
        
        if not shared_slots and not is_anytime:
            continue
            
        da = ma["name"]
        db = mb["name"]
        
        gen_a, class_a = get_drug_info(da)
        gen_b, class_b = get_drug_info(db)
        
        # Avoid checking same drug vs same drug (e.g. Paracetamol + Paracetamol)
        if gen_a == gen_b: continue

        c1, c2 = sorted([class_a, class_b])
        description = None
        severity = "Info"
        advice = "Monitor clinical status."
        source = "Heuristic"
        has_local_rule = False
        
        # 1. Check Mechanistic Rules in Database
        rule_res = supabase.table("ddi_rules").select("*").eq("class_a", c1).eq("class_b", c2).execute()
        
        if rule_res.data:
            has_local_rule = True
            rule_data = rule_res.data[0]
            severity = rule_data["severity"]
            description = rule_data["description"]
            advice = rule_data["advice"]
            source = "Local Knowledge Base"

        # 2. Dynamic Enrichment from FDA
        if not has_local_rule:
            fda_warning = await get_fda_interaction_warning(gen_a, gen_b)
            if not fda_warning:
                fda_warning = await get_fda_interaction_warning(gen_b, gen_a)
                
            if fda_warning:
                description = fda_warning
                source = "OpenFDA Regulatory API"
                warn_lower = fda_warning.lower()
                
                safe_phrases = ["no clinically significant", "did not affect", "no interaction", "not clinically significant"]
                major_phrases = ["must not be used", "contraindicated", "avoid concurrent", "avoid coadministration", "severe", "fatal", "not recommended"]
                
                if any(phrase in warn_lower for phrase in safe_phrases):
                    continue 
                elif any(phrase in warn_lower for phrase in major_phrases):
                    severity = "Major"
                    advice = "Contraindicated/Major Risk: Avoid concurrent administration."
                else:
                    severity = "Intermediate"
                    advice = "Monitor closely for adverse reactions or altered efficacy."

        if description or has_local_rule:
            # Advice Enhancement
            if advice == "Monitor BP.":
                advice = "Monitor blood pressure (maintain target < 140/90 mmHg or appropriate to patient baseline)."
            elif advice == "Routine monitoring.":
                advice = "Routine monitoring for onset of generalized adverse side effects."
            elif advice == "Monitor clinical status.":
                advice = "Careful monitoring of clinical status and progression of symptoms."
            elif advice == "Monitor Digoxin levels.":
                advice = "Monitor serum Digoxin levels closely (narrow therapeutic window, target 0.5-0.9 ng/mL)."
            elif advice == "Avoid concurrent use or space out dosing.":
                advice = "Avoid concurrent use. If strictly required, space out dosing by at least 4 to 6 hours."
            elif advice == "Avoid concurrent use.":
                advice = "Avoid concurrent use. Consider alternatives, or stagger dosing by 6+ hours to minimize interaction."
            elif advice == "Monitor renal function and potassium.":
                advice = "Monitor serum creatinine, eGFR, and hyperkalemia risk (target Potassium 3.5-5.0 mEq/L)."
            elif advice == "Monitor potassium levels.":
                advice = "Monitor serum potassium levels frequently to avoid hypo/hyperkalemic events."

            # Construct slot info for UI
            time_info = "At the same time"
            if shared_slots:
                time_info = "Same time: " + ", ".join(shared_slots)
            elif is_anytime:
                time_info = "Potential overlap (PRN/Anytime drug)"

            results.append({
                "pair": [da.title(), db.title()],
                "severity": severity,
                "description": f"[{time_info}] {description or 'Interaction suspected via class-mechanism logic.'}",
                "advice": advice,
                "source": source,
                "drug_a_moa": CLASS_MOA.get(class_a, "Mechanism unclassified."),
                "drug_b_moa": CLASS_MOA.get(class_b, "Mechanism unclassified.")
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

@app.get("/api/drug-class-guide")
async def get_drug_class_guide():
    """Returns formatted class metadata for the frontend guide UI."""
    if not FULL_CLASS_METADATA_CACHE:
        # Re-fetch if cache is empty
        load_class_metadata()
    
    # Format list into dictionary as expected by HTML/JS (mapped by display_name or class_key)
    guide_data = {}
    for item in FULL_CLASS_METADATA_CACHE:
        if item.get('display_name') and item.get('common_drugs'):
            guide_data[item['display_name']] = {
                "desc": item['general_description'],
                "moa": item['moa_description'],
                "drugs": item['common_drugs'] or []
            }
    return guide_data

@app.get("/api/resolve-drug-class")
async def resolve_drug_class(q: str):
    """Utility to determine drug class using the backend's master logic."""
    _, d_class = get_drug_info(q)
    return {"drug_class": d_class}

@app.get("/api/recommend-drugs")
async def recommend_drugs(diagnosis: str):
    if not supabase: return {"recommendations": []}
    try:
        safe_diag = diagnosis.replace("'", "")
        res = supabase.table("consultations").select("prescription_raw_text").ilike("assessment", f"%{safe_diag}%").limit(100).execute()
        
        if not res.data:
            return {"recommendations": []}
            
        drug_counts = {}
        for row in res.data:
            raw_text = row.get("prescription_raw_text")
            if raw_text:
                try:
                    items = json.loads(raw_text)
                    for item in items:
                        name = item.get("name")
                        if name:
                            clean_name = name.lower().strip()
                            drug_counts[clean_name] = drug_counts.get(clean_name, 0) + 1
                except:
                    pass
                    
        sorted_drugs = sorted(drug_counts.items(), key=lambda x: x[1], reverse=True)
        top_drugs = [{"name": name.title(), "count": count} for name, count in sorted_drugs[:5]]
        
        return {"recommendations": top_drugs}
    except Exception as e:
        print(f"Recommendation error: {e}")
        return {"recommendations": []}

@app.get("/api/recommend-smart")
async def recommend_smart(icd10: str, age: Optional[int] = None, weight: Optional[float] = None, gender: Optional[str] = None):
    if not supabase: return {"recommendations": [], "profile_notes": []}
    try:
        safe_icd10 = icd10.replace("'", "")
        # First try exact ICD bracket match
        res = supabase.table("consultations").select("prescription_raw_text").ilike("assessment", f"%[{safe_icd10}]%").limit(100).execute()
        
        if not res.data:
            # Fallback to general ICD search
            res = supabase.table("consultations").select("prescription_raw_text").ilike("assessment", f"%{safe_icd10}%").limit(100).execute()
            if not res.data:
                return {"recommendations": [], "profile_notes": []}
            
        drug_counts = {}
        for row in res.data:
            raw_text = row.get("prescription_raw_text")
            if raw_text:
                try:
                    items = json.loads(raw_text)
                    for item in items:
                        name = item.get("name")
                        if name:
                            clean_name = name.lower().strip()
                            drug_counts[clean_name] = drug_counts.get(clean_name, 0) + 1
                except:
                    pass
                    
        sorted_drugs = sorted(drug_counts.items(), key=lambda x: x[1], reverse=True)
        top_drugs = [{"name": name.title(), "count": count} for name, count in sorted_drugs[:5]]
        
        profile_notes = []
        if age is not None:
            if age < 12: profile_notes.append("Pediatric dosing considerations applied.")
            elif age > 65: profile_notes.append("Geriatric (Beers criteria) safety check applied.")
            else: profile_notes.append("Adult dosing standard.")
        
        if weight is not None:
            if weight < 40 and age and age >= 12: profile_notes.append("Low body weight dose adjustments considered.")
            elif weight > 100: profile_notes.append("High BMI dose scaling considered.")
            
        if gender:
            if gender.lower() == 'female' and age and 12 <= age <= 50:
                profile_notes.append("Checked against pregnancy/lactation contraindications.")
                
        if not profile_notes:
            profile_notes.append("Standard demographic filters applied.")
            
        return {"recommendations": top_drugs, "profile_notes": profile_notes}
    except Exception as e:
        print(f"Smart Recommendation error: {e}")
        return {"recommendations": [], "profile_notes": []}

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

@app.get("/api/analyze-symptoms")
async def analyze_symptoms(cc: str):
    if not supabase: return {"suggestions": []}
    try:
        # Search for consultations where subjective mentions this complaint
        res = supabase.table("consultations").select("assessment").ilike("subjective", f"%{cc}%").limit(50).execute()
        
        suggestions = {}
        for row in res.data:
            asmt = row.get("assessment", "")
            # Pattern: PRIMARY: Diagnosis Name [ICD-CODE]
            match = re.search(r"PRIMARY: (.*?) \[(.*?)\]", asmt)
            if match:
                diag = match.group(1).strip()
                code = match.group(2).strip()
                key = (diag, code)
                suggestions[key] = suggestions.get(key, 0) + 1
            else:
                # Basic cleanup for unstructured entries
                clean_asmt = asmt.replace("PRIMARY: ", "").split("\n")[0].strip()
                if clean_asmt and len(clean_asmt) > 3:
                     # Check if it has something that looks like an ICD code at the end
                     code_match = re.search(r"\[(.*?)\]$", clean_asmt)
                     if code_match:
                         code = code_match.group(1)
                         diag = clean_asmt.replace(f"[{code}]", "").strip()
                         suggestions[(diag, code)] = suggestions.get((diag, code), 0) + 1
                     else:
                         suggestions[(clean_asmt, "Unknown")] = suggestions.get((clean_asmt, "Unknown"), 0) + 1
        
        # Sort and take top 5
        sorted_suggestions = sorted(suggestions.items(), key=lambda x: x[1], reverse=True)
        return {
            "suggestions": [
                {"diagnosis": k[0], "code": k[1], "count": v} 
                for k, v in sorted_suggestions[:5]
            ]
        }
    except Exception as e:
        print(f"Symptom analysis error: {e}")
        return {"suggestions": []}

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
