import streamlit as st
import pdfplumber
import docx
import google.generativeai as genai
import json
import re
import time
import io
from PIL import Image

# ==========================================
# 🚀 1. PAGE SETUP & CSS
# ==========================================
st.set_page_config(page_title="Hidden Clause Radar", page_icon="🔍", layout="wide")

st.markdown("""
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden !important;}
    [data-testid="stToolbar"] {visibility: hidden !important;}
    [data-testid="stDecoration"] {display: none !important;}

    .stApp {background-color: #0F172A;}
    .main-header { text-align: center; color: #F8FAFC; font-weight: 900; font-size: 40px; margin-bottom: 5px;}
    .sub-header { text-align: center; color: #94A3B8; font-weight: 500; font-size: 18px; margin-bottom: 30px;}

    .verdict-box { padding: 20px; border-radius: 12px; margin-bottom: 20px; }
    .verdict-high { background: #450A0A; border: 1px solid #EF4444; }
    .verdict-medium { background: #451A03; border: 1px solid #F59E0B; }
    .verdict-low { background: #052E16; border: 1px solid #22C55E; }
    .verdict-title { font-size: 22px; font-weight: 900; margin-bottom: 6px; }
    .verdict-text { font-size: 15px; color: #CBD5E1; }

    .clause-card { background: #1E293B; border-radius: 10px; padding: 16px; margin-bottom: 14px; border-left: 5px solid #475569; }
    .clause-card.high { border-left-color: #EF4444; }
    .clause-card.medium { border-left-color: #F59E0B; }
    .clause-card.low { border-left-color: #22C55E; }

    .badge { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; margin-right: 8px; }
    .badge-high { background: #EF4444; color: white; }
    .badge-medium { background: #F59E0B; color: black; }
    .badge-low { background: #22C55E; color: black; }
    .badge-category { background: #334155; color: #E2E8F0; }

    .clause-quote { background: #0F172A; border-radius: 6px; padding: 10px 14px; margin: 10px 0; color: #E2E8F0; font-style: italic; font-size: 14px; }
    .clause-explain { color: #CBD5E1; font-size: 14px; }
    .clause-loc { color: #64748B; font-size: 12px; margin-top: 6px; }

    .metric-box { background: #1E293B; padding: 15px; border-radius: 8px; text-align: center; border-bottom: 4px solid #6366F1; }
    .metric-title { font-size: 13px; color: #94A3B8; font-weight: bold; margin-bottom: 5px; }
    .metric-value { font-size: 26px; font-weight: 900; margin: 0; color: #F8FAFC; }
    </style>
""", unsafe_allow_html=True)

st.markdown("<h1 class='main-header'>🔍 Hidden Clause Radar</h1>", unsafe_allow_html=True)
st.markdown("<p class='sub-header'>Upload any contract, agreement, or policy — AI finds the terms buried in the fine print.</p>", unsafe_allow_html=True)

# ==========================================
# 🔥 2. AI CONFIG
# ==========================================
try:
    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel("gemini-2.5-flash")
except Exception:
    st.error("⚠️ System Configuration Error. Please set GOOGLE_API_KEY in Streamlit secrets.")
    st.stop()

CATEGORIES = [
    "Auto-Renewal / Subscription Trap", "Hidden Fees & Charges", "Cancellation & Refund Restrictions",
    "Liability Limitation / Waiver", "Arbitration & Rights Waiver", "Data Privacy & Sharing",
    "Unilateral Modification Rights", "Penalty & Lock-in Clauses", "Jurisdiction / Governing Law Trap",
    "Indemnification Burden", "Other Hidden Clause",
]

ANALYSIS_PROMPT = f"""
You are a contract-risk analyst. You will be given the text (or image) of a document
(contract, terms of service, policy, agreement, etc). Your job is to find every clause that is
commonly BURIED or WRITTEN IN A WAY THAT DISADVANTAGES the person signing/accepting it —
things most people miss on a quick read.

Look specifically for clauses in these categories (use these exact category names):
{json.dumps(CATEGORIES)}

Return STRICT JSON only, in this exact shape:
{{
  "overall_risk": "High" | "Medium" | "Low",
  "overall_summary": "1-2 sentence plain-English verdict on how risky this document is overall",
  "clauses": [
    {{
      "category": "one of the categories above",
      "risk_level": "High" | "Medium" | "Low",
      "clause_text": "the exact or near-exact quoted text of the clause from the document",
      "plain_english": "what this really means for the signer, in simple plain English, and why it matters",
      "location_hint": "page number / section heading if identifiable, else '-'"
    }}
  ]
}}

RULES:
1. If the document has no hidden/risky clauses, return an empty "clauses" list and overall_risk "Low".
2. Do not invent clauses that are not in the document. Only report what is actually present.
3. Prioritize clauses an average reader would likely skim past or not fully understand.
4. Return ONLY valid JSON, no markdown fences, no extra commentary.
"""

# ==========================================
# 📄 3. TEXT EXTRACTION
# ==========================================
def extract_from_pdf(file):
    text = ""
    with pdfplumber.open(file) as pdf:
        for i, page in enumerate(pdf.pages):
            page_text = page.extract_text() or ""
            text += f"\n[Page {i + 1}]\n{page_text}\n"
    return text

def extract_from_docx(file):
    document = docx.Document(file)
    return "\n".join(p.text for p in document.paragraphs if p.text.strip())

def extract_from_txt(file):
    return file.read().decode("utf-8", errors="ignore")

def get_ai_input(file):
    name = file.name.lower()
    if name.endswith((".jpg", ".jpeg", ".png", ".webp", ".tiff")):
        image = Image.open(file)
        image.thumbnail((1536, 1536))
        return image
    elif name.endswith(".pdf"):
        return extract_from_pdf(file)
    elif name.endswith(".docx"):
        return extract_from_docx(file)
    elif name.endswith(".txt"):
        return extract_from_txt(file)
    return None

# ==========================================
# 🧠 4. AI ANALYSIS
# ==========================================
def analyze_document(ai_input, max_retries=3):
    for attempt in range(max_retries):
        try:
            response = model.generate_content([ANALYSIS_PROMPT, ai_input] if not isinstance(ai_input, str) else f"{ANALYSIS_PROMPT}\n\nDOCUMENT TEXT:\n{ai_input}")
            match = re.search(r'\{.*\}', response.text, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            return None
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(3)
            else:
                return None
    return None

# ==========================================
# 🖥️ 5. UI FLOW
# ==========================================
uploaded_file = st.file_uploader(
    "Upload a document (PDF, Word, Image, or Text)",
    type=["pdf", "docx", "jpg", "jpeg", "png", "webp", "tiff", "txt"],
)

if uploaded_file:
    if st.button("🔍 Scan for Hidden Clauses", use_container_width=True):
        with st.spinner("⏳ Reading document and scanning for hidden terms..."):
            ai_input = get_ai_input(uploaded_file)
            if ai_input is None or (isinstance(ai_input, str) and not ai_input.strip()):
                st.error("⚠️ Could not read any content from this file. Try a clearer scan or different format.")
                st.stop()
            result = analyze_document(ai_input)

        if not result:
            st.error("⚠️ AI analysis failed. Please try again.")
            st.stop()

        overall_risk = result.get("overall_risk", "Low")
        overall_summary = result.get("overall_summary", "No summary available.")
        clauses = result.get("clauses", [])

        risk_class = overall_risk.lower() if overall_risk.lower() in ["high", "medium", "low"] else "low"
        risk_icon = {"high": "🚨", "medium": "⚠️", "low": "✅"}[risk_class]

        st.markdown(f"""
        <div class="verdict-box verdict-{risk_class}">
            <div class="verdict-title">{risk_icon} Overall Risk: {overall_risk}</div>
            <div class="verdict-text">{overall_summary}</div>
        </div>
        """, unsafe_allow_html=True)

        high_count = sum(1 for c in clauses if c.get("risk_level", "").lower() == "high")
        med_count = sum(1 for c in clauses if c.get("risk_level", "").lower() == "medium")
        low_count = sum(1 for c in clauses if c.get("risk_level", "").lower() == "low")

        m1, m2, m3, m4 = st.columns(4)
        with m1: st.markdown(f"<div class='metric-box'><div class='metric-title'>Total Flagged</div><p class='metric-value'>{len(clauses)}</p></div>", unsafe_allow_html=True)
        with m2: st.markdown(f"<div class='metric-box' style='border-bottom-color:#EF4444;'><div class='metric-title'>High Risk</div><p class='metric-value' style='color:#EF4444;'>{high_count}</p></div>", unsafe_allow_html=True)
        with m3: st.markdown(f"<div class='metric-box' style='border-bottom-color:#F59E0B;'><div class='metric-title'>Medium Risk</div><p class='metric-value' style='color:#F59E0B;'>{med_count}</p></div>", unsafe_allow_html=True)
        with m4: st.markdown(f"<div class='metric-box' style='border-bottom-color:#22C55E;'><div class='metric-title'>Low Risk</div><p class='metric-value' style='color:#22C55E;'>{low_count}</p></div>", unsafe_allow_html=True)

        st.write("")

        if not clauses:
            st.success("✅ No commonly-hidden risky clauses were detected in this document.")
        else:
            st.markdown("### 📋 Flagged Clauses")
            for c in clauses:
                risk = c.get("risk_level", "Low").lower()
                risk = risk if risk in ["high", "medium", "low"] else "low"
                st.markdown(f"""
                <div class="clause-card {risk}">
                    <span class="badge badge-{risk}">{risk.upper()}</span>
                    <span class="badge badge-category">{c.get("category", "Other")}</span>
                    <div class="clause-quote">"{c.get("clause_text", "-")}"</div>
                    <div class="clause-explain">{c.get("plain_english", "-")}</div>
                    <div class="clause-loc">📍 {c.get("location_hint", "-")}</div>
                </div>
                """, unsafe_allow_html=True)

        # Downloadable report
        report_lines = [f"HIDDEN CLAUSE RADAR REPORT", f"Overall Risk: {overall_risk}", f"Summary: {overall_summary}", ""]
        for c in clauses:
            report_lines += [
                f"[{c.get('risk_level', '-')}] {c.get('category', '-')}",
                f"Clause: {c.get('clause_text', '-')}",
                f"Meaning: {c.get('plain_english', '-')}",
                f"Location: {c.get('location_hint', '-')}",
                "-" * 40,
            ]
        report_text = "\n".join(report_lines)

        d1, d2 = st.columns(2)
        with d1:
            st.download_button("⬇️ Download Report (.txt)", data=report_text.encode("utf-8"), file_name="Hidden_Clause_Report.txt", mime="text/plain", use_container_width=True)
        with d2:
            st.download_button("⬇️ Download Report (.json)", data=json.dumps(result, indent=2).encode("utf-8"), file_name="Hidden_Clause_Report.json", mime="application/json", use_container_width=True)

st.markdown("---")
st.caption("⚖️ This tool provides an AI-generated analysis for awareness purposes only and is not legal advice. Consult a qualified lawyer before signing important documents.")
