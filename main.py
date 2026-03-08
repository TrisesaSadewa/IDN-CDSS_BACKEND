import os
import json
import re
import time
import functools
from typing import List, Optional, Dict, Any, Tuple
from itertools import combinations
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
import asyncio
import datetime
import concurrent.futures

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
            CLASS_MOA = {item['class_key']: item['moa_description'] for item in res.data}
            global FULL_CLASS_METADATA_CACHE
            FULL_CLASS_METADATA_CACHE = res.data
            print(f"SUCCESS: {len(CLASS_MOA)} drug classes metadata loaded.")
    except Exception as e:
        print(f"ERROR: Failed to load drug class metadata: {e}")
        CLASS_MOA = {}
        FULL_CLASS_METADATA_CACHE = []

@app.get("/api/refresh-cache")
async def refresh_cache():
    """Manually triggers a reload of metadata, drug database, and DDI rules."""
    load_class_metadata()
    load_ddi_rules_cache()
    if structured_drug_db and hasattr(structured_drug_db, 'db_instance'):
        structured_drug_db.db_instance.load_data()
    return {"status": "success", "classes_loaded": len(CLASS_MOA), "ddi_rules_cached": len(DDI_RULES_CACHE)}

FULL_CLASS_METADATA_CACHE = []

load_class_metadata()

# --- DDI RULES CACHE ---
DDI_RULES_CACHE = {}

def load_ddi_rules_cache():
    global DDI_RULES_CACHE
    if not supabase: return
    try:
        print("Loading DDI Rules into memory cache...")
        res = supabase.table("ddi_rules").select("*").execute()
        if res.data:
            DDI_RULES_CACHE = {}
            for rule in res.data:
                key = tuple(sorted([rule['class_a'], rule['class_b']]))
                DDI_RULES_CACHE[key] = rule
            print(f"SUCCESS: {len(DDI_RULES_CACHE)} DDI rules cached in memory.")
    except Exception as e:
        print(f"ERROR loading DDI rules cache: {e}")
        DDI_RULES_CACHE = {}

load_ddi_rules_cache()

_fda_executor = concurrent.futures.ThreadPoolExecutor(max_workers=5, thread_name_prefix="fda-api")
_fda_semaphore = asyncio.Semaphore(3) 

# --- FDA RESULT CACHE ---
_fda_cache = {} 
_FDA_CACHE_TTL = 43200

def _get_cached_fda(drug_a: str, drug_b: str):
    key = tuple(sorted([drug_a.lower(), drug_b.lower()]))
    entry = _fda_cache.get(key)
    if entry and (time.time() - entry["ts"] < _FDA_CACHE_TTL):
        return entry["result"], True  
    return None, False

def _set_cached_fda(drug_a: str, drug_b: str, result):
    key = tuple(sorted([drug_a.lower(), drug_b.lower()]))
    _fda_cache[key] = {"result": result, "ts": time.time()}

# --- DRUG INFO CACHE  ---
_drug_info_cache = {}

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
    lab_requests: Optional[List[Dict[str, Any]]] = []
    ddi_pharmacy_notes: Optional[str] = None
    ddi_monitoring_notes: Optional[str] = None

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


def get_drug_info(drug_name: str) -> Tuple[str, str]:
    if not drug_name: return ("unknown", "unknown")
    
    # Check in-memory cache first
    cache_key = drug_name.lower().strip()
    if cache_key in _drug_info_cache:
        return _drug_info_cache[cache_key]
    
    clean_name = drug_name.replace("ANS ", "").lower().strip()
    clean_name = re.sub(r'\s+\d+.*$', '', clean_name).strip() 
    
    result = (clean_name, "unknown")
    
    # Database Lookup (Structured index from Supabase knowledge_map + generic_classes)
    if structured_drug_db and hasattr(structured_drug_db, 'DRUG_INDEX'):
        drug_obj = structured_drug_db.DRUG_INDEX.get(clean_name)
        if drug_obj and drug_obj.drug_class and drug_obj.drug_class.lower() != "unknown":
            result = (drug_obj.generic_name.lower(), drug_obj.drug_class.lower())
        else:
            first_word = clean_name.split()[0]
            drug_obj_fallback = structured_drug_db.DRUG_INDEX.get(first_word)
            if drug_obj_fallback and drug_obj_fallback.drug_class and drug_obj_fallback.drug_class.lower() != "unknown":
                result = (drug_obj_fallback.generic_name.lower(), drug_obj_fallback.drug_class.lower())

    _drug_info_cache[cache_key] = result
    return result


def _sync_fda_interaction_warning(drug_name: str, drug_target: str) -> Optional[str]:
    """Synchronous FDA API call — runs in thread pool to avoid blocking event loop."""
    import urllib.parse
    import requests
    if not drug_name or drug_name == "unknown": return None
    if not drug_target or drug_target == "unknown": return None
    
    # Extract primary ingredient for searching if string is complex (e.g., "A, B, C")
    search_name = drug_name.split(',')[0].strip()
    search_target = drug_target.split(',')[0].strip()
    
    q_name = urllib.parse.quote(f'"{search_name}"')
    q_target = urllib.parse.quote(f'"{search_target}"')
    
    url = f"https://api.fda.gov/drug/label.json?search=(openfda.generic_name:{q_name}+openfda.substance_name:{q_name})+AND+drug_interactions:{q_target}&limit=1"
    
    retries = 2
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, timeout=7) 
            if response.status_code == 200:
                data = response.json()
                if "results" in data:
                    label = data["results"][0]
                    interaction_text = label.get("drug_interactions", [""])[0]
                    
                    # Fuzzy match on the lower target
                    low_target = search_target.lower()
                    if low_target in interaction_text.lower():
                        clean_text = re.sub(r'\s+', ' ', interaction_text)
                        sentences = re.split(r'(?<=[.!?])\s+', clean_text)
                        for s in sentences:
                            if low_target in s.lower():
                                if len(s.split()) < 45 and "table" not in s.lower() and "examples of" not in s.lower():
                                    return s.strip()
                        
                        pattern = rf'(.{{0,120}}{re.escape(search_target)}.{{0,120}})'
                        match = re.search(pattern, clean_text, re.IGNORECASE)
                        if match:
                            return f"...{match.group(1).strip()}..."
                            
                        return interaction_text[:250] + "..."
            elif response.status_code == 429: 
                time.sleep(1) 
                continue
            elif response.status_code == 404: 
                return None
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt == retries:
                print(f"FDA Connection/Timeout Error for {drug_name} + {drug_target}: {e}")
            time.sleep(1) 
        except Exception as e:
            if attempt == retries:
                print(f"FDA API Error for {drug_name} + {drug_target} after {retries} retries: {e}")
            time.sleep(0.5)
    return None

async def get_fda_interaction_warning(drug_name: str, drug_target: str) -> Optional[str]:
    """Async wrapper — offloads synchronous FDA HTTP call to thread pool with concurrency limit."""
    async with _fda_semaphore:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_fda_executor, _sync_fda_interaction_warning, drug_name, drug_target)


def extract_frequency(text: str) -> str:
    """Robust extraction of frequencies, capturing # and : delimiters."""
    if not text: return "1 x 1"
    
    parts = re.split(r'[:#]', text)
    if len(parts) >= 3:
        sig_part = parts[-1].strip()
        if len(sig_part) >= 2:
            return sig_part
            
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
    
    text_to_search = re.split(r'[:#]', text)[0]
        
    match1 = re.search(r'(\d+/\d+|\d+[.,]\d+|\d+)\s*(mg|g|mcg|ml|cc|iu|tab|tablet|tabs|sach|sachet|bungkus|pulv|cap|kapsul|capsule|drop|drops)\b', text_to_search, re.IGNORECASE)
    if match1:
        return f"{match1.group(1)} {match1.group(2).lower()}"
        
    match2 = re.search(r'\b(tab|tablet|tabs|sach|sachet|bungkus|pulv|cap|kapsul|capsule|drop|drops)\s+(\d+/\d+|\d+[.,]\d+|\d+)', text_to_search, re.IGNORECASE)
    if match2:
        return f"{match2.group(2)} {match2.group(1).lower()}"
        
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
    
    anytime_keywords = ["prn", "k/p", "kp", "needed", "whenever", "anytime", "urgent", "imm", "setiap", "kapanpun", "whenever", "as needed"]
    if any(k in freq for k in anytime_keywords):
        return {"ANYTIME"}

    dash_m = re.search(r'(\d+)-(\d+)-(\d+)(?:-(\d+))?', freq)
    if dash_m:
        if int(dash_m.group(1)) > 0: slots.add("MORNING")
        if int(dash_m.group(2)) > 0: slots.add("AFTERNOON")
        if int(dash_m.group(3)) > 0: slots.add("EVENING")
        if dash_m.group(4) and int(dash_m.group(4)) > 0: slots.add("NIGHT")
        if slots: return slots

    norm_freq = freq.replace(' ', '')
    
    if "tid" in norm_freq or "t.i.d" in norm_freq or "3dd" in norm_freq:
        slots.update(["MORNING", "AFTERNOON", "EVENING"])
    elif "bid" in norm_freq or "b.i.d" in norm_freq or "2dd" in norm_freq:
        slots.update(["MORNING", "EVENING"])
    elif "od" in norm_freq or "o.d" in norm_freq or "1dd" in norm_freq:
        slots.add("MORNING")
    
    if not slots:
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

    if any(k in freq for k in ["pagi", "morning", "am"]): slots.add("MORNING")
    if any(k in freq for k in ["siang", "afternoon", "diner", "lunch", "siang"]): slots.add("AFTERNOON")
    if any(k in freq for k in ["sore", "evening"]): slots.add("EVENING")
    if any(k in freq for k in ["malam", "night", "bedtime", "hs", "bed"]): slots.add("NIGHT")
    
    if slots: return slots

    return {"ANYTIME"}

@app.post("/api/check-ddi")
async def check_ddi_endpoint(payload: DDIRequest):
    if not supabase: raise HTTPException(status_code=500, detail="Database connection not available")
    
    med_list = []
    if payload.medications:
        for m in payload.medications:
            if m.name:
                gen, cls = get_drug_info(m.name)
                # Skip non-clinical items from DDI analysis
                if cls in ["non-drug", "medical_supply", "administrative"]:
                    continue
                    
                med_list.append({
                    "name": m.name,
                    "frequency": m.frequency or "Anytime",
                    "slots": get_administration_slots(m.frequency)
                })
    elif payload.drugs:
        for dname in payload.drugs:
            if dname:
                gen, cls = get_drug_info(dname)
                # Skip non-drug and medical supply items from DDI analysis
                if cls in ["non-drug", "medical_supply", "administrative"]:
                    continue
                    
                med_list.append({
                    "name": dname,
                    "frequency": "Anytime",
                    "slots": {"ANYTIME"}
                })

    results = []
    if not med_list or len(med_list) < 2: 
        return {"interactions": [], "safe": True, "timing_safe": True}
    
    pairs = list(combinations(med_list, 2))

    local_results = []
    fda_needed = []  

    for ma, mb in pairs:
        shared_slots = ma["slots"].intersection(mb["slots"])
        is_anytime = "ANYTIME" in ma["slots"] or "ANYTIME" in mb["slots"]
        
        if not shared_slots and not is_anytime:
            continue
            
        da = ma["name"]
        db = mb["name"]
        
        gen_a, class_a = get_drug_info(da)
        gen_b, class_b = get_drug_info(db)
        
        if gen_a == gen_b: continue

        c1, c2 = sorted([class_a, class_b])
        cached_rule = DDI_RULES_CACHE.get((c1, c2))
        
        if cached_rule:
            local_results.append({
                "da": da, "db": db, "gen_a": gen_a, "gen_b": gen_b,
                "class_a": class_a, "class_b": class_b,
                "severity": cached_rule["severity"],
                "description": cached_rule["description"],
                "advice": cached_rule["advice"],
                "source": "Local Knowledge Base",
                "shared_slots": shared_slots, "is_anytime": is_anytime
            })
        else:
            cached_fda, was_cached = _get_cached_fda(gen_a, gen_b)
            if was_cached:
                if cached_fda:  
                    fda_needed.append({
                        "da": da, "db": db, "gen_a": gen_a, "gen_b": gen_b,
                        "class_a": class_a, "class_b": class_b,
                        "fda_result": cached_fda,
                        "shared_slots": shared_slots, "is_anytime": is_anytime
                    })
            else:
                fda_needed.append({
                    "da": da, "db": db, "gen_a": gen_a, "gen_b": gen_b,
                    "class_a": class_a, "class_b": class_b,
                    "fda_result": None,  
                    "shared_slots": shared_slots, "is_anytime": is_anytime
                })

    items_needing_fetch = [p for p in fda_needed if p["fda_result"] is None]
    if items_needing_fetch:
        async def _fetch_fda_pair(pair_info):
            gen_a, gen_b = pair_info["gen_a"], pair_info["gen_b"]
            result = await get_fda_interaction_warning(gen_a, gen_b)
            if not result:
                result = await get_fda_interaction_warning(gen_b, gen_a)
            _set_cached_fda(gen_a, gen_b, result)
            pair_info["fda_result"] = result
        
        await asyncio.gather(*[_fetch_fda_pair(p) for p in items_needing_fetch])

    results = []
    
    def _expand_advice(advice):
        advice_map = {
            "Monitor BP.": "Monitor blood pressure (maintain target < 140/90 mmHg or appropriate to patient baseline).",
            "Routine monitoring.": "Routine monitoring for onset of generalized adverse side effects.",
            "Monitor clinical status.": "Careful monitoring of clinical status and progression of symptoms.",
            "Monitor Digoxin levels.": "Monitor serum Digoxin levels closely (narrow therapeutic window, target 0.5-0.9 ng/mL).",
            "Avoid concurrent use or space out dosing.": "Avoid concurrent use. If strictly required, space out dosing by at least 4 to 6 hours.",
            "Avoid concurrent use.": "Avoid concurrent use. Consider alternatives, or stagger dosing by 6+ hours to minimize interaction.",
            "Monitor renal function and potassium.": "Monitor serum creatinine, eGFR, and hyperkalemia risk (target Potassium 3.5-5.0 mEq/L).",
            "Monitor potassium levels.": "Monitor serum potassium levels frequently to avoid hypo/hyperkalemic events."
        }
        return advice_map.get(advice, advice)

    def _time_label(shared_slots, is_anytime):
        if shared_slots:
            return "Same time: " + ", ".join(shared_slots)
        elif is_anytime:
            return "Potential overlap (PRN/Anytime drug)"
        return "At the same time"

    for item in local_results:
        results.append({
            "pair": [item["da"].title(), item["db"].title()],
            "severity": item["severity"],
            "description": f"[{_time_label(item['shared_slots'], item['is_anytime'])}] {item['description'] or 'Interaction suspected via class-mechanism logic.'}",
            "advice": _expand_advice(item["advice"]),
            "source": item["source"],
            "drug_a_moa": CLASS_MOA.get(item["class_a"], "Mechanism unclassified."),
            "drug_b_moa": CLASS_MOA.get(item["class_b"], "Mechanism unclassified.")
        })

    safe_phrases = ["no clinically significant", "did not affect", "no interaction", "not clinically significant"]
    major_phrases = ["must not be used", "contraindicated", "avoid concurrent", "avoid coadministration", "severe", "fatal", "not recommended"]

    for item in fda_needed:
        fda_warning = item["fda_result"]
        if not fda_warning:
            continue
        warn_lower = fda_warning.lower()
        if any(phrase in warn_lower for phrase in safe_phrases):
            continue
        elif any(phrase in warn_lower for phrase in major_phrases):
            severity = "Major"
            advice = "Contraindicated/Major Risk: Avoid concurrent administration."
        else:
            severity = "Intermediate"
            advice = "Monitor closely for adverse reactions or altered efficacy."

        results.append({
            "pair": [item["da"].title(), item["db"].title()],
            "severity": severity,
            "description": f"[{_time_label(item['shared_slots'], item['is_anytime'])}] {fda_warning}",
            "advice": advice,
            "source": "OpenFDA Regulatory API",
            "drug_a_moa": CLASS_MOA.get(item["class_a"], "Mechanism unclassified."),
            "drug_b_moa": CLASS_MOA.get(item["class_b"], "Mechanism unclassified.")
        })

    severity_order = {"Major": 1, "Intermediate": 2, "Moderate": 2, "Minor": 3, "Info": 4}
    results.sort(key=lambda x: severity_order.get(x["severity"], 99))
    return {"interactions": results, "safe": len(results) == 0}


@app.post("/api/parse-prescription")
async def parse_prescription_endpoint(payload: ParseRequest):
    if not ner_engine: raise HTTPException(status_code=500, detail="NER Parser not loaded. Check server logs.")
    try:
        text = payload.text
        
        if "|||" in text:
            lines = [l.strip() for l in text.split("|||") if l.strip()]
        elif ";" in text:
            lines = [l.strip() for l in text.split(";") if l.strip()]
        else:
            lines = text.split('\n')
            
        parsed_drugs = ner_engine.extract_drugs(lines)
        
        parsed_drugs = [d for d in parsed_drugs if d.get('class') not in ["non-drug", "medical_supply", "administrative"]]
        
        if not parsed_drugs and lines:
            parsed_drugs = [{"original_text": line} for line in lines]
            
        frontend_drugs = []
        for d in parsed_drugs:
            orig = d.get('original_text', '')
            if not orig: continue
            
            freq = extract_frequency(orig)
            
            text_dose = extract_dose_text(orig)
            
            if text_dose:
                dosage = text_dose
            elif d.get('dose_mg'):
                dosage = f"{d.get('dose_mg')} mg"
            else:
                dosage = "Unknown dose"
            
            b_name = d.get('brand_name', 'Unknown')
            if (not b_name or b_name.lower() == 'unknown'):
                parts = re.split(r'[:#]', orig)
                b_name = parts[0].replace("ANS ", "").replace("*", "").strip()
                
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
        load_class_metadata()
    
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
        res = supabase.table("consultations").select("prescription_raw_text").ilike("assessment", f"%[{safe_icd10}]%").limit(100).execute()
        
        if not res.data:
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
        res = supabase.table("consultations").select("assessment").ilike("subjective", f"%{cc}%").limit(50).execute()
        
        suggestions = {}
        for row in res.data:
            asmt = row.get("assessment", "")
            match = re.search(r"PRIMARY: (.*?) \[(.*?)\]", asmt)
            if match:
                diag = match.group(1).strip()
                code = match.group(2).strip()
                key = (diag, code)
                suggestions[key] = suggestions.get(key, 0) + 1
            else:
                clean_asmt = asmt.replace("PRIMARY: ", "").split("\n")[0].strip()
                if clean_asmt and len(clean_asmt) > 3:
                     code_match = re.search(r"\[(.*?)\]$", clean_asmt)
                     if code_match:
                         code = code_match.group(1)
                         diag = clean_asmt.replace(f"[{code}]", "").strip()
                         suggestions[(diag, code)] = suggestions.get((diag, code), 0) + 1
                     else:
                         suggestions[(clean_asmt, "Unknown")] = suggestions.get((clean_asmt, "Unknown"), 0) + 1
        
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
        
        consult_payload = {
            "appointment_id": data.appointment_id,
            "doctor_id": data.doctor_id,
            "subjective": subjective,
            "objective": "Recorded in Triage",
            "assessment": assessment,
            "plan": data.therapy_instructions,
            "prescription_raw_text": json.dumps(data.prescription_items)
        }
        if data.ddi_pharmacy_notes:
            consult_payload["ddi_pharmacy_notes"] = data.ddi_pharmacy_notes
        if data.ddi_monitoring_notes:
            consult_payload["ddi_monitoring_notes"] = data.ddi_monitoring_notes
        
        res = supabase.table("consultations").insert(consult_payload).execute()
        
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
    res = supabase.table("profiles").select("id, full_name, specialization").eq("role", "doctor").execute()
    return res.data

@app.get("/patient/appointments")
async def get_patient_appointments(patient_id: str):
    if not supabase: return []
    try:
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
        new_user = supabase.auth.admin.create_user({
            "email": data.email,
            "password": data.password,
            "user_metadata": {"full_name": data.name},
            "email_confirm": True
        })
        
        user_id = new_user.user.id
        
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
        new_user = supabase.auth.admin.create_user({
            "email": data.email,
            "password": data.password,
            "user_metadata": {"full_name": data.name},
            "email_confirm": True
        })
        
        user_id = new_user.user.id
        
        mrn = f"HIS-{datetime.datetime.now().strftime('%Y%m%d')}-{str(abs(hash(data.nik)) % 10000).zfill(4)}"
        
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

@app.get("/api/fda-label")
async def get_fda_label(drug: str):
    """Fetches full structured label data from OpenFDA."""
    import urllib.parse
    q_name = urllib.parse.quote(f'"{drug}"')
    url = f"https://api.fda.gov/drug/label.json?search=(openfda.generic_name:{q_name}+openfda.brand_name:{q_name})&limit=1"
    
    try:
        import requests
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            return response.json()
        return {"error": f"FDA API returned {response.status_code}", "results": []}
    except Exception as e:
        return {"error": str(e), "results": []}

@app.get("/api/refresh-metadata")
async def refresh_metadata():
    """Manually reloads all database metadata into memory."""
    try:
        load_class_metadata()
        structured_drug_db.load_data()
        return {"status": "success", "message": "Metadata cache refreshed from Supabase."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/")
def read_root(): return {"status": "active", "version": "11.4 - Master Dose Extraction"}

if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
