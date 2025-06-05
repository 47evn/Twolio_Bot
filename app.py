from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import google.generativeai as genai
import requests
import sys
import os
import re
import time
from threading import Timer
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.append(r"D:\testing\webbot")
from helpers import default_instruction

GOOGLE_API_KEY = "AIzaSyD52ImyueMUNql1-uCbbgEK4Ie9K14JRUI"
genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

app = Flask(__name__)
CORS(app)

access_token = None
user_session_state = {}

def authenticate_bot():
    global access_token
    url = "https://bi.siissoft.com/secureappointment/api/v1/auth/login"
    payload = {"username": "bot@siissoft.it", "password": "Dana"}
    try:
        response = requests.post(url, json=payload, timeout=5)
        response.raise_for_status()
        data = response.json()
        access_token = data.get("auth", {}).get("access_token")
        print("✅ Access token retrieved.")
    except Exception as e:
        print(f"❌ Auth failed: {e}")

def cleanup_old_sessions():
    """Clean up session states older than 1 hour"""
    current_time = time.time()
    sessions_to_remove = []
    
    for session_id, session_data in user_session_state.items():
        if 'last_activity' not in session_data:
            session_data['last_activity'] = current_time
        elif current_time - session_data['last_activity'] > 3600:  # 1 hour
            sessions_to_remove.append(session_id)
    
    for session_id in sessions_to_remove:
        user_session_state.pop(session_id, None)
        print(f"🧹 Cleaned up old session: {session_id}")
    
    # Schedule next cleanup
    Timer(1800, cleanup_old_sessions).start()  # Run every 30 minutes

# Initialize authentication and cleanup
authenticate_bot()
cleanup_old_sessions()

def fetch_group_info(headers, group_id):
    try:
        url = f"https://bi.siissoft.com/secureappointment/api/v1/groups/{group_id}"
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            return resp.json().get("group", {})
    except Exception as e:
        print(f"⚠️ Group API Error: {e}")
    return {}

def fetch_professionals(headers, group_id):
    try:
        url = "https://bi.siissoft.com/secureappointment/api/v1/professionals"
        body = {"groupId": group_id, "format": "whatsapp"}
        resp = requests.get(url, headers=headers, json=body, timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"⚠️ Professionals API Error: {e}")
    return {}

def fetch_slots(headers, group_id):
    try:
        url = f"https://bi.siissoft.com/secureappointment/api/v1/slots/{group_id}"
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"⚠️ Slots API Error: {e}")
    return {}

def extract_slot_info(api_response):
    """
    Extracts available slot dates and cleaned time lists from the API response.
    Removes duplicate times and sorts them.
    """
    slots_raw = api_response.get("slots", {})  # safely get the nested 'slots' dict

    slot_info_date = list(slots_raw.keys())

    # Cleaned slot_info_time: remove duplicates and sort time list
    slot_info_time = {
        date: sorted(list(set(times)))
        for date, times in slots_raw.items()
    }

    return slot_info_date, slot_info_time


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/chat', methods=['POST'])
def chat():
    global access_token
    data = request.json
    user_prompt = data.get('prompt', '').strip()

    user_context = {
        "id": data.get("id"),
        "name": data.get("name"),
        "surname": data.get("surname"),
        "phone_number": data.get("phone_number"),
        "email": data.get("email"),
        "group_id": data.get("current_group_id"),
        "previous_response": data.get("previous_response", [])
    }

    session_id = user_context["id"]
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    if session_id not in user_session_state:
        user_session_state[session_id] = {}

    session_data = user_session_state[session_id]
    
    # Update last activity timestamp
    session_data['last_activity'] = time.time()

    # Allow users to cancel booking process
    if user_prompt.lower() in ['cancel', 'annulla', 'stop', 'reset', 'ricomincia', 'esci']:
        user_session_state[session_id] = {'last_activity': time.time()}  # Clear session but keep timestamp
        return jsonify({
            "response": "❌ Prenotazione annullata. Come posso aiutarti?"
        })

    # Step 1: Handle date input
    if session_data.get("awaiting") == "date":
        # Validate date format (basic validation)
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', user_prompt):
            return jsonify({
                "response": "❌ Formato data non valido. Usa il formato: aaaa-mm-gg (es: 2024-12-25)\n\n💡 Scrivi 'annulla' per uscire dalla prenotazione."
            })
        
        session_data["dateStart"] = user_prompt
        session_data["awaiting"] = "time"
        return jsonify({
            "response": "📅 Perfetto! Ora inserisci l'orario (formato: HH:MM, es: 14:30):\n\n💡 Scrivi 'annulla' per uscire dalla prenotazione."
        })

    # Step 2: Handle time input and book appointment
    elif session_data.get("awaiting") == "time":
        # Validate time format
        if not re.match(r'^\d{2}:\d{2}$', user_prompt):
            return jsonify({
                "response": "❌ Formato orario non valido. Usa il formato: HH:MM (es: 14:30)\n\n💡 Scrivi 'annulla' per uscire dalla prenotazione."
            })

        session_data["timeStart"] = user_prompt

        booking_payload = {
            "groupId": user_context["group_id"],
            "userId": user_context["id"],
            "ProfessionalID": session_data.get("ProfessionalID", 0),
            "dateStart": session_data["dateStart"],
            "timeStart": session_data["timeStart"]
        }

        print("🔄 Attempting booking:", booking_payload)

        try:
            booking_url = "https://bi.siissoft.com/secureappointment/api/v1/appointments"
            booking_resp = requests.post(booking_url, headers=headers, json=booking_payload, timeout=15)

            # CRITICAL: Clear session state regardless of success/failure
            user_session_state[session_id] = {'last_activity': time.time()}

            if booking_resp.status_code == 200:
                return jsonify({
                    "response": f"✅ Appuntamento prenotato con successo!\n\n📅 Data: {booking_payload['dateStart']}\n🕐 Orario: {booking_payload['timeStart']}\n\n🎉 Ci vediamo presto! Come posso aiutarti adesso?"
                })
            else:
                error_msg = "Errore sconosciuto"
                try:
                    error_data = booking_resp.json()
                    error_msg = error_data.get('message', error_data.get('error', booking_resp.text))
                except:
                    error_msg = booking_resp.text

                return jsonify({
                    "response": f"❌ Prenotazione fallita: {error_msg}\n\n💡 Prova con un altro orario o contatta il supporto.\n\nCome posso aiutarti ora?"
                })

        except requests.exceptions.Timeout:
            user_session_state[session_id] = {'last_activity': time.time()}  # Clear session
            return jsonify({
                "response": "❌ Timeout durante la prenotazione. Il server sta impiegando troppo tempo a rispondere.\n\n💡 Riprova più tardi o contatta il supporto. Come posso aiutarti?"
            })
        except requests.exceptions.ConnectionError:
            user_session_state[session_id] = {'last_activity': time.time()}  # Clear session
            return jsonify({
                "response": "❌ Errore di connessione durante la prenotazione."
            })
        except Exception as e:
            user_session_state[session_id] = {'last_activity': time.time()}  # Clear session
            return jsonify({
                "response": f"❌ Errore imprevisto durante la prenotazione: {str(e)}\n\n💡 Riprova o contatta il supporto. Come posso aiutarti?"
            })

    # Handle normal chat flow (not in booking process)
    # Re-authenticate if needed
    if not access_token:
        authenticate_bot()
        if not access_token:
            return jsonify({
                "response": "❌ Errore di autenticazione. Riprova più tardi."
            })

    # Fetch info concurrently
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(fetch_group_info, headers, user_context["group_id"]): "group",
            executor.submit(fetch_professionals, headers, user_context["group_id"]): "professionals",
            executor.submit(fetch_slots, headers, user_context["group_id"]): "slots"
        }
        results = {"group": {}, "professionals": {}, "slots": {}}
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as e:
                print(f"⚠️ {key.capitalize()} API concurrency error: {e}")

    group_info = results["group"]
    professionals_info = results["professionals"]
    slots_info = results["slots"]
    
    user_info_text = (
        f"\n[User Info]\n"
        f"ID: {user_context.get('id')}, "
        f"Name: {user_context.get('name')} {user_context.get('surname')}, "
        f"Email: {user_context.get('email')}, "
        f"Phone: {user_context.get('phone_number')}, "
        f"Group ID: {user_context.get('group_id')}\n"
    )

    group_info_text = (
        f"[Group Info]\n"
        f"Name: {group_info.get('name')}\n"
        f"Description: {group_info.get('description')}\n"
        f"Website: {group_info.get('website')}\n"
        f"VOIP: {group_info.get('telephone_voip')}, "
        f"Virtual: {group_info.get('telephone_virtual')}\n"
        f"Primary Color: {group_info.get('color_primary')}, Accent: {group_info.get('color_accent')}\n"
        f"Logo: {group_info.get('logo')}\n\n"
    ) if group_info else "[Group Info]\n⚠️ Not available.\n\n"

    professionals_text = f"[Professionals Info]\n{professionals_info}\n\n" if professionals_info else "[Professionals Info]\n⚠️ Not available.\n\n"
    slots_text = f"[Available Slots to Book Appointment]\n{slots_info}\n\n" if slots_info else "[Available Slots to Book Appointment]\n⚠️ Not available.\n\n"

    full_prompt = f"{default_instruction}\n{user_info_text}{group_info_text}{professionals_text}{slots_text} previous_chat_history {user_context.get('previous_response', [])}\nUser: {user_prompt}\nAI:"

    try:
        response = model.generate_content(full_prompt)
        ai_reply = response.text.strip()

        # Handle booking initiation
        if "BOOK AN APPOINTMENT PLEASE" in ai_reply.upper():
            # slot_info_date = extract_slot_info(slots_info)
            slot_info_date, slot_info_time = extract_slot_info(slots_info)
            session_data["awaiting"] = "date"
            session_data["ProfessionalID"] = 0  # Set dynamically if needed
            return jsonify({
                "response": f"📅 Ecco gli orari disponibili:\n\n{slot_info_date}\n\n🗓️ Per prenotare, inserisci la data (formato: aaaa-mm-gg, es: 2024-12-25):\n\n💡 Scrivi 'annulla' in qualsiasi momento per uscire dalla prenotazione."
            })

        # Handle info requests
        match = re.search(r'INFO:\s*(\S+)', ai_reply)
        if match:
            endpoint = match.group(1)
            info_url = f"https://bi.siissoft.com/secureappointment/api/v1/info/{endpoint}"
            try:
                info_response = requests.get(info_url, headers=headers, json={"format": "webbot"}, timeout=10)
                if info_response.status_code == 200:
                    return jsonify({'response': info_response.json().get("message", "✅ Informazioni recuperate con successo.")})
                else:
                    return jsonify({'response': "❌ Non riesco a recuperare le informazioni richieste al momento."})
            except Exception as e:
                return jsonify({'response': f"❌ Errore nel recuperare le informazioni: Riprova più tardi."})

        # Handle specific booking details
        booking_match = re.search(
            r'Provide the Following Details\s*Professional ID\s*:\s*(\d+)\s*Date start\s*:\s*(\S+)\s*Time Start\s*:\s*(\S+)',
            ai_reply, re.IGNORECASE
        )
        if booking_match:
            slot_info_date, slot_info_time = extract_slot_info(slots_info)
            session_data["ProfessionalID"] = int(booking_match.group(1))
            session_data["awaiting"] = "date"
            print( )
            return jsonify({
                'response': f"📅 Ecco gli orari disponibili:\n\n{slot_info_time}\n\n🗓️ Per prenotare con il professionista selezionato, inserisci la data (formato: aaaa-mm-gg, es: 2024-12-25):"
            })

        return jsonify({'response': ai_reply})
    
    except Exception as e:
        print(f"❌ Gemini API Error: {str(e)}")
        return jsonify({'response': "❌ Si è verificato un errore temporaneo. Riprova tra qualche istante."})



# Health check endpoint
@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "healthy",
        "timestamp": time.time(),
        "active_sessions": len(user_session_state),
        "authenticated": access_token is not None
    })

# Admin endpoint to clear all sessions (for debugging)
@app.route('/admin/clear-sessions', methods=['POST'])
def clear_all_sessions():
    global user_session_state
    session_count = len(user_session_state)
    user_session_state = {}
    return jsonify({
        "message": f"Cleared {session_count} sessions",
        "timestamp": time.time()
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Starting Flask app on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)