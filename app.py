import os
import re
import json
from flask import Flask, request, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)

# ==========================================
# 1. Configuration & Safe Firebase Initializer
# ==========================================
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

db = None

def init_firebase():
    global db
    if not firebase_admin._apps:
        creds_json = os.environ.get("FIREBASE_CREDENTIALS")
        if creds_json:
            try:
                # แปลง JSON String และจัดการ Newline (\n) ใน Private Key ป้องกัน Timeout
                cred_dict = json.loads(creds_json, strict=False)
                if "private_key" in cred_dict:
                    cred_dict["private_key"] = cred_dict["private_key"].replace("\\n", "\n")
                cred = credentials.Certificate(cred_dict)
                firebase_admin.initialize_app(cred)
            except Exception as e:
                print(f"Error parsing FIREBASE_CREDENTIALS: {e}")
        elif os.path.exists("serviceAccountKey.json"):
            cred = credentials.Certificate("serviceAccountKey.json")
            firebase_admin.initialize_app(cred)
            
    if firebase_admin._apps and not db:
        db = firestore.client()

# เรียกใช้งานการเชื่อมต่อ Firebase
init_firebase()

# ==========================================
# 2. Helper Functions & Parsing Logic
# ==========================================
def to_mins(hhmm):
    """แปลงเวลา string (HH:MM) เป็นนาที"""
    if not hhmm:
        return None
    match = re.search(r"^(\d{1,2}):(\d{2})$", str(hhmm).strip())
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None

def mins_to_hours(mins):
    """แปลงนาทีเป็นชั่วโมง"""
    return round((mins / 60.0), 2)

def parse_thai_date(date_str):
    """แปลง วันที่ DD/MM/YY หรือ DD/MM/YYYY ให้เป็น YYYY-MM-DD"""
    match = re.search(r"(\d{1,2})\/(\d{1,2})\/(\d{2,4})", date_str)
    if not match:
        return None
    d, mo, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if y < 100:
        y += 2000
    return f"{y}-{mo:02d}-{d:02d}"

def match_nearest_shift(shifts, in_min):
    """หากะการทำงานที่ใกล้เคียงที่สุดจากเวลาเข้างาน"""
    if in_min is None or not shifts:
        return None
    selected = None
    min_diff = float("inf")
    for s in shifts:
        s_start = to_mins(s.get("start"))
        if s_start is None:
            continue
        diff = abs(s_start - in_min)
        if diff < min_diff:
            min_diff = diff
            selected = s
    return selected

def parse_shift_text(raw_text):
    """แกะสถานะการทำงานและเวลาเข้า-ออก (รองรับกรณีลาป่วย/เวลาไม่สมบูรณ์)"""
    text = raw_text.strip()

    # ดึงช่วงเวลา (HH:MM - HH:MM)
    range_match = re.search(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", text)
    time_in = range_match.group(1) if range_match else None
    time_out = range_match.group(2) if range_match else None

    # ตรวจสอบสถานะการทำงาน
    if re.search(r"ป่วย|ลาป่วย", text, re.IGNORECASE):
        status = "sick"
    elif re.search(r"^off$|^หยุด$", text, re.IGNORECASE):
        status = "off"
    elif not time_in and not time_out:
        status = "off" if re.search(r"off", text, re.IGNORECASE) else "work"
    else:
        status = "work"

    return {"status": status, "timeIn": time_in, "timeOut": time_out}

def calculate_ot(row, shifts):
    """คำนวณชั่วโมงปกติและชั่วโมง OT"""
    if row.get("status") != "work":
        return {"normalHours": 0, "otHours": 0}

    in_min = to_mins(row.get("timeIn"))
    out_min = to_mins(row.get("timeOut"))

    if in_min is None or out_min is None:
        return {"normalHours": 0, "otHours": 0}

    shift = match_nearest_shift(shifts, in_min)
    ot_min = 0

    if shift:
        shift_end = to_mins(shift.get("end"))
        if shift_end is not None and out_min > shift_end:
            ot_min = out_min - shift_end

    break_min = shift.get("breakMin", 0) if shift else 0
    worked_min = out_min - in_min

    return {
        "normalHours": mins_to_hours(max(worked_min - ot_min - break_min, 0)),
        "otHours": mins_to_hours(max(ot_min, 0))
    }

def process_report_text(text, shifts):
    """อ่านข้อความรายงานประจำวันแล้วแปลงเป็น Data Object"""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    branch = ""
    ymd = None
    rows = []

    for line in lines:
        b_match = re.search(r"สาขา\s*([A-Za-zก-๙0-9_-]+)", line)
        if b_match:
            branch = b_match.group(1).strip()

        d_match = re.search(r"วันที่\s*(\d{1,2}\/\d{1,2}\/\d{2,4})", line)
        if d_match:
            ymd = parse_thai_date(d_match.group(1))

        # ดักจับรายชื่อพนักงานและเวลา
        r_match = re.search(r"^\d+[.)]\s*(\d{4,})\s+(.+?)\s+([0-9: \-a-zA-Zก-๙]+.*)$", line)
        if r_match:
            code = r_match.group(1)
            name = r_match.group(2).strip()
            rest = r_match.group(3)

            parsed = parse_shift_text(rest)
            row_data = {"code": code, "name": name, **parsed}
            calc = calculate_ot(row_data, shifts)
            rows.append({**row_data, **calc})

    return branch, ymd, rows

# ==========================================
# 3. Webhook & Web Routes
# ==========================================
@app.route("/")
def index():
    return jsonify({
        "status": "Online",
        "service": "UNO OT Bot",
        "database": "Firestore Connected" if db else "Firestore Connection Failed"
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        return "Invalid signature", 400
    except Exception as e:
        print(f"Webhook Error: {e}")
        return "Internal Error", 500
        
    return "OK", 200

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text

    if "สาขา" not in user_text or "วันที่" not in user_text:
        return

    if not db:
        print("Database not connected")
        return

    try:
        # 1. ดึง Master Data จาก Firestore
        doc_ref = db.collection("ot_system").document("app_data")
        doc = doc_ref.get()
        if not doc.exists:
            return

        cloud_data = doc.to_dict()
        shifts = cloud_data.get("shifts", [])

        # 2. ประมวลผลข้อความ
        branch, ymd, rows = process_report_text(user_text, shifts)

        if not branch or not ymd or not rows:
            reply_msg = "❌ ไม่สามารถประมวลผลได้ กรุณาตรวจสอบรูปแบบข้อความ (ต้องระบุ สาขา, วันที่ และรายชื่อ)"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
            return

        # 3. จัดเตรียมโครงสร้างบันทึกข้อมูลเข้า Firestore ให้ตรงกับ index.html
        record_key = f"{branch}|{ymd}"
        next_records = cloud_data.get("records", {})
        next_records[record_key] = rows

        next_emps = cloud_data.get("employees", {})
        next_users = cloud_data.get("users", {})

        for r in rows:
            next_emps[r["code"]] = {"code": r["code"], "name": r["name"]}
            if r["code"] not in next_users:
                next_users[r["code"]] = {
                    "code": r["code"],
                    "pass": "1234",
                    "role": "employee",
                    "name": r["name"]
                }

        # 4. บันทึกกลับไปยัง Document เดียวกัน
        doc_ref.update({
            "records": next_records,
            "employees": next_emps,
            "users": next_users
        })

        # 5. แจ้งเตือนกลับไปยัง LINE
        reply_msg = f"✅ บันทึกข้อมูลเรียบร้อย!\nสาขา: {branch}\nวันที่: {ymd}\nจำนวน: {len(rows)} คน"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))

    except Exception as e:
        print(f"Error handling LINE message: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
