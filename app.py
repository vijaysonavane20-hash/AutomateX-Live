import streamlit as st
import pdfplumber
import pandas as pd
import google.generativeai as genai
import json
import re
from PIL import Image
import pyrebase

# ==========================================
# 🚀 1. PAGE SETUP & CSS
# ==========================================
st.set_page_config(page_title="AutomateX Pro - Finance", page_icon="⚡", layout="wide") 

st.markdown("""
    <style>
    #MainMenu {visibility: hidden;} footer {visibility: hidden;}
    .stApp {background-color: #F0F4F8;}
    .main-header { text-align: center; color: #0F172A; font-weight: 900; font-size: 3rem; margin-bottom: 20px;}
    .sub-header { text-align: center; color: #3B82F6; font-weight: bold; margin-bottom: 30px;}
    .metric-card { background: white; padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); text-align: center; border-left: 5px solid #3B82F6;}
    </style>
""", unsafe_allow_html=True)

# ==========================================
# 🔥 2. FIREBASE & AI CONFIG (ST.SECRETS)
# ==========================================
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

# ==========================================
# 🧠 3. SESSION STATES
# ==========================================
if "logged_in" not in st.session_state: st.session_state["logged_in"] = False
if "user_email" not in st.session_state: st.session_state["user_email"] = ""
if "credits" not in st.session_state: st.session_state["credits"] = 50 # Gave 50 credits default
if "extracted_df" not in st.session_state: st.session_state["extracted_df"] = None

def clear_data():
    st.session_state["extracted_df"] = None

# ==========================================
# 🛡️ 4. SIDEBAR LOGIC (LOGIN / SIGNUP)
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
                except: st.error("❌ Invalid Email or Password. Try again.")
            else:
                try:
                    user = auth.create_user_with_email_and_password(email, password)
                    st.success("✅ Account Created! You can now Sign In.")
                    st.info("Note: Accounts are auto-approved for this beta.")
                except Exception as e: st.error("❌ Account creation failed. Password must be 6+ chars.")
    else:
        st.success(f"👤 Logged in as:\n**{st.session_state['user_email']}**")
        st.markdown(f"""
        <div class='metric-card'>
            <h4 style='color:#64748B; margin:0;'>Pro Credits</h4>
            <h2 style='color:#10B981; margin:0;'>⚡ {st.session_state['credits']}</h2>
        </div>
        """, unsafe_allow_html=True)
        st.write("")
        if st.button("🚪 Logout", use_container_width=True):
            st.session_state["logged_in"], st.session_state["user_email"] = False, ""
            clear_data()
            st.rerun()

# ==========================================
# 🟢 5. FREE VERSION MODULE (NO LOGIN)
# ==========================================
if not st.session_state["logged_in"]:
    st.markdown("<h1 class='main-header'>AutomateX Lite 🟢</h1>", unsafe_allow_html=True)
    st.markdown("<p class='sub-header'>Basic PDF Grand Total Extractor (Free Tier)</p>", unsafe_allow_html=True)
    
    st.info("💡 Unlock **Itemized Extraction, Vendor Details, AI Accuracy & Excel Export** by logging in!")
    
    free_files = st.file_uploader("Upload PDF Invoices (Basic Scan)", type=["pdf"], accept_multiple_files=True)
    
    if free_files:
        with st.spinner("Scanning Basic Data..."):
            free_data = []
            for file in free_files:
                try:
                    text = ""
                    with pdfplumber.open(file) as pdf:
                        for page in pdf.pages[:2]: text += page.extract_text() + "\n"
                    
                    # Basic Regex for amounts
                    amounts = re.findall(r'[\d,]+\.\d{2}', text)
                    clean_amounts = [float(a.replace(',', '')) for a in amounts] if amounts else []
                    grand_total = max(clean_amounts) if clean_amounts else 0.0

                    free_data.append({"Document": file.name, "Estimated Total": f"₹ {grand_total}", "Access": "🔒 Pro Needed for details"})
                except: 
                    free_data.append({"Document": file.name, "Status": "Failed"})
                    
            if free_data:
                st.dataframe(pd.DataFrame(free_data), width=800)

# ==========================================
# 👑 6. PRO VERSION MODULE (LOGGED IN)
# ==========================================
else:
    st.markdown("<h1 class='main-header'>AutomateX Dashboard 👑</h1>", unsafe_allow_html=True)
    
    pro_files = st.file_uploader("📤 Upload Invoices (PDF/Images) for AI Deep Extraction", type=["pdf", "jpg", "jpeg", "png"], accept_multiple_files=True)
    
    if st.button("⚡ Run AI Extraction", type="primary") and pro_files:
        if st.session_state["credits"] < len(pro_files):
            st.error("⚠️ Insufficient Credits! Please top up.")
            st.stop()
            
        with st.spinner(f"🚀 AI Processing {len(pro_files)} document(s)..."):
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

                    prompt = """Extract invoice details to strictly valid JSON. Keys required: 
                    "Vendor Name", "Invoice Number", "Invoice Date", "Grand Total". 
                    Also extract "Items" as a list of dicts with: "Item Name", "Qty", "Rate", "Total". Return ONLY JSON."""
                    
                    response = model.generate_content([prompt, ai_input])
                    match = re.search(r'\{.*\}', response.text, re.DOTALL)
                    if match: extracted_data = json.loads(match.group(0))
                    
                    # Flatten Data
                    base_info = {
                        "File Name": file.name, 
                        "Vendor": extracted_data.get("Vendor Name", "Unknown"),
                        "Invoice #": extracted_data.get("Invoice Number", "-"),
                        "Date": extracted_data.get("Invoice Date", "-"),
                        "Grand Total": extracted_data.get("Grand Total", 0)
                    }
                    
                    items_list = extracted_data.get("Items", [])
                    if not items_list:
                        base_info["Item Name"] = "No items detected"
                        all_data.append(base_info)
                    else:
                        for item in items_list:
                            row = base_info.copy()
                            row.update(item)
                            all_data.append(row)
                            
                    success_count += 1 
                except Exception as e:
                    st.error(f"❌ Failed to parse {file.name}")
            
            if all_data:
                st.session_state["credits"] -= success_count
                st.session_state["extracted_df"] = pd.DataFrame(all_data)
                st.success(f"✅ Extraction Successful! {success_count} Credits Deducted.")

    # Show Editable Dashboard if data exists
    if st.session_state["extracted_df"] is not None:
        st.markdown("### 📝 Edit & Verify Data")
        st.info("You can directly click on the table below to edit any mistakes before downloading.")
        
        # MAGIC FEATURE: Editable Dataframe
        edited_df = st.data_editor(st.session_state["extracted_df"], num_rows="dynamic", use_container_width=True)
        
        col1, col2, col3 = st.columns(3)
        with col1:
            csv = edited_df.to_csv(index=False).encode('utf-8-sig')
            st.download_button(label="⬇️ Download CSV", data=csv, file_name="AutomateX_Export.csv", mime="text/csv", use_container_width=True)
        with col2:
            st.button("🔄 Clear Data", on_click=clear_data, use_container_width=True)

        st.markdown("---")
        st.markdown("### 📊 Quick Analytics")
        try:
            # Try to convert grand total to numeric for analytics
            edited_df['Numeric Total'] = pd.to_numeric(edited_df['Grand Total'].astype(str).str.replace(r'[^\d.]', '', regex=True), errors='coerce').fillna(0)
            total_spent = edited_df['Numeric Total'].sum()
            
            c1, c2 = st.columns(2)
            c1.metric("Total Invoices Processed", len(edited_df["File Name"].unique()))
            c2.metric("Estimated Total Value", f"₹ {total_spent:,.2f}")
            
            st.write("Vendor Breakdown:")
            vendor_totals = edited_df.groupby("Vendor")['Numeric Total'].sum()
            st.bar_chart(vendor_totals)
        except:
            st.warning("Analytics need clean numeric data in 'Grand Total' column.")
