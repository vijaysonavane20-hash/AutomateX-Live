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

# 🎨 2. CUSTOM CSS (Aapki original styling)
st.markdown("""
    <style>
    #MainMenu {visibility: hidden;} footer {visibility: hidden;}
    .stApp {background-color: #F8F9FA;}
    .stButton>button { border-radius: 8px; font-weight: bold; transition: 0.3s; }
    .main-header { text-align: center; color: #1E3A8A; font-weight: 900; }
    .pricing-table th { background-color: #1E3A8A; color: white; text-align: center; font-size: 18px; padding: 12px;}
    .pricing-table td { text-align: center; font-size: 16px; padding: 10px; background-color: white;}
    .metric-box { background: white; padding: 15px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); text-align: center;}
    .metric-title { font-size: 14px; color: #4B5563; font-weight: bold; margin-bottom: 5px; }
    .metric-value { font-size: 24px; font-weight: 900; margin: 0; }
    </style>
""", unsafe_allow_html=True)

# 🔥 FIREBASE CONFIGURATION (Secured with st.secrets)
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
if "credits" not in st.session_state: st.session_state["credits"] = 10 # Startup Bonus Credits

def reset_app(): st.session_state["uploader_key"] += 1

# 🤖 PRO AI CONFIG (Secured with st.secrets)
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
            except: st.error("❌ Invalid Credentials.")
    else:
        st.markdown(f"### 👤 Profile<br><span style='color:#FF0080; font-size:14px;'>{st.session_state['user_email']}</span>", unsafe_allow_html=True)
        
        # 💰 WALLET UI
        st.markdown(f"""
        <div class='metric-box' style='padding: 10px; margin-top: 10px; border: 2px solid #10B981;'>
            <b style='color:#4B5563;'>💰 Wallet Balance</b><br>
            <span style='font-size: 22px; color:#10B981; font-weight:900;'>{st.session_state['credits']} Credits</span>
        </div>
        """, unsafe_allow_html=True)
        
        if st.button("➕ Add Funds (UPI / Cards)"):
            st.info("Payment Gateway (Stripe/Razorpay) integration pending. Recharge will be live soon!")

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
        st.info("⏳ Scanning Documents & Generating Insights...")
        free_data = []
        for file in free_files:
            try:
                text = ""
                lines = []
                with pdfplumber.open(file) as pdf:
                    for page in pdf.pages[:2]: 
                        extracted = page.extract_text(layout=True)
                        if extracted: 
                            text += extracted + "\n"
                            lines.extend([line.strip() for line in extracted.split('\n') if line.strip() and len(line.strip()) > 3])
                
                vendor_name = lines[0] if lines else "-"
                if "INVOICE" in vendor_name.upper() or "TAX" in vendor_name.upper():
                    vendor_name = lines[1] if len(lines) > 1 else vendor_name

                date_match = re.search(r'(?i)(?:date|dated)[\s]*[:\-]?[\s]*([0-9]{1,2}(?:st|nd|rd|th)?[-/.\s]?[A-Za-z0-9]{2,9}[-/.\s]?[0-9]{2,4})', text)
                inv_no_match = re.search(r'(?i)(?:invoice\s*no|inv\s*no|bill\s*no|invoice\s*#|invoice\s*number)[\s]*[:\-]?[\s]*([A-Z0-9\-\/]+)', text)
                gstins = list(set(re.findall(r'\d{2}[A-Z]{5}\d{4}[A-Z]{1}[A-Z0-9]{3}', text.upper())))

                amounts = re.findall(r'[\d,]+\.\d{2}', text)
                clean_amounts = [float(a.replace(',', '')) for a in amounts] if amounts else []
                grand_total = max(clean_amounts) if clean_amounts else 0.0

                compliance = "✅ OK"
                if grand_total > 50000 and not gstins: compliance = "⚠️ Missing GSTIN (>50K)"

                free_data.append({
                    "Doc Name": file.name, "Vendor Name": vendor_name,
                    "Invoice No": inv_no_match.group(1).strip() if inv_no_match else "-",
                    "Invoice Date": date_match.group(1).strip() if date_match else "-",
                    "GSTIN": gstins[0] if gstins else "-", "Compliance Status": compliance,
                    "Grand Total": grand_total, "Line Items": "🔒 Requires Pro"
                })
            except: free_data.append({"Doc Name": file.name, "Status": "Failed to read."})
                
        if free_data:
            df_free = pd.DataFrame(free_data)
            duplicates = df_free.duplicated(subset=['Vendor Name', 'Invoice No'], keep=False)
            df_free.loc[duplicates & (df_free['Invoice No'] != '-'), 'Compliance Status'] = "🚨 DUPLICATE DETECTED"

            total_spend = df_free[pd.to_numeric(df_free['Grand Total'], errors='coerce').notnull()]['Grand Total'].sum()
            num_duplicates = len(df_free[df_free['Compliance Status'] == '🚨 DUPLICATE DETECTED'])
            num_alerts = len(df_free[df_free['Compliance Status'].str.contains('⚠️', na=False)])

            st.markdown("### 📊 Batch Analytics Summary")
            col_m1, col_m2, col_m3, col_m4 = st.columns(4)
            with col_m1: st.markdown(f"<div class='metric-box'><div class='metric-title'>Total Invoices</div><p class='metric-value' style='color:#1E3A8A;'>{len(df_free)}</p></div>", unsafe_allow_html=True)
            with col_m2: st.markdown(f"<div class='metric-box'><div class='metric-title'>Total Spend</div><p class='metric-value' style='color:#10B981;'>₹{total_spend:,.2f}</p></div>", unsafe_allow_html=True)
            with col_m3: st.markdown(f"<div class='metric-box'><div class='metric-title'>🚨 Duplicates</div><p class='metric-value' style='color:{'#EF4444' if num_duplicates > 0 else '#10B981'};'>{num_duplicates}</p></div>", unsafe_allow_html=True)
            with col_m4: st.markdown(f"<div class='metric-box'><div class='metric-title'>⚠️ Compliance Alerts</div><p class='metric-value' style='color:{'#F59E0B' if num_alerts > 0 else '#10B981'};'>{num_alerts}</p></div>", unsafe_allow_html=True)
            
            st.success("✅ Extraction Complete!")
            csv_free = df_free.to_csv(index=False).encode('utf-8-sig')
            
            # 🔥 TOP BUTTONS
            t1, t2 = st.columns([1, 1])
            with t1: st.download_button("⬇️ Download Excel (Top)", data=csv_free, file_name="AutomateX_Accounts.csv", mime="text/csv", key="down_free_top")
            with t2: st.button("🔄 Clear & Restart (Top)", on_click=reset_app, key="reset_free_top")

            def color_alerts(val): return 'color: red; font-weight: bold' if '🚨' in str(val) else 'color: orange; font-weight: bold' if '⚠️' in str(val) else 'color: green'
            st.dataframe(df_free.style.map(color_alerts, subset=['Compliance Status']), width="stretch")
            
            # 🔥 BOTTOM BUTTONS
            b1, b2 = st.columns([1, 1])
            with b1: st.download_button("⬇️ Download Excel (Bottom)", data=csv_free, file_name="AutomateX_Accounts.csv", mime="text/csv", key="down_free_bot")
            with b2: st.button("🔄 Clear & Restart (Bottom)", on_click=reset_app, key="reset_free_bot")

# =================================================================
# 👑 MODULE 2: THE PAID PRO VERSION
# =================================================================
else:
    st.markdown("<h1 class='main-header'>AutomateX Pro Dashboard 👑</h1>", unsafe_allow_html=True)
    pro_files = st.file_uploader("Upload PDF or Image Invoices", type=["pdf", "jpg", "jpeg", "png"], accept_multiple_files=True, key=f"uploader_pro_{st.session_state['uploader_key']}")
    
    if pro_files:
        if st.session_state["credits"] < len(pro_files):
            st.error(f"⚠️ You only have {st.session_state['credits']} credits left. Please recharge your wallet to process {len(pro_files)} documents.")
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

                # 🔥 THE MEGA PROMPT (With GSTIN, Buyer Details, Taxes, and Math Logic combined)
                prompt = """
                Extract Invoice details into STRICT JSON. 
                RULES:
                1. Root level MUST have: "Vendor Name", "Vendor GSTIN", "Buyer Name", "Buyer GSTIN", "Invoice Number", "Invoice Date", "Total Tax Amount", "CGST", "SGST", "IGST", "Discount", "Grand Total", "Bank Account No", "IFSC Code".
                2. Extract "Items" as a list of dictionaries.
                3. STRICT MATH COLUMNS: For line items, you MUST strictly use these exact keys for math: 
                   - "Item Name"
                   - "HSN/SAC"
                   - "Qty" (Quantity)
                   - "Rate" (Unit Price)
                   - "Tax %"
                   - "Base Amount" (Taxable Value / Qty * Rate)
                   - "Final Total" (Amount including tax)
                4. CLEAN NUMBERS: Return ONLY numbers and decimals for amounts. Remove symbols and commas (e.g., return 1081.00 instead of 1,081.00).
                If a value is not found, return "-".
                """
                response = model.generate_content([prompt, ai_input])
                match = re.search(r'\{.*\}', response.text, re.DOTALL)
                if match: extracted_data = json.loads(match.group(0))
                else: extracted_data = {}
                
                # Fetching the Mega Data
                base_info = {
                    "Doc Name": file.name, 
                    "Vendor Name": extracted_data.get("Vendor Name", "-"),
                    "Vendor GSTIN": extracted_data.get("Vendor GSTIN", "-"),
                    "Buyer Name": extracted_data.get("Buyer Name", "-"),
                    "Buyer GSTIN": extracted_data.get("Buyer GSTIN", "-"),
                    "Invoice Number": extracted_data.get("Invoice Number", "-"), 
                    "Invoice Date": extracted_data.get("Invoice Date", "-"),
                    "CGST": extracted_data.get("CGST", "-"),
                    "SGST": extracted_data.get("SGST", "-"),
                    "IGST": extracted_data.get("IGST", "-"),
                    "Total Tax": extracted_data.get("Total Tax Amount", "-"),
                    "Discount": extracted_data.get("Discount", "-"),
                    "Grand Total": extracted_data.get("Grand Total", "-"),
                    "Bank Account No": extracted_data.get("Bank Account No", "-"), 
                    "IFSC Code": extracted_data.get("IFSC Code", "-")
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
            except: 
                st.error(f"⚠️ Could not process '{file.name}'. No credits deducted for this file.")
            
        if all_data:
            if success_count > 0:
                st.session_state["credits"] -= success_count
                st.success(f"✅ {success_count} invoices processed successfully. {success_count} credits deducted from Wallet.")
                
            df = pd.DataFrame(all_data).astype(object)
            
            # 🔥 BULLETPROOF MATH VERIFICATION (Aapka custom verification code)
            def clean_num(series): return pd.to_numeric(series.astype(str).str.replace(r'[^\d.]', '', regex=True), errors='coerce').fillna(0.0)
            
            for col in ["Qty", "Rate", "Base Amount"]:
                if col not in df.columns: df[col] = 0.0

            df["Calculated Base Amount"] = clean_num(df["Qty"]) * clean_num(df["Rate"])
            calc_val = df["Calculated Base Amount"].round(2)
            ai_val = clean_num(df["Base Amount"]).round(2)
            
            conditions = (abs(calc_val - ai_val) <= 2.0) | (ai_val == 0.0)
            df["Math Verification"] = conditions.map({True: "✅ Verified", False: "🚨 Mismatch"})
            df.fillna("-", inplace=True)
            
            # Ordering Columns properly
            all_cols = df.columns.tolist()
            front_cols = ["Doc Name", "Vendor Name", "Vendor GSTIN", "Buyer Name", "Invoice Number", "Invoice Date", "Math Verification", "Qty", "Rate", "Base Amount", "Calculated Base Amount"]
            middle_cols = [c for c in all_cols if c not in front_cols]
            df = df[[c for c in front_cols if c in all_cols] + middle_cols]
            
            csv_data = df.to_csv(index=False).encode('utf-8-sig') 
            
            # 🔥 TOP BUTTONS
            t1, t2 = st.columns([1, 1])
            with t1: st.download_button("⬇️ Download Excel (Top)", data=csv_data, file_name="AutomateX_Pro.csv", mime="text/csv", key="down_pro_top")
            with t2: st.button("🔄 Clear Dashboard (Top)", on_click=reset_app, key="reset_pro_top")
            
            def highlight_errors(val): return 'background-color: #FECACA; font-weight: bold; color: red;' if '🚨 Mismatch' in str(val) else 'color: green;' if '✅ Verified' in str(val) else ''
            st.dataframe(df.style.map(highlight_errors, subset=['Math Verification']), width="stretch")
            
            # 🔥 BOTTOM BUTTONS
            b1, b2 = st.columns([1, 1])
            with b1: st.download_button("⬇️ Download Excel (Bottom)", data=csv_data, file_name="AutomateX_Pro.csv", mime="text/csv", key="down_pro_bot")
            with b2: st.button("🔄 Clear Dashboard (Bottom)", on_click=reset_app, key="reset_pro_bot")
