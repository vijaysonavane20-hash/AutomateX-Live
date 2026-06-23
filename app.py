# =================================================================
# 👑 MODULE 2: THE PAID PRO VERSION (UPGRADED)
# =================================================================
else:
    st.markdown("<h1 class='main-header'>AutomateX Pro Dashboard 👑</h1>", unsafe_allow_html=True)
    
    # 📁 Supported ALL major formats
    pro_files = st.file_uploader("Upload Invoices (PDF/Images - All Formats)", type=["pdf", "jpg", "jpeg", "png", "webp", "tiff"], accept_multiple_files=True, key=f"uploader_pro_{st.session_state['uploader_key']}")
    
    if pro_files:
        if st.session_state["credits"] < len(pro_files):
            st.error(f"⚠️ You only have {st.session_state['credits']} credits left. Please recharge your wallet.")
            st.stop()
            
        st.info(f"⏳ Processing {len(pro_files)} document(s). API Retry Mode: ON (Will wait if server is busy)...")
        all_data = []
        success_count = 0 
        
        for file in pro_files:
            extracted_data = {}
            # 🖼️ File Processing
            if file.name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.tiff')):
                image = Image.open(file)
                image.thumbnail((1024, 1024))
                ai_input = image
            else:
                text = ""
                with pdfplumber.open(file) as pdf:
                    for page in pdf.pages[:2]: text += page.extract_text() + "\n"
                ai_input = text

            # 🔥 THE ZIDDI PROMPT (With Date strictness, Dynamic columns, and Withholding Tax)
            prompt = """
            Extract Invoice details into STRICT JSON. 
            RULES:
            1. Root level MUST have: "Vendor Name", "Vendor GSTIN", "Buyer Name", "Buyer GSTIN", "Invoice Number", 
               "Invoice Date" (MUST be converted exactly to DD/MM/YYYY format, e.g., 20/06/2026 regardless of original format), 
               "Withholding Tax", "Total Tax Amount", "CGST", "SGST", "IGST", "Discount", "Grand Total", "Bank Account No", "IFSC Code".
            2. Extract "Items" as a list of dictionaries with math keys ("Item Name", "Qty", "Rate", "Base Amount", "Final Total").
            3. DYNAMIC COLUMNS: If you find ANY other valuable extra details in the invoice (e.g., PO Number, Vehicle No, Due Date), put them inside a dictionary key called "Additional Info".
            4. CLEAN NUMBERS: Return ONLY numbers and decimals for amounts.
            If a value is not found, return "-".
            """
            
            # ⏳ API BUSY RETRY LOGIC (Never Skip)
            max_retries = 5
            response = None
            for attempt in range(max_retries):
                try:
                    response = model.generate_content([prompt, ai_input])
                    break # Success, break the loop
                except Exception as e:
                    if attempt < max_retries - 1:
                        st.toast(f"⏳ API Busy! Waiting & Retrying '{file.name}' (Attempt {attempt+2}/{max_retries})...")
                        time.sleep(8) # Wait 8 seconds before trying again
                    else:
                        st.error(f"❌ API Failed for '{file.name}' after {max_retries} attempts.")
                        response = None
            
            if response:
                match = re.search(r'\{.*\}', response.text, re.DOTALL)
                if match: extracted_data = json.loads(match.group(0))
                else: extracted_data = {}
                
                # Flattening Base Data & Tax Bundle
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
                    "Withholding Tax": extracted_data.get("Withholding Tax", "-"),
                    "Total Tax": extracted_data.get("Total Tax Amount", "-"),
                    "Discount": extracted_data.get("Discount", "-"),
                    "Grand Total": extracted_data.get("Grand Total", "-"),
                    "Bank Account No": extracted_data.get("Bank Account No", "-"), 
                    "IFSC Code": extracted_data.get("IFSC Code", "-")
                }

                # 🧠 Dynamic Extra Columns Injection
                additional_info = extracted_data.get("Additional Info", {})
                if isinstance(additional_info, dict):
                    for k, v in additional_info.items():
                        base_info[k] = v
                
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
            
        if all_data:
            if success_count > 0:
                st.session_state["credits"] -= success_count
                st.success(f"✅ {success_count} invoices processed successfully!")
                
            df = pd.DataFrame(all_data).astype(object)
            
            # 🚨 DUPLICATE DETECTION IN PRO
            duplicates = df.duplicated(subset=['Vendor Name', 'Invoice Number'], keep=False)
            df['Duplicate Status'] = duplicates.map({True: "🚨 Duplicate", False: "✅ Unique"})
            
            # 🔥 BULLETPROOF MATH VERIFICATION
            def clean_num(series): return pd.to_numeric(series.astype(str).str.replace(r'[^\d.]', '', regex=True), errors='coerce').fillna(0.0)
            
            for col in ["Qty", "Rate", "Base Amount"]:
                if col not in df.columns: df[col] = 0.0

            df["Calculated Base Amount"] = clean_num(df["Qty"]) * clean_num(df["Rate"])
            calc_val = df["Calculated Base Amount"].round(2)
            ai_val = clean_num(df["Base Amount"]).round(2)
            
            conditions = (abs(calc_val - ai_val) <= 2.0) | (ai_val == 0.0)
            df["Math Verification"] = conditions.map({True: "✅ Verified", False: "🚨 Mismatch"})
            df.fillna("-", inplace=True)
            
            # Ordering Columns properly (Putting Duplicate Status upfront)
            all_cols = df.columns.tolist()
            front_cols = ["Duplicate Status", "Doc Name", "Vendor Name", "Vendor GSTIN", "Invoice Number", "Invoice Date", "Math Verification", "Qty", "Rate", "Base Amount", "Calculated Base Amount"]
            middle_cols = [c for c in all_cols if c not in front_cols]
            df = df[[c for c in front_cols if c in all_cols] + middle_cols]
            
            # 📥 DOWNLOAD OPTIONS (CSV & Real Excel)
            csv_data = df.to_csv(index=False).encode('utf-8-sig') 
            
            import io
            excel_buffer = io.BytesIO()
            with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name="Invoices")
            excel_data = excel_buffer.getvalue()
            
            st.markdown("### 📥 Download Results")
            d1, d2, d3 = st.columns([1, 1, 2])
            with d1: st.download_button("⬇️ Download CSV", data=csv_data, file_name="AutomateX_Pro.csv", mime="text/csv", use_container_width=True)
            with d2: st.download_button("⬇️ Download Excel (.xlsx)", data=excel_data, file_name="AutomateX_Pro.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
            with d3: st.button("🔄 Clear Dashboard", on_click=reset_app)
            
            def highlight_errors(val): return 'background-color: #FECACA; color: red;' if '🚨 Mismatch' in str(val) or '🚨 Duplicate' in str(val) else 'color: green;' if '✅' in str(val) else ''
            st.dataframe(df.style.map(highlight_errors, subset=['Math Verification', 'Duplicate Status']), width="stretch")
