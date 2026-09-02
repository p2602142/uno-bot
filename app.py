from flask import Flask, request
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import firebase_admin
from firebase_admin import credentials, firestore
import re
import os

app = Flask(__name__)

# ดึงค่า Token จาก Environment Variables บน Render
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# เชื่อมต่อ Firebase Firestore
cred = credentials.Certificate('serviceAccountKey.json')
firebase_admin.initialize_app(cred)
db = firestore.client()

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def to_mins(hhmm):
    if not hhmm: return None
    m = re.match(r"^(\d{1,2}):(\d{2})$", hhmm.strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None

def mins_to_hours(mins):
    return round((mins / 60) * 100) / 100

def parse_thai_date(date_str):
    m = re.search(r"(\d{1,2})\/(\d{1,2})\/(\d{2,4})", date_str)
    if not m: return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100: y += 2000
    return f"{y}-{mo:02d}-{d:02d}"

def parse_shift_text(raw_text):
    text = raw_text.strip()
    
    # 1. เช็กกรณี ลาป่วย / หยุด / off
    if re.search(r"ป่วย|ลาป่วย", text):
        return {"status": "sick", "timeIn": None, "timeOut": None}
    if re.search(r"^off$|^หยุด$", text, re.IGNORECASE):
        return {"status": "off", "timeIn": None, "timeOut": None}
    
    # 2. ดึงเวลา HH:MM - HH:MM (ข้ามพวกข้อความต่อท้าย เช่น ot 0.11)
    range_match = re.search(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", text)
    if range_match:
        return {
            "status": "work", 
            "timeIn": range_match.group(1), 
            "timeOut": range_match.group(2)
        }
        
    return {"status": "work", "timeIn": None, "timeOut": None}

def match_nearest_shift(shifts, in_min):
    if in_min is None: return None
    selected, min_diff = None, float('inf')
    for s in shifts:
        s_start = to_mins(s.get('start'))
        if s_start is None: continue
        diff = abs(s_start - in_min)
        if diff < min_diff:
            min_diff = diff
            selected = s
    return selected

def calculate_ot(row, shifts):
    if row['status'] != 'work':
        return {"normalHours": 0, "otHours": 0}
    
    in_min = to_mins(row.get('timeIn'))
    out_min = to_mins(row.get('timeOut'))
    if in_min is None or out_min is None:
        return {"normalHours": 0, "otHours": 0}

    shift = match_nearest_shift(shifts, in_min)
    ot_min = 0
    if shift:
        shift_end = to_mins(shift.get('end'))
        if shift_end is not None and out_min > shift_end:
            ot_min = out_min - shift_end

    break_min = shift.get('breakMin', 0) if shift else 0
    worked_min = out_min - in_min
    
    return {
        "normalHours": mins_to_hours(max(worked_min - ot_min - break_min, 0)),
        "otHours": mins_to_hours(max(ot_min, 0))
    }

def process_report_text(text, shifts):
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    branch, ymd = "", None
    rows = []

    for line in lines:
        b_match = re.search(r"สาขา\s*([A-Za-zก-๙0-9_-]+)", line)
        if b_match: 
            branch = b_match.group(1).strip()
            
        d_match = re.search(r"วันที่\s*(\d{1,2}\/\d{1,2}\/\d{2,4})", line)
        if d_match: 
            ymd = parse_thai_date(d_match.group(1))

        # ดักจับแพทเทิร์นรายการ (รองรับ Space หลายช่อง)
        r_match = re.search(r"^\d+[.)]\s*(\d{4,})\s+(\S+)\s+(.*)$", line)
        if r_match:
            code, name, rest = r_match.groups()
            parsed = parse_shift_text(rest)
            row = {"code": code, "name": name, **parsed}
            calc = calculate_ot(row, shifts)
            rows.append({**row, **calc})

    return branch, ymd, rows

# ==========================================
# WEBHOOK CONTROLLER
# ==========================================
@app.route("/webhook", methods=['POST'])
def webhook():
    body = request.get_data(as_text=True)
    signature = request.headers.get('X-Line-Signature')
    try:
        handler.handle(body, signature)
    except Exception:
        return 'Invalid signature', 400
    return 'OK', 200

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text

    # กรองเฉพาะข้อความที่มี สาขา และ วันที่
    if "สาขา" not in user_text or "วันที่" not in user_text:
        return

    # ดึงข้อมูล Master shifts จาก Firestore
    doc_ref = db.collection("ot_system").document("app_data")
    doc = doc_ref.get()
    if not doc.exists:
        return

    cloud_data = doc.to_dict()
    shifts = cloud_data.get("shifts", [])

    # ประมวลผลข้อความ
    branch, ymd, rows = process_report_text(user_text, shifts)

    if not branch or not ymd or not rows:
        reply_msg = "❌ ไม่สามารถประมวลผลได้ กรุณาตรวจสอบรูปแบบข้อความ (ต้องมี สาขา, วันที่ และรายชื่อ)"
        line_bot_api.reply_message(event.replyToken, TextSendMessage(text=reply_msg))
        return

    # เตรียม Data Structure สำหรับอัปเดตลง Firebase
    record_key = f"{branch}|{ymd}"
    next_records = cloud_data.get("records", {})
    next_records[record_key] = rows

    next_emps = cloud_data.get("employees", {})
    next_users = cloud_data.get("users", {})

    for r in rows:
        next_emps[r['code']] = {"code": r['code'], "name": r['name']}
        if r['code'] not in next_users:
            next_users[r['code']] = {
                "code": r['code'],
                "pass": "1234",
                "role": "employee",
                "name": r['name']
            }

    # บันทึกขึ้น Firestore
    doc_ref.update({
        "records": next_records,
        "employees": next_emps,
        "users": next_users
    })

    # ตอบกลับ LINE
    reply_msg = f"✅ บันทึกข้อมูลเรียบร้อย!\nสาขา: {branch}\nวันที่: {ymd}\nจำนวน: {len(rows)} คน"
    line_bot_api.reply_message(event.replyToken, TextSendMessage(text=reply_msg))

if __name__ == "__main__":
    app.run(port=5000)
