import streamlit as st
import pdfplumber
import pandas as pd
import google.generativeai as genai
import json
import time 
import re
import io
from PIL import Image
import pyrebase
import concurrent.futures # For Bullet Train Speed (Parallel Processing)

# ==========================================
# 🚀 1. PAGE SETUP & CSS
# ==========================================
st.set_page_config(page_title="AutomateX Pro - Finance OS", page_icon="⚡", layout="wide") 

st.markdown("""
    <style>
    #MainMenu {visibility: hidden;} footer {visibility: hidden;}
    .stApp {background-color: #F8F9FA;}
    .main-header { text-align: center; color: #1E3A8A; font-weight: 900; font-size: 40px; margin-bottom: 5px;}
    .sub-header { text-align: center; color: #64748B; font-weight: 500; font-size: 18px; margin-bottom: 30px;}
    .stButton>button { border-radius: 8px; font-weight: bold; transition: 0.3s; }
    
    /* Beautiful Dashboard Metrics */
    .metric-box { background: white; padding: 15px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); text-align: center; border-bottom: 4px solid #3B82F6;}
    .metric-title { font-size: 14px; color: #4B5563; font-weight: bold; margin-bottom: 5px; }
    .metric-value { font-size: 24px; font-weight: 900; margin: 0; color: #0F172A;}
    
    /* Pricing Table Style */
    .pricing-table { width: 100%; border-collapse: collapse; margin-bottom: 30px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); border-radius: 8px; overflow: hidden; }
    .pricing-table th { background-color: #1E3A8A; color: white; padding: 12px; font-size: 18px; text-align: center; }
    .pricing-table td { background-color: white; padding: 12px; font-size: 16px; border-bottom: 1px solid #E2E8F0; text-align: center; }
    .pricing-table tr:hover { background-color: #F1F5F9; }
    .check-yes { color: #10B981; font-weight: bold; font-size: 20px;}
    .check-no { color: #EF4444; font-weight: bold; font-size: 20px;}
    </style>
""", unsafe_allow_html=True)

# ==========================================
# 🔥 2. FIREBASE & AI CONFIG (ST.SECRETS)
# ==========================================
try:
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

    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel("gemini-2.5-flash") 
except Exception as e:
    st.error("⚠️ System Configuration Error. Please check GitHub Secrets.")
    st.stop()

# ==========================================
# 🧠 3. SESSION STATES & RESET
# ==========================================
if "logged_in" not in st.session_state: st.session_state["logged_in"] = False
if "user_email" not in st.session_state: st.session_state["user_email"] = ""
if "uploader_key" not in st.session_state: st.session_state["uploader_key"] = 0 
if "credits" not in st.session_state: st.session_state["credits"] = 10 

def reset_app(): st.session_state["uploader_key"] += 1

# ==========================================
# 🛡️ 4. SIDEBAR LOGIC (LOGIN & WALLET)
# ==========================================
with st.sidebar:
    st.image("https://cdn-icons-png.flaticon.com/512/2936/2936666.png", width=80)
    st.markdown("## AutomateX OS")
    st.markdown("---")
    
    if not st.session_state["logged_in"]:
        st.markdown("### 🔐 Secure Login")
        auth_mode = st.radio("Choose Action", ["Sign In", "Create Account"], horizontal=True)
        email = st.text_input("Email ID", placeholder="admin@automatex.com")
        password = st.text_input("Password", type="password")
        
        if st.button("🚀 Proceed", use_container_width=True):
            if auth_mode == "Sign In":
                try:
                    user = auth.sign_in_with_email_and_password(email, password)
                    st.session_state["logged_in"], st.session_state["user_email"] = True, email
                    st.rerun()
                except: st.error("❌ Invalid Email or Password.")
            else:
                try:
                    user = auth.create_user_with_email_and_password(email, password)
                    st.success("✅ Account Created! You can now Sign In.")
                except: st.error("❌ Account creation failed. Password must be 6+ chars.")
    else:
        st.success(f"👤 Logged in as:\n**{st.session_state['user_email']}**")
        st.markdown(f"""
        <div class='metric-box' style='border-bottom: 4px solid #10B981;'>
            <div class='metric-title'>Pro Wallet Balance</div>
            <p class='metric-value' style='color:#10B981;'>⚡ {st.session_state['credits']} Credits</p>
        </div>
        """, unsafe_allow_html=True)
        st.write("")
        if st.button("➕ Add Funds (Coming Soon)"): st.info("Payment Gateway integration pending.")
        st.write("---")
        if st.button("🚪 Logout", use_container_width=True):
            st.session_state["logged_in"], st.session_state["user_email"] = False, ""
            st.rerun()

# ==========================================
# ⚙️ 5. PARALLEL PROCESSING CORE ENGINE
# ==========================================
def process_single_invoice(file, is_pro=False):
    """Processes a single file and returns extracted dictionary data."""
    try:
        if file.name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.tiff')):
            image = Image.open(file)
            image.thumbnail((1024, 1024))
            ai_input = image
        else:
            text = ""
            with pdfplumber.open(file) as pdf:
                for page in pdf.pages[:2]: text += page.extract_text() + "\n"
            ai_input = text

        if is_pro:
            prompt = """
            Extract Invoice details into STRICT JSON. 
            RULES:
            1. Root keys MUST be: "Vendor Name", "Vendor GSTIN", "Buyer Name", "Buyer GSTIN", "Invoice Number", 
               "Invoice Date" (Format: DD/MM/YYYY), "Handwritten Notes" (Scan for pen marks/scribbles, if none "-"),
               "Withholding Tax", "Total Tax", "CGST", "SGST", "IGST", "Discount", "Grand Total", "Bank Account No", "IFSC Code".
            2. "Items" list of dicts with: "Item Name", "HSN/SAC", "Qty", "Rate", "Tax %", "Base Amount", "Final Total".
            3. STRICT COLUMNS MATH: Ensure every item in the "Items" list strictly has valid numbers for "Qty", "Rate", "Base Amount", and "Final Total". Do not leave them as text or empty.
            4. DYNAMIC COLUMNS: Any extra info (PO Number, Vehicle No) goes into a dict named "Additional Info".
            5. Return ONLY valid numbers for amounts. If missing, return "-".
            """
            max_retries = 5 
        else:
            prompt = """
            Extract Basic Invoice details into STRICT JSON. 
            RULES:
            1. Root keys MUST be: "Vendor Name", "Vendor GSTIN", "Buyer Name", "Invoice Number", "Invoice Date", "Total Tax", "Grand Total".
            2. "Items" list of dicts with: "Item Name", "Qty", "Rate", "Total".
            3. Return ONLY valid JSON.
            """
            max_retries = 1 

        response = None
        for attempt in range(max_retries):
            try:
                response = model.generate_content([prompt, ai_input])
                break
            except Exception as e:
                if attempt < max_retries - 1: time.sleep(5)
                else: return {"Error": f"API Failed for {file.name}", "Doc Name": file.name}
        
        if response:
            match = re.search(r'\{.*\}', response.text, re.DOTALL)
            if match: data = json.loads(match.group(0))
            else: data = {}
            
            base_info = {"Doc Name": file.name}
            
            # Extract based on version
            if is_pro:
                keys_to_extract = ["Vendor Name", "Vendor GSTIN", "Buyer Name", "Buyer GSTIN", "Invoice Number", "Invoice Date", "Handwritten Notes", "Withholding Tax", "Total Tax", "CGST", "SGST", "IGST", "Discount", "Grand Total", "Bank Account No", "IFSC Code"]
            else:
                keys_to_extract = ["Vendor Name", "Vendor GSTIN", "Buyer Name", "Invoice Number", "Invoice Date", "Total Tax", "Grand Total"]

            for k in keys_to_extract: base_info[k] = data.get(k, "-")
            
            # Dynamic Info (Pro Only)
            if is_pro:
                additional_info = data.get("Additional Info", {})
                if isinstance(additional_info, dict):
                    for k, v in additional_info.items(): base_info[k] = v
            
            items_list = data.get("Items", [])
            flat_data = []
            if len(items_list) == 0:
                base_info["Item Info"] = "No line items found"
                flat_data.append(base_info)
            else:
                for item in items_list:
                    row = base_info.copy()
                    row["Item Name"] = item.get("Item Name", "-")
                    row["HSN/SAC"] = item.get("HSN/SAC", "-") if is_pro else "-"
                    row["Qty"] = item.get("Qty", "0") if str(item.get("Qty", "")).strip() not in ["", "-", "None"] else "0"
                    row["Rate"] = item.get("Rate", "0") if str(item.get("Rate", "")).strip() not in ["", "-", "None"] else "0"
                    
                    if is_pro:
                        row["Tax %"] = item.get("Tax %", "-")
                        row["Base Amount"] = item.get("Base Amount", "0") if str(item.get("Base Amount", "")).strip() not in ["", "-", "None"] else "0"
                        row["Final Total"] = item.get("Final Total", "-")
                    else:
                        row["Total"] = item.get("Total", "-")
                        
                    for k, v in item.items():
                        if k not in row: row[k] = v
                    flat_data.append(row)
            return {"success": True, "data": flat_data}
        return {"success": False, "Doc Name": file.name}
    except Exception as e:
        return {"success": False, "Doc Name": file.name}

# ==========================================
# 🟢 6. MODULE 1: FREE VERSION (60% UPGRADED)
# ==========================================
if not st.session_state["logged_in"]:
    st.markdown("<h1 class='main-header'>AutomateX Lite 🟢</h1>", unsafe_allow_html=True)
    st.markdown("<p class='sub-header'>Free Smart Extractor (60% Pro Features unlocked!)</p>", unsafe_allow_html=True)
    
    # BEAUTIFUL COMPARISON TABLE
    st.markdown("""
    <table class="pricing-table">
        <tr>
            <th>Features Included</th>
            <th>🟢 Free Version</th>
            <th>👑 Pro Version</th>
        </tr>
        <tr><td>Vendor & Buyer Details</td><td class="check-yes">✔</td><td class="check-yes">✔</td></tr>
        <tr><td>Line Items Extraction</td><td class="check-yes">✔</td><td class="check-yes">✔</td></tr>
        <tr><td>Duplicate Invoice Detector</td><td class="check-yes">✔</td><td class="check-yes">✔</td></tr>
        <tr><td>Bank & IFSC Details</td><td class="check-no">✖</td><td class="check-yes">✔</td></tr>
        <tr><td>Handwritten Notes Parsing</td><td class="check-no">✖</td><td class="check-yes">✔</td></tr>
        <tr><td>Bulletproof Math Verification</td><td class="check-no">✖</td><td class="check-yes">✔</td></tr>
        <tr><td>Download Format</td><td>CSV Only</td><td>Real Excel (.xlsx) + CSV</td></tr>
        <tr><td>Speed & Servers</td><td>Standard</td><td>High-Speed Parallel Processing</td></tr>
    </table>
    """, unsafe_allow_html=True)

    free_files = st.file_uploader("Upload PDF Invoices (Max 3 files per batch)", type=["pdf"], accept_multiple_files=True, key=f"uploader_free_{st.session_state['uploader_key']}")
    
    if free_files:
        if len(free_files) > 3:
            st.error("⚠️ Free version allows max 3 files at a time. Please login for unlimited uploads.")
            st.stop()
            
        st.info("⏳ Processing Documents via AI Engine...")
        all_free_data = []
        
        with concurrent.futures.ThreadPoolExecutor() as executor:
            results = list(executor.map(lambda f: process_single_invoice(f, is_pro=False), free_files))
            
        for res in results:
            if res.get("success"): all_free_data.extend(res["data"])
            else: st.warning(f"⚠️ Failed to read: {res.get('Doc Name')}")

        if all_free_data:
            df_free = pd.DataFrame(all_free_data)
            
            # 🚨 DUPLICATE DETECTION (SMART LOGIC FOR FREE VERSION)
            if 'Vendor Name' in df_free.columns and 'Invoice Number' in df_free.columns:
                first_docs = df_free.drop_duplicates(subset=['Vendor Name', 'Invoice Number'], keep='first')
                first_doc_map = first_docs.set_index(['Vendor Name', 'Invoice Number'])['Doc Name'].to_dict()
                
                def assign_status_free(row):
                    if str(row['Invoice Number']).strip() in ["", "-", "None"]: return "✅ Unique"
                    return "✅ Unique" if row['Doc Name'] == first_doc_map.get((row['Vendor Name'], row['Invoice Number'])) else "🚨 Duplicate"
                
                df_free['Status'] = df_free.apply(assign_status_free, axis=1)
            else:
                df_free['Status'] = "✅ Unique"

            df_free = df_free.astype(str)
            
            # Reorder Columns
            cols = df_free.columns.tolist()
            if 'Status' in cols:
                cols.insert(0, cols.pop(cols.index('Status')))
                df_free = df_free[cols]

            st.success("✅ Free Extraction Complete!")
            
            csv_free = df_free.to_csv(index=False).encode('utf-8-sig')
            col1, col2 = st.columns([1, 1])
            with col1: st.download_button("⬇️ Download CSV Data", data=csv_free, file_name="AutomateX_Free.csv", mime="text/csv")
            with col2: st.button("🔄 Clear Table", on_click=reset_app)
            
            def style_free(val):
                if '🚨 Duplicate' in str(val): return 'background-color: #FECACA; color: red;'
                elif '✅' in str(val): return 'color: green;'
                return ''
            
            st.dataframe(df_free.style.map(style_free, subset=['Status'] if 'Status' in df_free.columns else []), width="stretch")

# ==========================================
# 👑 7. MODULE 2: PRO VERSION (BULLET TRAIN)
# ==========================================
else:
    st.markdown("<h1 class='main-header'>AutomateX Pro Dashboard 👑</h1>", unsafe_allow_html=True)
    st.markdown("<p class='sub-header'>Enterprise-Grade Parallel Processing AI Engine</p>", unsafe_allow_html=True)
    
    pro_files = st.file_uploader("📤 Upload Invoices (All Formats: PDF, JPG, PNG, WEBP, TIFF)", type=["pdf", "jpg", "jpeg", "png", "webp", "tiff"], accept_multiple_files=True, key=f"uploader_pro_{st.session_state['uploader_key']}")
    
    if pro_files:
        if st.session_state["credits"] < len(pro_files):
            st.error(f"⚠️ You only have {st.session_state['credits']} credits left. Please recharge your wallet.")
            st.stop()
            
        st.info(f"⚡ Parallel Processing {len(pro_files)} document(s)... API Retry Mode ON.")
        all_pro_data = []
        success_count = 0 
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(lambda f: process_single_invoice(f, is_pro=True), pro_files))
            
        for res in results:
            if res.get("success"):
                all_pro_data.extend(res["data"])
                success_count += 1
            else:
                st.error(f"❌ Failed to parse {res.get('Doc Name')}")
                
        if all_pro_data:
            st.session_state["credits"] -= success_count
            st.success(f"✅ Fast Processing Complete! {success_count} credits deducted.")
            
            df = pd.DataFrame(all_pro_data)
            
            # 🔥 BULLETPROOF MATH VERIFICATION
            def clean_num(series): return pd.to_numeric(series.astype(str).str.replace(r'[^\d.-]', '', regex=True), errors='coerce').fillna(0.0)
            
            for col in ["Qty", "Rate", "Base Amount"]:
                if col not in df.columns: df[col] = 0.0

            df["Calculated Base"] = clean_num(df["Qty"]) * clean_num(df["Rate"])
            calc_val = df["Calculated Base"].round(2)
            ai_val = clean_num(df["Base Amount"]).round(2)
            
            conditions = (abs(calc_val - ai_val) <= 2.0) | (ai_val == 0.0)
            df["Math Check"] = conditions.map({True: "✅ Verified", False: "🚨 Mismatch"})
            
            # 🚨 DUPLICATE DETECTION (SMART LOGIC: First is Unique, Copies are Duplicates)
            if 'Vendor Name' in df.columns and 'Invoice Number' in df.columns:
                first_docs = df.drop_duplicates(subset=['Vendor Name', 'Invoice Number'], keep='first')
                first_doc_map = first_docs.set_index(['Vendor Name', 'Invoice Number'])['Doc Name'].to_dict()
                
                def assign_status(row):
                    if str(row['Invoice Number']).strip() in ["", "-", "None"]: return "✅ Unique"
                    return "✅ Unique" if row['Doc Name'] == first_doc_map.get((row['Vendor Name'], row['Invoice Number'])) else "🚨 Duplicate"
                    
                df['Status'] = df.apply(assign_status, axis=1)
            else:
                df['Status'] = "✅ Unique"
                
            df.fillna("-", inplace=True)
            
            # Column Ordering
            all_cols = df.columns.tolist()
            front_cols = ["Status", "Doc Name", "Vendor Name", "Vendor GSTIN", "Invoice Number", "Invoice Date", "Handwritten Notes", "Math Check", "Qty", "Rate", "Base Amount", "Calculated Base"]
            middle_cols = [c for c in all_cols if c not in front_cols]
            df = df[[c for c in front_cols if c in all_cols] + middle_cols].astype(str) 
            
            # 📥 GENERATE REAL EXCEL
            excel_buffer = io.BytesIO()
            with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name="AutomateX Data")
            excel_data = excel_buffer.getvalue()
            csv_data = df.to_csv(index=False).encode('utf-8-sig') 
            
            # UI BUTTONS & TABLE
            st.markdown("### 📥 Download Results & Analytics")
            d1, d2, d3 = st.columns([1, 1, 2])
            with d1: st.download_button("⬇️ Download Excel (.xlsx)", data=excel_data, file_name="AutomateX_Pro.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
            with d2: st.download_button("⬇️ Download CSV", data=csv_data, file_name="AutomateX_Pro.csv", mime="text/csv", use_container_width=True)
            with d3: st.button("🔄 Clear Dashboard", on_click=reset_app, use_container_width=True)
            
            # Error-Free Styling Function
            def style_dataframe(val):
                if '🚨 Mismatch' in str(val) or '🚨 Duplicate' in str(val): return 'background-color: #FECACA; color: red;'
                elif '✅' in str(val): return 'color: green;'
                return ''
            
            try:
                st.dataframe(df.style.map(style_dataframe, subset=['Math Check', 'Status']), width="stretch")
            except Exception as e:
                st.dataframe(df, width="stretch")
