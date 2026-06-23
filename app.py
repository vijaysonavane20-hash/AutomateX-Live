import streamlit as st
import pdfplumber
import pandas as pd
import google.generativeai as genai
import json
import time 
import re
from PIL import Image
import pyrebase

# 🚀 1. PAGE SETUP
st.set_page_config(page_title="AutomateX - Smart Extractor", page_icon="⚡", layout="wide") 

# 🎨 2. CUSTOM CSS
st.markdown("""
    <style>
    #MainMenu {visibility: hidden;} footer {visibility: hidden;}
    .stApp {background-color: #F8F9FA;}
    .stButton>button { border-radius: 8px; font-weight: bold; transition: 0.3s; }
    .main-header { text-align: center; color: #1E3A8A; font-weight: 900; }
    .metric-box { background: white; padding: 15px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); text-align: center;}
    .metric-title { font-size: 14px; color: #4B5563; font-weight: bold; margin-bottom: 5px; }
    .metric-value { font-size: 24px; font-weight: 900; margin: 0; }
    </style>
""", unsafe_allow_html=True)

# 🔥 FIREBASE CONFIGURATION (SECURED WITH st.secrets)
firebase_config = {
    "apiKey": st.secrets["FIREBASE_API_KEY"],
    "authDomain": "automate-office-work.firebaseapp.com",
    "projectId": "automate-office-work",
    "storageBucket": "automate-office-work.firebasestorage.app",
    "messagingSenderId": "813309616906",
    "appId": "1:813309616906:web:db3615fd788469372bd0f3",
    "databaseURL": "" 
}
firebase = pyrebase.initialize_app(firebase_config)
auth = firebase.auth()

# Session States
if "logged_in" not in st.session_state: st.session_state["logged_in"] = False
if "user_email" not in st.session_state: st.session_state["user_email"] = ""
if "uploader_key" not in st.session_state: st.session_state["uploader_key"] = 0 
if "credits" not in st.session_state: st.session_state["credits"] = 10 

def reset_app(): st.session_state["uploader_key"] += 1

# 🤖 PRO AI CONFIG (SECURED WITH st.secrets)
GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash") 

# =================================================================
# 🛡️ SIDEBAR (LOGIN & WALLET SYSTEM)
# =================================================================
with st.sidebar:
    if not st.session_state["logged_in"]:
        st.markdown("### 🔐 User Login")
        st.info("Sign in to access Advanced Extraction & Your Wallet.")
        choice = st.radio("Action", ["Sign In", "Create Account"], horizontal=True)
        email = st.text_input("Email", placeholder="name@company.com")
        password = st.text_input("Password", type="password")
        if st.button("🚀 Sign In", key="login_btn"):
            try:
                if choice == "Sign In":
                    user = auth.sign_in_with_email_and_password(email, password)
                    st.session_state["logged_in"], st.session_state["user_email"] = True, email
                    st.rerun()
                else:
                    user = auth.create_user_with_email_and_password(email, password)
                    st.success("Account Created! You can now sign in.")
            except Exception as e: 
                st.error("❌ Invalid Credentials.")
    else:
        st.markdown(f"### 👤 Profile<br><span style='color:#FF0080; font-size:14px;'>{st.session_state['user_email']}</span>", unsafe_allow_html=True)
        
        st.markdown(f"""
        <div class='metric-box' style='padding: 10px; margin-top: 10px; border: 2px solid #10B981;'>
            <b style='color:#4B5563;'>💰 Wallet Balance</b><br>
            <span style='font-size: 22px; color:#10B981; font-weight:900;'>{st.session_state['credits']} Credits</span>
        </div>
        """, unsafe_allow_html=True)
        
        if st.button("➕ Add Funds"):
            st.info("Payment Gateway integration pending.")
        st.write("---")
        if st.button("Logout 🚪"):
            st.session_state["logged_in"], st.session_state["user_email"] = False, ""
            st.rerun()

# =================================================================
# 🟢 MODULE 1: THE SMART FREE VERSION
# =================================================================
if not st.session_state["logged_in"]:
    st.markdown("<h1 class='main-header'>AutomateX Finance Analyzer (Free)</h1>", unsafe_allow_html=True)
    free_files = st.file_uploader("Upload PDF Invoices", type=["pdf"], accept_multiple_files=True, key=f"uploader_free_{st.session_state['uploader_key']}")
    
    if free_files:
        st.info("⏳ Scanning Documents...")
        free_data = []
        for file in free_files:
            try:
                text = ""
                with pdfplumber.open(file) as pdf:
                    for page in pdf.pages[:2]: 
                        extracted = page.extract_text()
                        if extracted: text += extracted + "\n"
                
                amounts = re.findall(r'[\d,]+\.\d{2}', text)
                clean_amounts = [float(a.replace(',', '')) for a in amounts] if amounts else []
                grand_total = max(clean_amounts) if clean_amounts else 0.0

                free_data.append({
                    "Doc Name": file.name, 
                    "Grand Total": grand_total, 
                    "Status": "✅ OK",
                    "Details": "🔒 Sign in for Pro Extraction"
                })
            except: 
                free_data.append({"Doc Name": file.name, "Status": "❌ Failed"})
                
        if free_data:
            df_free = pd.DataFrame(free_data)
            st.success("✅ Extraction Complete!")
            st.dataframe(df_free, width="stretch")

# =================================================================
# 👑 MODULE 2: THE PAID PRO VERSION
# =================================================================
else:
    st.markdown("<h1 class='main-header'>AutomateX Pro Dashboard 👑</h1>", unsafe_allow_html=True)
    pro_files = st.file_uploader("Upload PDF/Image Invoices", type=["pdf", "jpg", "jpeg", "png"], accept_multiple_files=True, key=f"uploader_pro_{st.session_state['uploader_key']}")
    
    if pro_files:
        if st.session_state["credits"] < len(pro_files):
            st.error("⚠️ Not enough credits.")
            st.stop()
            
        st.info(f"⏳ Processing {len(pro_files)} document(s)...")
        all_data = []
        success_count = 0 
        
        for file in pro_files:
            extracted_data = {}
            try:
                if file.name.lower().endswith(('.jpg', '.jpeg', '.png')):
                    image = Image.open(file)
                    image.thumbnail((1024, 1024))
                    ai_input = image
                else:
                    text = ""
                    with pdfplumber.open(file) as pdf:
                        for page in pdf.pages[:2]: text += page.extract_text() + "\n"
                    ai_input = text

                prompt = """Extract strictly to JSON: "Vendor Name", "Invoice Number", "Invoice Date", "Total Amount". 
                Extract "Items" as a list of dicts with: "Qty", "Rate", "Base Amount", "Final Total". Return ONLY valid JSON."""
                
                response = model.generate_content([prompt, ai_input])
                match = re.search(r'\{.*\}', response.text, re.DOTALL)
                if match: extracted_data = json.loads(match.group(0))
                
                base_info = {
                    "Doc Name": file.name, 
                    "Vendor Name": extracted_data.get("Vendor Name", "-"),
                    "Invoice Number": extracted_data.get("Invoice Number", "-"),
                    "Total Amount": extracted_data.get("Total Amount", "-")
                }
                
                items_list = extracted_data.get("Items", [])
                if len(items_list) == 0:
                    base_info["Item Info"] = "No line items found"
                    all_data.append(base_info)
                else:
                    for item in items_list:
                        row_data = base_info.copy()
                        row_data.update(item)
                        all_data.append(row_data)
                        
                success_count += 1 
            except Exception as e: 
                st.error(f"⚠️ Failed: {file.name}")
            
        if all_data:
            if success_count > 0:
                st.session_state["credits"] -= success_count
                st.success(f"✅ Deducted {success_count} credits.")
                
            df = pd.DataFrame(all_data)
            st.dataframe(df, width="stretch")
            
            csv_data = df.to_csv(index=False).encode('utf-8-sig') 
            st.download_button("⬇️ Download Excel", data=csv_data, file_name="AutomateX_Pro.csv", mime="text/csv")
            st.button("🔄 Clear Dashboard", on_click=reset_app)
